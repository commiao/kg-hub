"""Regression for the active capture worker, isolated from host configuration."""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tools.capture_probe as P


class WorkerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = self.root / "legacy"
        self.current = self.root / "next"
        self.now = time.time()
        for directory, age in ((self.old, 86400), (self.current, 60)):
            (directory / "logs").mkdir(parents=True)
            con = sqlite3.connect(directory / "claude-mem.db")
            con.execute("CREATE TABLE observations (created_at_epoch INTEGER)")
            con.execute("INSERT INTO observations VALUES (?)", (int((self.now-age)*1000),))
            con.commit()
            con.close()
        self.marker = self.root / "dual-active.json"
        runtime_patch = patch.multiple(P, CM_DB=self.old / "claude-mem.db", CM_LOG_DIR=self.old / "logs",
                       DUAL_MARKER=self.marker)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)

    def activate(self):
        config = self.root / "sources.json"
        config.write_text(json.dumps({"sources": [
            {"name": "legacy", "path": str(self.old / "claude-mem.db")},
            {"name": "next", "path": str(self.current / "claude-mem.db")}]}))
        self.marker.write_text(json.dumps({"source_config": str(config)}))
        (self.current / "settings.json").write_text(json.dumps({
            "CLAUDE_MEM_WORKER_PORT": "37721", "CLAUDE_MEM_DATA_DIR": str(self.current)}))

    def log(self, directory, age, text):
        stamp = datetime.fromtimestamp(self.now-age).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        (directory / "logs" / "claude-mem-test.log").write_text(
            "".join(f"[{stamp}] [ERROR] [PARSER] {line}\n" for line in text))

    def test_active_worker_uses_current_health_db_and_logs(self):
        self.activate()
        self.log(self.old, 30, ["SDK authentication failed; API Error: 401 old-key"])
        # A current-instance failure older than its successful observation is resolved.
        self.log(self.current, 120, ["SDK authentication failed; API Error: 401 previous"])
        calls = []
        def health(url, **kwargs):
            calls.append(url)
            if url != "http://127.0.0.1:37721/api/health":
                return None, "HTTP 502"
            return {"status": "ok", "pid": 123, "uptime": 90, "version": "fixture"}, None
        with patch.object(P, "http_json", health):
            node = P.probe_worker()
        self.assertEqual(calls, ["http://127.0.0.1:37721/api/health"])
        self.assertEqual(node["state"], P.GREEN, node)
        self.assertEqual(node["metrics"]["pid"], 123)

    def test_active_worker_connection_failure_stays_red_on_current_port(self):
        self.activate()
        with patch.object(P, "http_json", return_value=(None, "URLError")) as http:
            node = P.probe_worker()
        self.assertEqual(node["state"], P.RED)
        self.assertIn(":37721", node["detail"])
        self.assertEqual(http.call_args.args[0], "http://127.0.0.1:37721/api/health")

    def test_current_generation_failure_is_not_hidden_by_old_instance(self):
        self.activate()
        self.log(self.current, 10, ["SDK authentication failed; API Error: 401 current-key"])
        with patch.object(P, "http_json", return_value=({"status": "ok"}, None)):
            node = P.probe_worker()
        self.assertEqual(node["state"], P.RED)
        self.assertIn("401", node["detail"])

    def test_activated_worker_missing_settings_does_not_fall_back_to_old_port(self):
        self.activate()
        (self.current / "settings.json").unlink()
        with patch.object(P, "http_json", return_value=(None, "fixture")) as http:
            node = P.probe_worker()
        self.assertEqual(node["state"], P.RED)
        self.assertIn("配置", node["detail"])
        http.assert_not_called()

    def test_invalid_port_and_mismatched_data_directory_are_red(self):
        self.activate()
        settings = self.current / "settings.json"
        for port, directory in ((True, self.current), ("bad", self.current),
                                (70000, self.current), (37721, self.old)):
            with self.subTest(port=port, directory=directory):
                settings.write_text(json.dumps({"CLAUDE_MEM_WORKER_PORT": port,
                                                "CLAUDE_MEM_DATA_DIR": str(directory)}))
                with patch.object(P, "http_json", return_value=(None, "fixture")) as http:
                    node = P.probe_worker()
                self.assertEqual(node["state"], P.RED)
                self.assertIn("配置", node["detail"])
                http.assert_not_called()

    def test_legacy_missing_settings_uses_default_port(self):
        with patch.object(P, "http_json", return_value=({"status": "ok"}, None)) as http:
            node = P.probe_worker()
        self.assertEqual(node["state"], P.GREEN)
        self.assertEqual(http.call_args.args[0], "http://127.0.0.1:37701/api/health")

    def test_probe_http_bypasses_proxy_and_preserves_post_auth(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"status":"ok"}'
        opener = unittest.mock.Mock()
        opener.open.return_value = Response()
        with patch.object(P.urllib.request, "build_opener", return_value=opener) as build, \
             patch.object(P.urllib.request, "urlopen", side_effect=AssertionError("system proxy")):
            data, err = P.http_json("http://127.0.0.1:37721/api/health", method="POST",
                                   payload={"fixture": 1}, token="fixture-only-token")
        self.assertIsNone(err)
        self.assertEqual(data, {"status": "ok"})
        self.assertEqual(build.call_args.args[0].proxies, {})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"fixture": 1})
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-only-token")


if __name__ == "__main__":
    unittest.main()
