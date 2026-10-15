"""Real temporary files + local HTTP receiver; no production channel is used."""
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import watchdog as W
from tools import codex_hook_drill_runner as R
from tools import drill_codex_hooks as C

DRILL = "HOOK-DRILL-20261015-123456-012345abcdef"


class Drill(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "production.json"
        self.log = self.base / "production.log"
        self.state.write_text('{"anomalies":{"codex_hooks_unhealthy":true},"sentinel":42}')
        self.log.write_text("production sentinel\n")
        self.sentinels = {p: p.read_bytes() for p in (self.state, self.log)}
        self.globals = {k: getattr(W, k) for k in
                        ("STATE_DIR", "STATE_FILE", "ALERTS_LOG", "emit_alert", "save_state",
                         "write_alert_log", "FEISHU_WEBHOOK", "send_feishu")}
        self.original_post = W.httpx.post
        # Retain real tempfile behavior while keeping all drill data under the test tree.
        mkdtemp = tempfile.mkdtemp
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(R.tempfile, "mkdtemp",
            side_effect=lambda **kw: mkdtemp(prefix=kw["prefix"], dir=self.base)))
        self.stack.enter_context(patch.object(R.time, "sleep"))
        for key, val in {"STATE_DIR": self.base, "STATE_FILE": self.state,
                         "ALERTS_LOG": self.log}.items():
            self.stack.enter_context(patch.object(W, key, val))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def assert_isolated(self, proof):
        for path, data in self.sentinels.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(W.STATE_FILE, self.state)
        self.assertEqual(W.ALERTS_LOG, self.log)
        self.assertIs(W.httpx.post, self.original_post)
        self.assertIs(W.emit_alert, self.globals["emit_alert"])
        root = Path(proof["state_directory"])
        self.assertNotEqual(root, self.base)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        receipt = root / "receipt.json"
        self.assertEqual(json.loads(receipt.read_text()), proof)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertFalse(proof["production_state_used"])

    @contextlib.contextmanager
    def receiver(self, responses):
        payloads = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                status, body = responses[min(len(payloads) - 1, len(responses) - 1)]
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/private-token", payloads
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def live(self, url):
        with patch.object(W, "load_notify_config", return_value={"feishu_webhook": url}):
            return R.run_drill(W, DRILL, live=True)

    def test_dry_run_cannot_send_and_restores_globals(self):
        with patch.object(W.httpx, "post", side_effect=AssertionError("real network")) as post, \
             patch.object(W, "load_notify_config", side_effect=AssertionError("real config")), \
             patch.object(W, "send_macos_notification", side_effect=AssertionError("fallback")), \
             patch.object(W, "check_capture_chain", side_effect=AssertionError("real topology")), \
             patch.object(W, "main", side_effect=AssertionError("real watchdog")):
            proof = R.run_drill(W, DRILL)
            post.assert_not_called()
        self.assertTrue(proof["ok"])
        self.assertEqual(proof["mode"], "dry-run")
        self.assertEqual(len(proof["messages"]), 2)
        self.assertTrue(all(m["simulated"] and not m["delivered"] for m in proof["messages"]))
        self.assertNotIn("feishu_code", proof["messages"][0])
        self.assertEqual(proof["final_pending"], {})
        self.assert_isolated(proof)

    def test_live_real_http_fire_clear_and_dedup(self):
        with self.receiver([(200, b'{"code":0}'), (200, b'{"StatusCode":0}')]) as (url, payloads):
            proof = self.live(url)
        self.assertTrue(proof["ok"])
        self.assertEqual(len(payloads), 2)
        self.assertEqual([m["severity"] for m in proof["messages"]], ["fire", "clear"])
        self.assertTrue(all(m["delivered"] and m["feishu_code"] == 0 for m in proof["messages"]))
        for body in payloads:
            text = body["content"]["text"]
            self.assertIn("【演练】", text.splitlines()[0])
            self.assertIn(DRILL, text)
        self.assertIn("未批准", payloads[0]["content"]["text"])
        self.assertIn("hash 失效", payloads[0]["content"]["text"])
        self.assertIn("sha256:", payloads[0]["content"]["text"])
        self.assertTrue(proof["duplicate_fault_suppressed"])
        self.assertTrue(proof["duplicate_recovery_suppressed"])
        self.assert_isolated(proof)

    def test_failed_fire_never_sends_clear_or_retries(self):
        for status, body in [(200, b'{"code":19024}'), (503, b'{"code":0}'),
                             (200, b'invalid json'), (200, b'{"code":false}')]:
            with self.subTest(status=status, body=body), self.receiver([(status, body)]) as (url, payloads):
                proof = self.live(url)
            self.assertFalse(proof["ok"])
            self.assertEqual(proof["stage"], "fire")
            self.assertEqual(len(payloads), 1)
            self.assertFalse(proof["messages"][0]["delivered"])
            self.assertEqual(proof["final_pending"][R.KIND]["severity"], "fire")
            self.assert_isolated(proof)

    def test_failed_clear_retains_pending_without_resending_fire(self):
        with self.receiver([(200, b'{"code":0}'), (200, b'{"code":19024}')]) as (url, payloads):
            proof = self.live(url)
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["stage"], "clear")
        self.assertEqual(len(payloads), 2)
        self.assertEqual(proof["final_pending"][R.KIND]["severity"], "clear")
        self.assert_isolated(proof)

    def test_live_requires_enabled_feishu_no_macos_fallback(self):
        for cfg in ({"enabled": False}, {}):
            with patch.object(W, "load_notify_config", return_value=cfg), \
                 patch.object(W, "FEISHU_WEBHOOK", ""), \
                 patch.object(W, "send_macos_notification") as fallback:
                proof = R.run_drill(W, DRILL, live=True)
            self.assertFalse(proof["ok"])
            self.assertEqual(proof["messages"], [])
            fallback.assert_not_called()
            self.assert_isolated(proof)

    def test_transport_failure_does_not_leak_webhook(self):
        with patch.object(W.httpx, "post", side_effect=OSError("https://private/token")):
            proof = self.live("https://private/token")
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["messages"][0]["transport_error"], "OSError")
        self.assertNotIn("https://private", json.dumps(proof))
        self.assert_isolated(proof)

    def test_rule_that_drops_one_fault_fails_before_delivery(self):
        judge = W.judge_snapshots
        def partial(*args):
            decision = judge(*args)
            return decision._replace(codex_hooks=decision.codex_hooks[:1],
                                     codex_hook_ids=decision.codex_hook_ids[:1])
        with patch.object(W, "judge_snapshots", side_effect=partial):
            proof = R.run_drill(W, DRILL)
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["messages"], [])
        self.assertIn("both missing approval and changed hash", proof["error"])
        self.assert_isolated(proof)

    def test_isolation_guard_fails_before_writing_production(self):
        judge = W.judge_snapshots
        def escape(*args):
            W.STATE_FILE = self.state
            return judge(*args)
        with patch.object(W, "judge_snapshots", side_effect=escape):
            proof = R.run_drill(W, DRILL)
        self.assertFalse(proof["ok"])
        self.assertEqual(proof["messages"], [])
        self.assertEqual(proof["error"], "state isolation lost")
        self.assert_isolated(proof)

    def test_repeat_runs_have_distinct_state_directories(self):
        first, second = R.run_drill(W, DRILL), R.run_drill(W, DRILL)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertNotEqual(first["state_directory"], second["state_directory"])


