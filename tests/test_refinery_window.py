"""北京时间默认处理窗口的回归测试。"""
from __future__ import annotations

import sys as _sys
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import json

_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kg_refinery as refinery
import refinery_window


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
        self.assertIn("按实时配置", probe)
        self.assertIn("refinery-window.json", design)

    def test_reviewed_all_day_config_takes_effect_without_reload(self):
        with TemporaryDirectory() as directory:
            active = Path(directory) / "refinery-window.json"
            reviewed = ROOT / "config/refinery-window.json"
            with patch.object(refinery, "WINDOW_CONFIG", active):
                refinery_window.apply(reviewed, active)
                for hour in range(24):
                    with self.subTest(hour=hour), patch.object(refinery, "datetime", _clock_at(hour)):
                        self.assertTrue(refinery.in_backlog_window())
                alternate = Path(directory) / "alternate.json"
                alternate.write_text(json.dumps({"mode": "hours", "start_hour": 9, "end_hour": 18}))
                refinery_window.apply(alternate, active)
                with patch.object(refinery, "datetime", _clock_at(8)):
                    self.assertFalse(refinery.in_backlog_window())
                with patch.object(refinery, "datetime", _clock_at(9)):
                    self.assertTrue(refinery.in_backlog_window())
                with patch.object(refinery, "datetime", _clock_at(18)):
                    self.assertFalse(refinery.in_backlog_window())

    def test_invalid_config_is_rejected_and_does_not_replace_active_file(self):
        with TemporaryDirectory() as directory:
            active = Path(directory) / "refinery-window.json"
            active.write_text('{"mode":"all_day"}')
            bad = Path(directory) / "bad.json"
            bad.write_text('{"mode":"hours","start_hour":8,"end_hour":8}')
            with self.assertRaises(ValueError):
                refinery_window.apply(bad, active)
            self.assertEqual(refinery_window.load(active), ("all_day", None, None))
            active.write_text('{invalid')
            with patch.object(refinery, "WINDOW_CONFIG", active):
                with self.assertRaises(ValueError):
                    refinery.in_backlog_window()


def _clock_at(hour: int):
    class FixedClock:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 10, hour, tzinfo=tz)

    return FixedClock


if __name__ == "__main__":
    unittest.main()
