"""No production messages: real loopback transport and isolated controller files."""
import contextlib
import io
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import drill_upgrade_acceptance_failure as D
from tools import upgrade_failure_drill_transport as T
from tools import claude_mem_upgrade_acceptance as A

DRILL = "UPGRADE-DRILL-20261015-123456-012345abcdef"


def remote_snapshot():
    return {"anomalies": {"codex_hooks_unhealthy": False}, "pending_sha256": "none",
            "hook_ids": [], "hook_alerted": [], "alert_log_sha256": "same",
            "last_run": "heartbeat", "drill_in_state_or_log": False}


class Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.root = Path(self.temp.name) / DRILL
        self.root.mkdir(mode=0o700)
        self.config = self.home / ".codex/config.toml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text("production sentinel")
        self.production = self.home / ".kg-hub/state/claude-mem-upgrade-acceptance/acceptance.json"
        self.production.parent.mkdir(parents=True)
        self.production.write_text('{"status":"passed","pending_alert":null}')
        self.original = {p:p.read_bytes() for p in (self.config, self.production)}
        self.funcs = {name:getattr(A,name) for name in
                      ("inspect_installation", "run_default_drill", "save_state", "notify_failure")}
        self.original_run = subprocess.run

    def run_drill(self, **kwargs):
        proof = D.run_drill(self.root, home=self.home, **kwargs)
        for path, data in self.original.items():
            self.assertEqual(path.read_bytes(), data)
        for name, func in self.funcs.items():
            self.assertIs(getattr(A, name), func)
        self.assertIs(subprocess.run, self.original_run)
        self.assertEqual(json.loads((self.root / "receipt.json").read_text()), proof)
        self.assertEqual((self.root / "receipt.json").stat().st_mode & 0o777, 0o600)
        return proof

    def test_default_real_loopback_retry_dedup_recovery_no_remote_commands(self):
        commands=[]
        def traced(argv, **kwargs):
            commands.append(argv)
            self.assertNotIn("--live", argv)
            self.assertNotIn("--send-feishu", argv)
            self.assertEqual(Path(argv[1]).name, "upgrade_failure_drill_transport.py")
            return self.original_run(argv, **kwargs)
        with patch.object(D, "nas_snapshot", side_effect=AssertionError("NAS forbidden")), \
             patch.object(A.subprocess, "run", side_effect=traced):
            # The wrapper restores the function that was active at entry.
            proof = D.run_drill(self.root, home=self.home)
        self.assertTrue(proof["ok"], proof)
        self.assertEqual(len(commands), 2)
        self.assertEqual([e["http_status"] for e in proof["deliveries"]], [503,200])
        self.assertEqual([e["pending_attempt"] for e in proof["deliveries"]], [1,2])
        self.assertEqual(proof["real_send_attempts"], 0)
        self.assertEqual(proof["real_messages_confirmed"], 0)
        self.assertFalse(proof["real_preflight_executed"])
        self.assertFalse(proof["nas_verified"])
        self.assertTrue(proof["duplicate_failure_suppressed"] and proof["recovery_quiet"])
        self.assertEqual([r["notification_calls"] for r in proof["rounds"]], [1,2,2,2,2,2,2])
        self.assertEqual([r["status"] for r in proof["rounds"]][-2:], ["unchanged","unchanged"])
        self.assertEqual(self.production.read_bytes(), self.original[self.production])

    def explicit_sender(self, argv, **kwargs):
        self.assertNotIn("--live", argv)
        if "--send-feishu" in argv:
            return subprocess.CompletedProcess(argv,0,json.dumps({"drill_id":DRILL,"destination":"feishu", "ok":True,
                "http_attempts":1,"http_status":200,"feishu_code":0}),"")
        return self.original_run(argv,**kwargs)

    def test_explicit_send_one_attempt_and_nas_check(self):
        with patch.object(D,"nas_snapshot",side_effect=lambda *a:remote_snapshot()) as nas, \
             patch.object(A.subprocess,"run",side_effect=self.explicit_sender):
            proof=D.run_drill(self.root,home=self.home,send_feishu=True,ssh_host="user@nas")
        self.assertTrue(proof["ok"],proof)
        self.assertEqual(proof["real_send_attempts"],1)
        self.assertEqual(proof["real_messages_confirmed"],1)
        self.assertEqual(nas.call_count,2)

    def test_send_failure_keeps_evidence_and_never_resends_or_recovers(self):
        def failure(argv,**kwargs):
            if "--send-feishu" in argv:
                return subprocess.CompletedProcess(argv,1,json.dumps({"drill_id":DRILL,"destination":"feishu","http_attempts":1,"ok":False,"http_status":200,"feishu_code":19024}),"")
            return self.original_run(argv,**kwargs)
        with patch.object(D,"nas_snapshot",return_value=remote_snapshot()), \
             patch.object(A.subprocess,"run",side_effect=failure):
            proof=D.run_drill(self.root,home=self.home,send_feishu=True,ssh_host="user@nas")
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["real_send_attempts"],1)
        self.assertEqual(len(proof["rounds"]),2)
        state=json.loads((self.root/"isolated-acceptance/acceptance.json").read_text())
        self.assertEqual(state["pending_alert"]["attempts"],2)
        self.assertEqual(state["status"],"failed")

    def test_timeout_is_unknown_one_attempt_no_retry(self):
        def timeout(argv,**kwargs):
            if "--send-feishu" in argv:
                raise subprocess.TimeoutExpired(argv,25,stderr=b"https://private/secret")
            return self.original_run(argv,**kwargs)
        with patch.object(D,"nas_snapshot",return_value=remote_snapshot()), \
             patch.object(A.subprocess,"run",side_effect=timeout):
            proof=D.run_drill(self.root,home=self.home,send_feishu=True,ssh_host="user@nas")
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["real_send_attempts"],1)
        self.assertNotIn("https://private",json.dumps(proof))
        self.assertEqual(proof["deliveries"][-1]["result"],"unknown")

    def test_changed_local_or_nas_state_fails_inconclusive_without_resetting(self):
        snap={"capture":{"checked":False},"file_sha256":{"config":"before"}}
        changed={"capture":{"checked":False},"file_sha256":{"config":"after"}}
        with patch.object(D,"local_snapshot",side_effect=[snap,changed]):
            proof=self.run_drill()
        self.assertFalse(proof["ok"])
        self.assertIn("protected local files changed",proof["failure_reason"])

    def test_nas_check_can_run_without_send_and_detects_changed_pending(self):
        changed=remote_snapshot();changed["pending_sha256"]="new real incident"
        with patch.object(D,"nas_snapshot",side_effect=[remote_snapshot(),changed]):
            proof=self.run_drill(verify_nas=True,ssh_host="user@nas")
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["real_send_attempts"],0)
        self.assertIn("NAS alert state changed",proof["failure_reason"])

    def test_malfunctioning_dedup_cannot_send_a_third_message(self):
        real_accept=A.accept
        def broken(*args,**kwargs):
            rc,state=real_accept(*args,**kwargs)
            # Erase the isolated acknowledgement to model a regression in dedup.
            path=self.root/"isolated-acceptance/acceptance.json"
            current=json.loads(path.read_text());current["notified_fingerprint"]=None
            D.write_json(path,current)
            return rc,state
        with patch.object(A,"accept",side_effect=broken):
            proof=self.run_drill()
        self.assertFalse(proof["ok"])
        self.assertEqual(len(proof["deliveries"]),2)