class EntryPoint(unittest.TestCase):
    def test_explicit_live_and_safe_ssh_arguments(self):
        with patch.dict(os.environ, {}, clear=True):
            command = C.ssh_command("user@nas", "kg-hub-watchdog", DRILL, False)
        self.assertNotIn("--live", shlex.split(command[-1]))
        self.assertIn("ProxyJump=none", command)
        self.assertIn("--live", shlex.split(C.ssh_command("user@nas", "watchdog", DRILL, True)[-1]))
        for host in ["-oProxyCommand=bad", "nas;bad", "nas $(bad)"]:
            with self.assertRaises(ValueError):
                C.ssh_command(host, "watchdog", DRILL, False)

    def test_cli_default_streams_runner_and_saves_private_receipt(self):
        def fake_ssh(command, **kwargs):
            self.assertIn("def run_drill", kwargs["input"])
            self.assertNotIn("shell", kwargs)
            remote = shlex.split(command[-1])
            drill = remote[remote.index("--drill-id") + 1]
            self.assertNotIn("--live", remote)
            proof = {"drill_id": drill, "mode": "dry-run", "ok": True}
            return subprocess.CompletedProcess(command, 0, R.RECEIPT_PREFIX + json.dumps(proof), "")
        with tempfile.TemporaryDirectory() as td, patch.object(C.subprocess, "run", side_effect=fake_ssh), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(C.main(["--output-dir", td]), 0)
            receipt = next(Path(td).glob("*/receipt.json"))
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(receipt.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(receipt.read_text())["mode"], "dry-run")

    def test_missing_receipt_timeout_and_nonzero_fail_closed(self):
        for result in [subprocess.CompletedProcess([], 1, "", "https://private/token"),
                       subprocess.TimeoutExpired([], 120, output=b"https://private/token", stderr="timeout"),
                       OSError("https://private/token")]:
            with self.subTest(result=type(result).__name__), tempfile.TemporaryDirectory() as td:
                kw = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch.object(C.subprocess, "run", **kw) as run, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(C.main(["--output-dir", td]), 1)
                    run.assert_called_once()
                for path in Path(td).glob("*/*"):
                    self.assertNotIn("https://private/token", path.read_text())
                proof = json.loads(next(Path(td).glob("*/receipt.json")).read_text())
                self.assertEqual(proof["delivery_status"], "unknown")
                self.assertFalse(proof["automatic_retry"])


if __name__ == "__main__":
    unittest.main()
