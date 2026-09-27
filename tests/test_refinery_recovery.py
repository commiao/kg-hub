"""A transient refusal must not stop unrelated backlog for an hour."""
import asyncio
import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_refinery as R
from utils import refinery_recovery as recovery


class RecoveryTests(unittest.TestCase):
    def test_transient_fault_uses_only_readiness_to_resume(self):
        state = {}
        recovery.record_failure(state, "terminal_write_unavailable", cycle=7,
                                interval=90, quota_delay=1800, now=100)
        self.assertFalse(recovery.probe_due(state, 104))
        self.assertTrue(recovery.probe_due(state, 105))
        self.assertEqual(state["reason"], "terminal_write_unavailable")
        recovery.probe_result(state, False, now=105)
        self.assertEqual(state["retry_at"], 120)
        self.assertTrue(recovery.status_fields(state)["upstream_error_paused"])
        recovery.probe_result(state, True, now=120)
        self.assertNotIn("reason", state)
        self.assertEqual(state["hits"], 1)

    def test_real_failure_keeps_pausing_with_bounded_probe_rate(self):
        state = {}
        recovery.record_failure(state, "upstream_error", cycle=1, interval=90,
                                quota_delay=1800, now=0)
        delays = []
        for _ in range(8):
            now = state["retry_at"]
            recovery.probe_result(state, False, now=now)
            delays.append(state["retry_at"] - now)
        self.assertEqual(delays, [15, 30, 60, 300, 300, 300, 300, 300])
        self.assertEqual(state["reason"], "upstream_error")

    def test_daily_quota_uses_utc_reset_not_loop_count(self):
        state = {}
        recovery.record_failure(state, "daily_quota", cycle=1, interval=90,
                                quota_delay=1800, now=86340)
        self.assertEqual(state["retry_at"], 86400)
        recovery.record_failure(state, "upstream_error", cycle=100, interval=90,
                                quota_delay=1800, now=86345)
        self.assertEqual(state["reason"], "daily_quota")
        self.assertEqual(state["retry_at"], 86400)

    def test_failed_backlog_halts_live_but_recovery_runs_other_observations(self):
        async def scenario():
            wm = {"ingested": set(), "rejected": set(), "failed": set()}
            cfg = {"shadow_mode": True, "global": {}, "scoring": {}, "platforms": {"_default": {}}}
            rows = [{"id": i, "project": "p", "title": "t", "type": "discovery",
                     "content_hash": f"h{i}", "narrative": "n"} for i in (1, 2)]
            state, backoff, decided = {}, {}, {1: True, 2: True}
            call = AsyncMock(return_value="terminal_write_unavailable")
            with patch.object(R, "ingest_via_api", call), patch.object(R, "save_watermark"):
                first = await R.process_batch(rows[:1], wm, cfg, None, decided,
                                              backoff, 1, "backlog", state)
                live = await R.process_batch(rows[1:], wm, cfg, None, decided,
                                             backoff, 1, "live", state)
                self.assertEqual(call.await_count, 1)
                self.assertEqual(live["result_counts"].get("halted"), 1)
                self.assertFalse(wm["failed"])
                self.assertIn(1, backoff)
                recovery.probe_result(state, True)
                call.return_value = "ok"
                await R.process_batch(rows, wm, cfg, None, decided, backoff, 2, "backlog", state)
                self.assertEqual(wm["ingested"], {2})
                self.assertEqual(call.await_count, 2)
        asyncio.run(scenario())

    def test_closed_window_never_submits_already_selected_rows(self):
        async def scenario():
            row = {"id": 1, "project": "p", "content_hash": "h", "type": "discovery"}
            with patch.object(R, "ingest_via_api", AsyncMock()) as call:
                result = await R.process_batch(
                    [row], {"ingested": set(), "rejected": set(), "failed": set()},
                    {"shadow_mode": True}, None, {1: True}, {}, 1, "live",
                    can_submit=lambda: False)
                call.assert_not_awaited()
                self.assertEqual(result["result_counts"].get("halted"), 1)
        asyncio.run(scenario())

    def test_precise_gateway_codes_survive_poll(self):
        for kind in ("terminal_write_unavailable", "provider_circuit_open", "gateway_unavailable"):
            with patch.object(R, "_http", return_value=(200, {"status": "error", "error_kind": kind})):
                self.assertEqual(asyncio.run(R.poll_until_done("s", "1")), kind)


if __name__ == "__main__":
    unittest.main()