class Transport(unittest.TestCase):
    def test_loopback_rejects_remote_urls_and_no_config_fallback(self):
        for url in ("https://open.feishu.cn/test","http://localhost/test","http://127.0.0.1@remote/test"):
            result=T.deliver(DRILL,loopback_url=url)
            self.assertFalse(result["ok"])
            self.assertEqual(result["http_attempts"],0)

    def fake_sender(self, home, body):
        p=home/".claude/skills/feishu-notify/scripts/send.py"
        p.parent.mkdir(parents=True)
        p.write_text(body)

    def test_real_mode_uses_installed_sender_and_strict_business_receipt(self):
        class Response(io.BytesIO):
            status=200
        sender="import urllib.request\nwith urllib.request.urlopen('https://private/token') as r: r.read()\n"
        for payload,expected in (({"code":0},True),({"StatusCode":0},True),({"code":False},False),({"code":19024},False)):
            with tempfile.TemporaryDirectory() as td:
                home=Path(td);self.fake_sender(home,sender)
                with patch.object(T.urllib.request,"urlopen",side_effect=lambda *a,**k:Response(json.dumps(payload).encode())):
                    result=T.deliver(DRILL,send_feishu=True,home=home)
                self.assertEqual(result["ok"],expected,result)
                self.assertNotIn("private",json.dumps(result))

    def test_sender_exception_output_is_not_leaked(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            self.fake_sender(home,"import sys\nprint('https://private/token',file=sys.stderr)\nraise OSError('https://private/token')")
            result=T.deliver(DRILL,send_feishu=True,home=home)
            self.assertFalse(result["ok"])
            self.assertNotIn("private",json.dumps(result))

    def test_real_http_rejection_records_status_without_secret_url(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            self.fake_sender(home,"import sys,urllib.request\ntry: urllib.request.urlopen('https://private/token')\nexcept Exception: sys.exit(1)\n")
            error=urllib.error.HTTPError("https://private/token",503,"unavailable",{},None)
            with patch.object(T.urllib.request,"urlopen",side_effect=error):
                result=T.deliver(DRILL,send_feishu=True,home=home)
            self.assertFalse(result["ok"])
            self.assertEqual(result["http_status"],503)
            self.assertNotIn("private",json.dumps(result))

    def test_second_sender_http_attempt_blocked(self):
        class Response(io.BytesIO):status=200
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            self.fake_sender(home,"import urllib.request\nfor _ in range(2):\n with urllib.request.urlopen('https://private/token') as r: r.read()\n")
            with patch.object(T.urllib.request,"urlopen",side_effect=lambda *a,**k:Response(b'{"code":0}')) as post:
                result=T.deliver(DRILL,send_feishu=True,home=home)
            self.assertFalse(result["ok"])
            self.assertTrue(result["extra_attempt_blocked"])
            self.assertEqual(post.call_count,1)


class LocalChecks(unittest.TestCase):
    def test_health_read_retries_once_and_never_converts_unknown_to_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);db=home/".claude-mem/claude-mem.db";db.parent.mkdir()
            with contextlib.closing(sqlite3.connect(db)) as c:
                c.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY)")
                c.execute("INSERT INTO observations VALUES (1)")
                c.commit()
            (db.parent/"settings.json").write_text('{"CLAUDE_MEM_WORKER_PORT":12345}')
            with patch.object(D.urllib.request,"build_opener") as build, patch.object(D.time,"sleep"):
                build.return_value.open.side_effect=[TimeoutError(),io.BytesIO(b'{"status":"ok","pid":7}')]
                result=D.local_snapshot(home)
                self.assertEqual(result["capture"]["health_read_attempts"],2)
                self.assertEqual(result["capture"]["observations"],1)
                build.return_value.open.side_effect=TimeoutError()
                with self.assertRaises(D.DrillFailure):D.local_snapshot(home)


class Entry(unittest.TestCase):
    def test_no_live_or_abbreviated_send_flags(self):
        for entry in (D.main,T.main):
            for arg in ("--live","--send-fei"):
                with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as exc:
                    entry([arg])
                self.assertEqual(exc.exception.code,2)

    def test_main_default_is_local_and_preserves_private_unique_receipt(self):
        with tempfile.TemporaryDirectory() as td,patch.object(D,"local_snapshot",return_value={"capture":{"checked":False},"file_sha256":{}}), \
             patch.object(D,"nas_snapshot",side_effect=AssertionError("NAS")),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(D.main(["--output-dir",td]),0)
            self.assertEqual(D.main(["--output-dir",td]),0)
            receipts=list(Path(td).glob("*/receipt.json"));self.assertEqual(len(receipts),2)
            for p in receipts:
                self.assertEqual(p.parent.stat().st_mode&0o777,0o700)
                self.assertEqual(p.stat().st_mode&0o777,0o600)
                self.assertEqual(json.loads(p.read_text())["mode"],"local-only")


if __name__=="__main__":unittest.main()
