"""Held surge → automatic breaker, alerts, and releasing held work without a restart.

2026-10-10: a ~15 s idle cut on the provider side left ~24 possibly-paid calls
an hour in review for hours. Nothing stopped or alerted; and each release of
held work meant stopping the refinery (five times that day).
"""
import ast
import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import breakers  # noqa: E402
from tools import release_held as H  # noqa: E402
from tools import watchdog as W  # noqa: E402
from utils import release_request as R  # noqa: E402
from utils.held_surge import AUTO_BY, HeldSurgeGuard, trip_if_surging  # noqa: E402

KEY = "kg_hub.entity_extract"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class HeldSurgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "breakers.json"
        self.clock = Clock()
        self.guard = HeldSurgeGuard(threshold=3, window=900, clock=self.clock)
        self.brk = SimpleNamespace(
            is_tripped=lambda key: breakers.is_tripped(key, self.path),
            set_tripped=lambda key, tripped, **kw: breakers.set_tripped(key, tripped, path=self.path, **kw))

    def tearDown(self):
        self.temp.cleanup()

    def note(self, at):
        self.clock.now = at
        return trip_if_surging(self.guard, self.brk, KEY)

    def test_trips_at_threshold_inside_the_window(self):
        self.assertIsNone(self.note(0))
        self.assertIsNone(self.note(100))
        tripped = self.note(200)
        self.assertEqual(tripped["count"], 3)
        state = breakers.read_state(self.path)["breakers"][KEY]
        self.assertTrue(state["tripped"])
        self.assertEqual(state["by"], AUTO_BY)
        self.assertIn("3 条任务结果未知", state["reason"])
        self.assertFalse(breakers.is_tripped("claude_mem.observation", self.path)[0])

    def test_old_outcomes_fall_out_of_the_window(self):
        for at in (0, 100, 1000, 1950):
            self.assertIsNone(self.note(at))

    def test_a_manual_reason_is_never_overwritten(self):
        breakers.set_tripped(KEY, True, by="operator", reason="manual", path=self.path)
        for at in (0, 1, 2, 3):
            self.assertIsNone(self.note(at))
        self.assertEqual(breakers.read_state(self.path)["breakers"][KEY]["reason"], "manual")

    def test_zero_threshold_disables(self):
        guard = HeldSurgeGuard(threshold=0, window=900, clock=self.clock)
        for _ in range(10):
            self.assertIsNone(trip_if_surging(guard, self.brk, KEY))


class ServerWiringTests(unittest.TestCase):
    """Run the real update_ingested_key_status against fakes."""

    def load(self, calls):
        source = (ROOT / "kg_hub_server.py").read_text("utf-8")
        fn = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "update_ingested_key_status")
        ns = {"datetime": __import__("datetime").datetime, "timezone": __import__("datetime").timezone,
              "trip_if_surging": lambda guard, brk, key: calls.append(key) or None,
              "_HELD_SURGE": object(), "breakers": object(),
              "logger": SimpleNamespace(error=print, exception=print, warning=print)}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<server>", "exec"), ns)
        return ns["update_ingested_key_status"]

    def test_only_unknown_model_outcomes_feed_the_guard(self):
        calls = []
        update = self.load(calls)

        async def execute_query(*a, **k):
            return [], None, None
        graphiti = SimpleNamespace(driver=SimpleNamespace(execute_query=execute_query))
        for status, kind in (("needs_reconciliation", "model_outcome_unknown"),
                             ("needs_reconciliation", "predigest_incomplete"),
                             ("error", "rate_limited"), ("error", None)):
            asyncio.run(update(graphiti, "sd", "sid", status, error_kind=kind))
        self.assertEqual(calls, [KEY])


