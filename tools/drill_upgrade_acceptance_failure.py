#!/usr/bin/env python3
"""Replay upgrade acceptance failure safely. Default: local simulation only.

--send-feishu permits one marked failure alert and read-only NAS verification.
--verify-nas adds NAS verification without sending. No --live, hook mutations,
real capture writes, real NAS preflight, worker restart, or automatic scheduling.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import urllib.request
try:
    from . import claude_mem_upgrade_acceptance as A
    from .upgrade_failure_drill_transport import NoRedirect
except ImportError:
    import claude_mem_upgrade_acceptance as A
    from upgrade_failure_drill_transport import NoRedirect


class DrillFailure(RuntimeError):
    """A fixed diagnostic safe to persist without a webhook URL."""


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def local_snapshot(home):
    """Only local reads. Do not launch a real Codex/collector or query NAS."""
    state_path = home / ".kg-hub/state/claude-mem-upgrade-acceptance/acceptance.json"
    files = {home / ".codex/config.toml", state_path,
             home / ".local/share/claude-mem/current/RELEASE.json"}
    if state_path.is_file():
        state = json.loads(state_path.read_text())
        files.update(Path(p) for p in state.get("evidence", {}).get("sources", {}))
    result = {"capture": {"checked": False}, "native_runtime_queried": False}
    db = home / ".claude-mem/claude-mem.db"
    marker = home / ".kg-hub/state/claude-mem-dual-active.json"
    files.add(marker)
    if marker.is_file():
        source = Path(json.loads(marker.read_text())["source_config"])
        files.add(source)
        db = Path(json.loads(source.read_text())["sources"][-1]["path"])
    settings_file = db.parent / "settings.json"
    files.add(settings_file)
    if db.is_file() and settings_file.is_file():
        settings = json.loads(settings_file.read_text())
        port = int(settings.get("CLAUDE_MEM_WORKER_PORT", 37701))
        if not 1 <= port <= 65535:
            raise ValueError("invalid worker port")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        for attempt in range(2):
            try:
                with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
                    health = json.load(response)
                break
            except Exception:
                if attempt:
                    raise DrillFailure("local worker health unavailable after two read attempts")
                time.sleep(0.25)
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            count, max_id = connection.execute("SELECT COUNT(*),MAX(id) FROM observations").fetchone()
        result["capture"] = {"checked": True, "db": str(db), "status": health.get("status"),
                             "pid": health.get("pid"), "observations": count, "max_id": max_id,
                             "health_read_attempts": attempt + 1}
    result["file_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file()
                              else None for p in sorted(files)}
    return result


NAS_SNAPSHOT = """
import hashlib,json,sys
from tools import watchdog as w
raw=w.STATE_FILE.read_bytes();s=json.loads(raw)
log=w.ALERTS_LOG.read_bytes() if w.ALERTS_LOG.exists() else b''
print(json.dumps({'anomalies':s.get('anomalies',{}),
 'pending_sha256':hashlib.sha256(json.dumps(s.get('pending_alerts',{}),sort_keys=True).encode()).hexdigest(),
 'hook_ids':s.get('codex_hook_ids',[]),'hook_alerted':s.get('codex_hook_alerted',[]),
 'state_sha256':hashlib.sha256(raw).hexdigest(),'last_run':s.get('last_run'),
 'alert_log_sha256':hashlib.sha256(log).hexdigest(),
 'drill_in_state_or_log':sys.argv[1].encode() in raw or sys.argv[1].encode() in log}))
