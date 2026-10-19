"""不送任务的空档(KG_HUB_REFINERY_BLACKOUT)。

家里路由器每天约 02:00 重启，NAS 出网断几分钟。这段时间送出去的任务会落成
URLError/超时的「结果未知」，进账本后要人工对账 —— 之前的交接把它误归成供应商
故障时段。所以默认北京时间 01:55-02:10 live 与 backlog 都不送新任务，在飞的不动。

钉住的是：时区与工作窗口同一个(北京时间)、左闭右开、跨午夜、空串关闭、
与工作窗口叠加时只收窄不放宽、状态文件与看板能看出处于空档。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dashboard_status  # noqa: E402
import kg_refinery as refinery  # noqa: E402
import refinery_window  # noqa: E402
import topology  # noqa: E402


def _clock_at(hour: int, minute: int = 0):
    class FixedClock:
        @classmethod
        def now(cls, tz=None):
            # 按北京时间构造再换到调用方要的时区，模拟真实 datetime.now(tz=...)。
            beijing = datetime(2026, 10, 19, hour, minute, tzinfo=refinery.CST)
            return beijing.astimezone(tz) if tz is not None else beijing.replace(tzinfo=None)

    return FixedClock


def _at(hour: int, minute: int = 0):
    return patch.object(refinery, "datetime", _clock_at(hour, minute))


class ParseTests(unittest.TestCase):
    def test_default_is_beijing_0155_to_0210(self):
        # run_all.sh 清掉了宿主的 KG_HUB_*，这里读到的就是代码默认值。
        self.assertEqual(refinery.BLACKOUT_RAW, "01:55-02:10")
        self.assertEqual(refinery.BLACKOUT, (115, 130))

    def test_empty_disables(self):
        self.assertIsNone(refinery_window.parse_blackout(""))
        self.assertIsNone(refinery_window.parse_blackout("   "))
        self.assertIsNone(refinery_window.parse_blackout(None))

    def test_bad_values_are_rejected_not_silently_disabled(self):
        for raw in ("02:00", "2-3", "25:00-02:00", "01:60-02:00", "02:00-02:00",
                    "01:55-02:10-03:00", "ab:cd-01:00"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                refinery_window.parse_blackout(raw)

    def test_compose_passes_empty_string_through(self):
        # ":-" 会把空串换回默认值，关不掉空档；必须是 "-"。
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("KG_HUB_REFINERY_BLACKOUT=${KG_HUB_REFINERY_BLACKOUT-01:55-02:10}", compose)


class InBlackoutTests(unittest.TestCase):
    def test_inside_and_outside_default(self):
        for (h, m), want in {(1, 54): False, (1, 55): True, (2, 0): True,
                             (2, 9): True, (2, 10): False, (14, 0): False}.items():
            with self.subTest(at=f"{h:02d}:{m:02d}"), _at(h, m):
                self.assertEqual(refinery.in_blackout(), want)

    def test_uses_beijing_time_not_utc(self):
        # 北京 02:00 == UTC 18:00。按 UTC 判就会在北京 10:00 停送。
        with _at(2, 0):
            self.assertTrue(refinery.in_blackout())
        with _at(10, 0):
            self.assertFalse(refinery.in_blackout())

    def test_crossing_midnight(self):
        span = refinery_window.parse_blackout("23:50-00:10")
        with patch.object(refinery, "BLACKOUT", span):
            for (h, m), want in {(23, 49): False, (23, 50): True, (0, 0): True,
                                 (0, 9): True, (0, 10): False, (2, 0): False}.items():
                with self.subTest(at=f"{h:02d}:{m:02d}"), _at(h, m):
                    self.assertEqual(refinery.in_blackout(), want)

    def test_switch_off(self):
        with patch.object(refinery, "BLACKOUT", None):
            for minute in range(0, 24 * 60, 5):
                with _at(minute // 60, minute % 60):
                    self.assertFalse(refinery.in_blackout())


class WindowOverlayTests(unittest.TestCase):
    """空档叠加在工作窗口之上：只收窄，不会让窗口外变成可送。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "refinery-window.json"

    def check(self, cases):
        with patch.object(refinery, "WINDOW_CONFIG", self.config):
            for (h, m), want in cases.items():
                with self.subTest(at=f"{h:02d}:{m:02d}"), _at(h, m):
                    self.assertEqual(refinery.submission_open(), want)

    def test_fallback_night_window(self):
        # 配置文件缺失 → 回退 22:00-08:00；空档落在窗口里面。
        self.check({(1, 0): True, (1, 55): False, (2, 9): False, (2, 10): True,
                    (12, 0): False})

    def test_all_day_window_still_honours_blackout(self):
        # 线上批准的正本是 all_day(config/refinery-window.json)。
        self.config.write_text('{"mode":"all_day"}')
        self.check({(1, 54): True, (2, 0): False, (2, 10): True, (12, 0): True})

    def test_blackout_outside_window_never_opens_anything(self):
        with patch.object(refinery, "BLACKOUT", refinery_window.parse_blackout("12:00-12:30")):
            self.check({(12, 10): False, (12, 40): False, (23, 0): True, (2, 0): True})

    def test_switch_off_restores_plain_window(self):
        self.config.write_text('{"mode":"all_day"}')
        with patch.object(refinery, "BLACKOUT", None):
            self.check({(2, 0): True})


