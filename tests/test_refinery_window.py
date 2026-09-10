"""北京时间默认处理窗口的回归测试。"""
from __future__ import annotations

import sys as _sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import unittest

_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kg_refinery as refinery


ROOT = Path(__file__).resolve().parents[1]


class RefineryWindowTests(unittest.TestCase):
    def test_default_window_is_beijing_2200_to_0800(self):
        self.assertEqual(refinery.BACKLOG_START, 22)
        self.assertEqual(refinery.BACKLOG_END, 8)
        self.assertEqual(refinery.CST.utcoffset(None), timedelta(hours=8))

        for hour in (22, 23, 0, 7):
            with self.subTest(hour=hour), patch.object(refinery, "datetime", _clock_at(hour)):
                self.assertTrue(refinery.in_backlog_window())
        for hour in (8, 21):
            with self.subTest(hour=hour), patch.object(refinery, "datetime", _clock_at(hour)):
                self.assertFalse(refinery.in_backlog_window())

    def test_compose_and_probe_match_runtime_default(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        probe = (ROOT / "tools/capture_probe.py").read_text(encoding="utf-8")
        design = (ROOT / "docs/REFINERY-DESIGN.md").read_text(encoding="utf-8")

        self.assertIn("KG_HUB_REFINERY_WINDOW_START=${KG_HUB_REFINERY_WINDOW_START:-22}", compose)
        self.assertIn("KG_HUB_REFINERY_WINDOW_END=${KG_HUB_REFINERY_WINDOW_END:-8}", compose)
        self.assertIn("北京时间 22:00-08:00", probe)
        self.assertIn("北京时间 22:00-08:00", design)


def _clock_at(hour: int):
    class FixedClock:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 10, hour, tzinfo=tz)

    return FixedClock


if __name__ == "__main__":
    unittest.main()