"""


def nas_snapshot(host, drill_id):
    if not re.fullmatch(r"[A-Za-z0-9_@.:-]+", host) or host.startswith("-"):
        raise ValueError("invalid SSH host")
    argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if os.environ.get("KG_HUB_SSH_ALLOW_PROXYJUMP") != "1":
        argv += ["-o", "ProxyJump=none"]
    # drill_id is generated locally from digits and hex, never user shell text.
    argv += [host, "sudo -n /var/packages/ContainerManager/target/usr/bin/docker exec -i "
             "kg-hub-watchdog python - " + drill_id]
    response = subprocess.run(argv, input=NAS_SNAPSHOT, capture_output=True, text=True,
                              timeout=30, check=False)
    if response.returncode:
        raise DrillFailure("NAS read-only verification failed")
    return json.loads(response.stdout)


def require(condition, label):
    if not condition:
        raise DrillFailure(label)


def run_drill(root, *, home=None, send_feishu=False, verify_nas=False, ssh_host=None):
    """Root must be a newly created private run directory. No automatic resend."""
    home = home or Path.home()
    drill_id = root.name
    code = Path(A.__file__).resolve().parent
    proof = {"schema_version": 1, "drill_id": drill_id, "ok": False,
             "mode": "send-feishu" if send_feishu else "local-only",
             "controller_sha256": hashlib.sha256(Path(A.__file__).read_bytes()).hexdigest(),
             "production_state_used_for_simulation": False, "live_flag_used": False,
             "deliveries": [], "rounds": [], "stage": "before-checks",
             "real_send_attempts": 0, "real_messages_confirmed": 0,
             "real_preflight_executed": False}
    state_dir = root / "isolated-acceptance"
    fixture_home = root / "virtual-home"
    phase, local_posts, local_preflights = "failed", [], []
    notify_calls = 0
    original_run, original_save = A.subprocess.run, A.save_state
    before, remote_before = None, None
    server = thread = None
    do_nas = verify_nas or send_feishu

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = payload["content"]["text"]
            marked = text.startswith("【演练】") and drill_id in text
            local_posts.append({"marked": marked})
            status = 503 if len(local_posts) == 1 else 200
            self.send_response(status if marked else 400)
            self.end_headers()
            self.wfile.write(json.dumps({"code": 19024 if status == 503 else 0}).encode())
        def log_message(self, *args):
            pass

    def simulated_inspection(*args):
        return "DRILL_ONLY:" + drill_id, {"simulated": True}, []

    def simulated_preflight(code_arg, runs):
        # Fully local fixture: never invoke drill_codex_hooks.py or SSH here.
        path = runs / str(len(local_preflights) + 1) / "receipt.json"
        path.parent.mkdir(parents=True, mode=0o700)
        write_json(path, {"mode": "local-simulated", "real_preflight_executed": False})
        local_preflights.append(str(path))
        return {"ok": phase == "recovered", "reason": None if phase == "recovered"
                else "SIMULATED_UPGRADE_ACCEPTANCE_FAILURE", "receipt": str(path)}

    def guarded_save(path, state):
        require(path == state_dir / "acceptance.json", "state path escaped isolation")
        original_save(path, state)

    def guarded_subprocess(argv, *args, **kwargs):
        nonlocal notify_calls
        # The controller may call only its notifier; all other child commands are forbidden.
        expected = str(fixture_home / ".claude/skills/feishu-notify/scripts/send.py")
        require(isinstance(argv, list) and len(argv) > 1 and argv[1] == expected
                and "--live" not in argv, "unexpected subprocess")
        notify_calls += 1
        require(notify_calls <= 2, "duplicate notification blocked")
        pending = json.loads((state_dir / "acceptance.json").read_text())["pending_alert"]
        require(pending["attempts"] == notify_calls, "retry checkpoint missing")
        real = send_feishu and notify_calls == 2
        command = [sys.executable, str(code / "upgrade_failure_drill_transport.py"),
                   "--drill-id", drill_id]
        if real:
            proof["real_send_attempts"] += 1  # Reserve budget BEFORE ambiguous network results.
            command += ["--send-feishu"]
        else:
            command += ["--loopback-url", f"http://127.0.0.1:{server.server_port}/drill"]
        try:
            response = original_run(command, capture_output=True, text=True, timeout=25, check=False)
            receipt = json.loads(response.stdout)
            require(receipt.get("drill_id") == drill_id
                    and receipt.get("destination") == ("feishu" if real else "loopback")
                    and receipt.get("http_attempts") == 1, "mismatched transport receipt")
            if receipt.get("ok") is True:
                require(response.returncode == 0 and 200 <= receipt.get("http_status", 0) < 300
                        and type(receipt.get("feishu_code")) is int and receipt["feishu_code"] == 0,
                        "business acknowledgement missing")
            else:
                require(response.returncode != 0, "transport exit code mismatch")
            event = dict(receipt, exit_code=response.returncode, pending_attempt=pending["attempts"])
            proof["deliveries"].append(event)
            if real and receipt.get("ok") is True:
                proof["real_messages_confirmed"] += 1
            return response
        except Exception as exc:
            proof["deliveries"].append({"destination": "feishu" if real else "loopback",
                                        "error_type": type(exc).__name__, "result": "unknown"})
            raise

    def cycle(name):
        rc, result = A.accept(fixture_home, code, state_dir)
        current = json.loads((state_dir / "acceptance.json").read_text())
        proof["rounds"].append({"name": name, "exit_code": rc, "status": result["status"],
                               "pending": copy.deepcopy(current.get("pending_alert")),
                               "notified": current.get("notified_fingerprint"),
                               "notification_calls": notify_calls})
        write_json(root / (name + ".json"), current)
        return rc, current

    try:
        before = local_snapshot(home)
        write_json(root / "before-local.json", before)
        if do_nas:
            remote_before = nas_snapshot(ssh_host, drill_id)
            write_json(root / "before-nas.json", remote_before)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        proof["stage"] = "state-machine"
        with ExitStack() as stack:
            for key, value in {"inspect_installation": simulated_inspection,
                               "run_default_drill": simulated_preflight,
                               "save_state": guarded_save}.items():
                stack.enter_context(patch.object(A, key, value))
            stack.enter_context(patch.object(A.subprocess, "run", guarded_subprocess))
            rc, state = cycle("01-rejection")
            require(rc == 1 and state["pending_alert"]["attempts"] == 1
                    and notify_calls == 1 and proof["deliveries"][0].get("http_status") == 503,
                    "rejected delivery was not retained")
            rc, state = cycle("02-retry")
            require(rc == 1 and state["pending_alert"] is None and notify_calls == 2,
                    "retry failed; stop without resending")
            require(len(proof["deliveries"]) == 2 and proof["deliveries"][1].get("ok") is True,
                    "missing successful transport receipt")
            for name in ("03-duplicate-failure", "04-duplicate-failure"):
                rc, state = cycle(name)
                require(rc == 1 and state["pending_alert"] is None and notify_calls == 2,
                        "failure deduplication failed")
            phase = "recovered"
            rc, state = cycle("05-recovery")
            require(rc == 0 and state["pending_alert"] is None
                    and state["notified_fingerprint"] is None, "recovery failed")
            for name in ("06-recovered-quiet", "07-recovered-quiet"):
                rc, state = cycle(name)
                require(rc == 0 and state["status"] == "passed" and notify_calls == 2,
                        "recovery was not silent")
        proof["stage"] = "after-checks"
        after = local_snapshot(home)
        write_json(root / "after-local.json", after)
        require(before["file_sha256"] == after["file_sha256"],
                "protected local files changed; inspect concurrent activity")
        capture_before, capture_after = before["capture"], after["capture"]
        require(capture_before["checked"] == capture_after["checked"], "capture evidence changed")
        if capture_before["checked"]:
            require(capture_before["pid"] == capture_after["pid"]
                    and capture_before["status"] == capture_after["status"] == "ok"
                    and capture_before["db"] == capture_after["db"]
                    and capture_after["observations"] >= capture_before["observations"],
                    "capture isolation could not be confirmed")
        if do_nas:
            after_nas = nas_snapshot(ssh_host, drill_id)
            write_json(root / "after-nas.json", after_nas)
            keys = ("anomalies", "pending_sha256", "hook_ids", "hook_alerted", "alert_log_sha256")
            require(all(remote_before[k] == after_nas[k] for k in keys)
                    and not remote_before["drill_in_state_or_log"] and not after_nas["drill_in_state_or_log"],
                    "NAS alert state changed; inspect concurrent activity")
        require(len(local_preflights) == 5 and notify_calls == 2, "unexpected side effects")
        require(proof["real_send_attempts"] == (1 if send_feishu else 0), "send budget mismatch")
        require(proof["real_messages_confirmed"] == (1 if send_feishu else 0), "delivery unconfirmed")
        proof.update(ok=True, stage="complete", retry_verified=True, duplicate_failure_suppressed=True,
                     recovery_quiet=True, local_production_files_unchanged=True,
                     capture_checked=capture_before["checked"], nas_verified=do_nas,
                     nas_scope="read-only" if do_nas else "not-queried-local-mode",
                     local_preflights=local_preflights, final_pending=None)
    except Exception as exc:
        # Do not include exception text, sender stderr or webhook URLs in the receipt.
        proof["error_type"] = type(exc).__name__
        if isinstance(exc, DrillFailure):
            proof["failure_reason"] = str(exc)
    finally:
        if server:
            server.shutdown(); server.server_close()
        if thread:
            thread.join()
        proof["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(root / "receipt.json", proof)
    return proof


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--send-feishu", action="store_true", help="显式允许一条标注演练的真实失败告警，同时核验 NAS")
    parser.add_argument("--verify-nas", action="store_true", help="只读核验 NAS 生产告警状态；默认不连接 NAS")
    parser.add_argument("--ssh-host", default=os.environ.get("KG_HUB_NAS_SSH", "commiao@100.123.208.32"))
    parser.add_argument("--output-dir", type=Path, default=Path.home() / ".kg-hub/state/drills/upgrade-failure")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_@.:-]+", args.ssh_host) or args.ssh_host.startswith("-"):
        parser.error("invalid SSH host")
    drill_id = "UPGRADE-DRILL-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12]
    root = args.output_dir.expanduser() / drill_id
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    root.chmod(0o700)
    proof = run_drill(root, send_feishu=args.send_feishu, verify_nas=args.verify_nas, ssh_host=args.ssh_host)
    print(f"{proof['mode']} {'通过' if proof['ok'] else '失败；先查回执，勿自动重跑'}；回执：{root / 'receipt.json'}")
    return 0 if proof["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
