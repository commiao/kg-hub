"""refinery 存活心跳的回归测试。"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kg_refinery as refinery


class RefineryHeartbeatTests(unittest.TestCase):
    def test_heartbeat_interval_is_bounded_and_invalid_values_fall_back(self):
        self.assertEqual(refinery.bounded_heartbeat_interval(None), 60)
        self.assertEqual(refinery.bounded_heartbeat_interval("oops"), 60)
        self.assertEqual(refinery.bounded_heartbeat_interval("1"), 15)
        self.assertEqual(refinery.bounded_heartbeat_interval("60"), 60)
        self.assertEqual(refinery.bounded_heartbeat_interval("9999"), 300)

    def test_heartbeat_does_not_overwrite_last_progress_timestamp(self):
        """长批次还在跑时，心跳必须能前进而不伪造处理进度。"""
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            with patch.object(refinery, "STATE_DIR", Path(tmp)), \
                 patch.object(refinery, "STATUS", status):
                refinery.write_status(live_cursor=7, idle_outside_window=False)
                before = json.loads(status.read_text())
                refinery.write_status(heartbeat_only=True)
                after = json.loads(status.read_text())

        self.assertEqual(after["ts"], before["ts"])
        self.assertNotEqual(after["heartbeat_at"], before["heartbeat_at"])

    def test_heartbeat_loop_runs_without_consulting_work_window(self):
        """无论 backlog 窗口是否打开，liveness 都应按固定节奏刷新。"""
        async def cancel_after_first_sleep(_seconds):
            raise asyncio.CancelledError

        async def run():
            with patch.object(refinery, "in_backlog_window", side_effect=AssertionError), \
                 patch.object(refinery, "write_status") as write_status, \
                 patch.object(refinery.asyncio, "sleep", new=cancel_after_first_sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await refinery.heartbeat_loop()
                write_status.assert_called_once_with(heartbeat_only=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