class WatchdogTests(unittest.TestCase):
    def judge(self, stats, cfg=None):
        anomalies, details = {}, {}
        W.apply_held_checks(stats, cfg or {}, anomalies, details)
        return anomalies, details

    def test_held_growth_fires_on_the_last_hour_not_the_stock(self):
        self.assertEqual(self.judge({"needs_reconciliation": 4000,
                                     "needs_reconciliation_last_1h": 4})[0], {})
        anomalies, details = self.judge({"needs_reconciliation_last_1h": 12})
        self.assertTrue(anomalies["held_growth"])
        self.assertIn("12 条", details["held_growth"])
        self.assertTrue(self.judge({"needs_reconciliation_last_1h": 2},
                                   {"held_growth_per_hour": 2})[0]["held_growth"])

    def test_only_an_automatic_breaker_alerts(self):
        manual = {"model_breaker": {"tripped": True, "by": "operator", "reason": "x"}}
        self.assertNotIn("model_breaker_auto", self.judge(manual)[0])
        auto = {"model_breaker": {"tripped": True, "by": AUTO_BY, "reason": "15 分钟内 5 条"}}
        anomalies, details = self.judge(auto)
        self.assertTrue(anomalies["model_breaker_auto"])
        self.assertIn("15 分钟内 5 条", details["model_breaker_auto"])

    def test_new_kinds_start_clear_every_cycle(self):
        source = (ROOT / "tools" / "watchdog.py").read_text("utf-8")
        for kind in ("held_growth", "model_breaker_auto"):
            self.assertIn(f'"{kind}": False', source)


class ReleaseRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.wm = {"held": {1, 2, 3}, "ingested": {9}}
        self.saved = []

    def tearDown(self):
        self.temp.cleanup()

    def save(self, wm):
        self.saved.append({k: sorted(v) for k, v in wm.items()})

    def request(self, **body):
        (self.state / R.REQUEST_NAME).write_text(json.dumps({"version": 1, "id": "r1", **body}))

    def test_nothing_pending(self):
        self.assertIsNone(R.apply(self.state, self.wm, self.save))
        self.assertEqual(self.saved, [])

    def test_applies_to_the_in_memory_watermark_and_answers(self):
        self.request(release=[1, 99], ingested=[2])
        result = R.apply(self.state, self.wm, self.save)
        self.assertEqual(self.wm, {"held": {3}, "ingested": {2, 9}})
        self.assertEqual(self.saved, [{"held": [3], "ingested": [2, 9]}])
        self.assertEqual((result["released"], result["marked_ingested"], result["not_held"]), (1, 1, [99]))
        self.assertFalse((self.state / R.REQUEST_NAME).exists())
        self.assertEqual(json.loads(R.receipt_path(self.state, "r1").read_text()), result)

    def test_reapplying_after_a_crash_is_harmless(self):
        self.request(release=[1])
        R.apply(self.state, self.wm, self.save)
        self.request(release=[1])
        result = R.apply(self.state, self.wm, self.save)
        self.assertEqual((result["released"], result["not_held"]), (0, [1]))
        self.assertEqual(self.wm["held"], {2, 3})

    def test_a_bad_request_is_rejected_and_removed(self):
        (self.state / R.REQUEST_NAME).write_text(json.dumps({"version": 1, "id": "../x", "release": [1]}))
        result = R.apply(self.state, self.wm, self.save)
        self.assertIn("id", result["error"])
        self.assertEqual(self.wm["held"], {1, 2, 3})
        self.assertFalse((self.state / R.REQUEST_NAME).exists())
        self.assertTrue((self.state / "release-request.rejected.json").exists())

    def test_refinery_applies_requests_before_the_breaker_gate(self):
        source = (ROOT / "kg_refinery.py").read_text("utf-8")
        loop = source[source.index("    while True:\n        cycle += 1"):]
        self.assertLess(loop.index("release_request.apply(STATE_DIR, wm, save_watermark)"),
                        loop.index("breakers.is_tripped(BREAKER_KEY)"))

    def test_tool_round_trip_with_a_running_refinery(self):
        plan_ok = [4]

        def refinery():
            for _ in range(200):
                if (self.state / R.REQUEST_NAME).exists():
                    R.apply(self.state, self.wm, self.save)
                    return
                time.sleep(0.01)
        t = threading.Thread(target=refinery)
        t.start()
        self.wm["held"].add(4)
        result = H.request_unhold(self.state, [1, 2, 4], plan_ok, wait=5, poll=0.01)
        t.join()
        self.assertEqual((result["released"], result["marked_ingested"]), (2, 1))
        self.assertEqual(self.wm["held"], {3})
        self.assertIn(4, self.wm["ingested"])

    def test_tool_refuses_while_a_request_is_pending(self):
        self.request(release=[1])
        with self.assertRaises(SystemExit):
            H.request_unhold(self.state, [2], [], wait=0)


if __name__ == "__main__":
    unittest.main()