class NoNewSubmissionTests(unittest.TestCase):
    def test_selected_row_is_not_sent_inside_blackout(self):
        # 主循环把 can_submit 交给 process_batch / 调度池；空档内它必须为假。
        row = {"id": 1, "project": "p", "content_hash": "h", "type": "discovery"}

        async def scenario():
            with patch.object(refinery, "ingest_via_api", AsyncMock(return_value="ok")) as call:
                result = await refinery.process_batch(
                    [row], {"ingested": set(), "rejected": set(), "failed": set()},
                    {"shadow_mode": True}, None, {1: True}, {}, 1, "live",
                    can_submit=refinery.submission_open)
                return call, result

        with tempfile.TemporaryDirectory() as d, \
                patch.object(refinery, "WINDOW_CONFIG", Path(d) / "absent.json"), \
                patch.object(refinery, "save_watermark", lambda wm: None):
            with _at(2, 0):
                call, result = asyncio.run(scenario())
            call.assert_not_awaited()
            self.assertEqual(result["result_counts"].get("halted"), 1)
            with _at(2, 10):
                call, result = asyncio.run(scenario())
            call.assert_awaited_once()


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self._orig = (refinery.STATE_DIR, refinery.STATUS)
        refinery.STATE_DIR, refinery.STATUS = d, d / "status.json"
        self.addCleanup(self._restore)

    def _restore(self):
        refinery.STATE_DIR, refinery.STATUS = self._orig

    def read(self) -> dict:
        return json.loads(refinery.STATUS.read_text())

    def test_status_reports_blackout_on_every_write(self):
        with _at(2, 0):
            refinery.write_status(backlog_window_open=True, last_error=None)
        self.assertIs(self.read()["blackout_active"], True)
        self.assertEqual(self.read()["blackout"], "01:55-02:10")
        # 空档结束后连心跳也会把它刷回 False，不会被旧结论盖住。
        with _at(2, 10):
            refinery.write_status(heartbeat_only=True)
        self.assertIs(self.read()["blackout_active"], False)

    def test_switched_off_reports_null_spec(self):
        with patch.object(refinery, "BLACKOUT", None), \
                patch.object(refinery, "BLACKOUT_RAW", ""), _at(2, 0):
            refinery.write_status(heartbeat_only=True)
        self.assertIs(self.read()["blackout_active"], False)
        self.assertIsNone(self.read()["blackout"])


class DashboardTests(unittest.TestCase):
    NOW = datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc)

    def status(self, **extra):
        base = {"heartbeat_at": (self.NOW - timedelta(seconds=30)).isoformat(),
                "backlog_window_open": True, "backlog_remaining": 15000,
                "blackout_active": True, "blackout": "01:55-02:10"}
        base.update(extra)
        return base

    def test_activity_is_planned_amber_not_red(self):
        activity = dashboard_status.refinery_activity(self.status(), self.NOW)
        self.assertEqual(activity["state"], "amber")
        self.assertIn("空档", activity["label"])

    def test_blackout_is_not_a_stall_alarm(self):
        # 空档是计划内的，不该让「窗口开着却没干活」的告警响。
        self.assertFalse(topology.refinery_halt(self.status())["halted"])


if __name__ == "__main__":
    unittest.main()
