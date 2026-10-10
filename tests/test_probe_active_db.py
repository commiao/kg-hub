"""工具栏与 SQLite 节点要读当前一代采集库，不是双源切换后冻结的旧库。

2026-10-08：切到双源 10 天后，看板仍按旧库报「Claude Code 已空闲 12 天」。
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tools.capture_probe as P


def make_db(path, last_ms):
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE sdk_sessions (memory_session_id TEXT, platform_source TEXT);"
        "CREATE TABLE observations (id INTEGER PRIMARY KEY, memory_session_id TEXT,"
        " created_at_epoch INTEGER);")
    con.execute("INSERT INTO sdk_sessions VALUES ('m', 'claude')")
    con.execute("INSERT INTO observations VALUES (1, 'm', ?)", (last_ms,))
    con.commit()
    con.close()


class ActiveCaptureDbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        now_ms = time.time() * 1000
        self.legacy, self.current = root / "legacy.db", root / "current.db"
        make_db(self.legacy, now_ms - 12 * 86400 * 1000)
        make_db(self.current, now_ms - 60 * 1000)
        self.config = root / "sources.json"
        self.config.write_text(json.dumps({"sources": [
            {"name": "legacy", "path": str(self.legacy)},
            {"name": "next", "path": str(self.current)}]}))
        self.marker = root / "dual-active.json"
        self.saved = (P.CM_DB, P.DUAL_MARKER)
        P.CM_DB = self.legacy

    def tearDown(self):
        P.CM_DB, P.DUAL_MARKER = self.saved
        self.temp.cleanup()

    def claude_state(self):
        nodes, _ = P.probe_tools()
        return next(n for n in nodes if n["id"] == "tool:claude")["state"]

    def test_dual_capture_reads_current_generation(self):
        self.marker.write_text(json.dumps({"source_config": str(self.config)}))
        P.DUAL_MARKER = self.marker
        self.assertEqual(P.active_capture_db(), self.current)
        self.assertEqual(self.claude_state(), P.GREEN)
        node, max_id = P.probe_sqlite()
        self.assertIn("current.db", node["detail"])

    def test_single_source_keeps_legacy_db(self):
        P.DUAL_MARKER = self.marker            # 不存在
        self.assertEqual(P.active_capture_db(), self.legacy)
        self.assertEqual(self.claude_state(), P.AMBER)

    def test_unreadable_config_falls_back_to_legacy_db(self):
        self.marker.write_text("{not json")
        P.DUAL_MARKER = self.marker
        self.assertEqual(P.active_capture_db(), self.legacy)


if __name__ == "__main__":
    unittest.main()
