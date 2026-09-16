"""「该干活却没干」必须报警 —— 温度只是代理，停工才是后果。

2026-09-16 实测的真事故：几块盘稳态 58°C，而 refinery 的歇工线是 `>= 58`、
watchdog 的温度告警线是 `>= 59`。中间一度的盲区正好卡住：**活停了，但没人被告知**。
积压 7509 条六天一条没动，零告警，是我主动去查才发现的。

所以这套测试钉的不是温度阈值，而是两件事：
1. 判决由服务端算，口径只有一份（面板和告警不会各算各的）；
2. 只有「窗口开着却被环境门控挡住」才算停工——窗口外不干活是设计不是故障，
   人工断路是运维动作不是故障。误报会让告警很快被无视，那比没有告警更糟。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import topology as T  # noqa: E402


class RefineryHaltVerdictTests(unittest.TestCase):
    def verdict(self, **status):
        base = {"backlog_window_open": True, "backlog_remaining": 7509,
                "disk_temp": 58, "ts": "2026-09-16T07:26:56+00:00"}
        base.update(status)
        return T.refinery_halt(base)

    def test_thermal_hold_inside_the_window_is_a_stall(self):
        # 这就是六天没被发现的那个状态。
        v = self.verdict(thermal_hold=True)
        self.assertTrue(v["halted"])
        self.assertIn("盘温门控", v["gates"])
        self.assertFalse(v["deliberate"])
        self.assertEqual(v["backlog_remaining"], 7509)
        self.assertEqual(v["disk_temp"], 58)

    def test_idle_outside_the_window_is_not_a_stall(self):
        # 窗口外不干活是设计。这里误报，告警很快就会被无视。
        v = self.verdict(backlog_window_open=False, thermal_hold=True)
        self.assertFalse(v["halted"])
        self.assertFalse(v["window_open"])

    def test_quota_and_rate_limit_also_count(self):
        for key, label in (("quota_paused", "配额耗尽停发"),
                           ("rate_limited", "被限速")):
            with self.subTest(gate=key):
                v = self.verdict(**{key: True})
                self.assertTrue(v["halted"])
                self.assertIn(label, v["gates"])

    def test_manual_breaker_is_reported_but_flagged_deliberate(self):
        # 人工断路是运维**主动**动作。要看得见，但不能和环境故障混为一谈。
        v = self.verdict(breaker_open=True, breaker_reason="悬账在涨")
        self.assertTrue(v["deliberate"])
        self.assertTrue(any("人工断路" in g for g in v["gates"]))
        self.assertTrue(any("悬账在涨" in g for g in v["gates"]))

    def test_working_normally_is_not_a_stall(self):
        v = self.verdict()
        self.assertFalse(v["halted"])
        self.assertEqual(v["gates"], [])

    def test_missing_or_garbage_status_never_claims_a_stall(self):
        # 读不到就别猜。凭空报警和漏报一样坏。
        for bad in (None, {}, "nonsense", 42, []):
            with self.subTest(value=bad):
                v = T.refinery_halt(bad)
                self.assertFalse(v["halted"])
                self.assertEqual(v["gates"], [])


class WatchdogWiringTests(unittest.TestCase):
    """告警项要真接上：少一处声明就永远不会发出来。"""

    def setUp(self):
        self.source = (Path(__file__).resolve().parent.parent
                       / "tools" / "watchdog.py").read_text("utf-8")

    def test_the_anomaly_is_declared_set_and_described(self):
        self.assertIn('"refinery_stalled": False', self.source)   # 初值
        self.assertIn('new_anomalies["refinery_stalled"] = True', self.source)
        self.assertIn('details["refinery_stalled"]', self.source)

    def test_the_verdict_comes_from_the_server_not_recomputed_here(self):
        # 面板和告警必须看同一份判决，否则会出现「面板红着但没告警」的裂缝。
        self.assertIn("def check_refinery_halt", self.source)
        self.assertIn('payload.get("refinery")', self.source)
        self.assertNotIn("thermal_hold", self.source.split(
            "def check_refinery_halt", 1)[1][:800])

    def test_an_unreadable_verdict_does_not_alarm(self):
        body = self.source.split("def check_refinery_halt", 1)[1][:900]
        self.assertIn("return None", body)
        gate = self.source.index('if halt and halt.get("halted")')
        self.assertGreater(gate, 0)

    def test_stall_alarm_is_independent_of_the_temperature_alarm(self):
        # 正是这两条互相依赖才产生了那一度的盲区：停工线低于告警线 ⇒ 静默停机。
        # 只看停工告警**自己的判据**（取判决 → 置位那一段）。两个告警在源码里前后
        # 相邻，把中间的温度检查圈进来就成了无意义的断言。
        start = self.source.index("halt = check_refinery_halt()")
        end = self.source.index('new_anomalies["refinery_stalled"] = True')
        condition = self.source[start:end]
        self.assertNotIn("DISK_TEMP_WARN", condition)
        self.assertNotIn("check_disk_temp", condition)
        # 反过来也要成立：温度告警不依赖停工判决。
        temp_at = self.source.index("hottest, temp_msg = check_disk_temp()")
        temp_block = self.source[temp_at:temp_at + 400]
        self.assertNotIn("refinery_stalled", temp_block)


if __name__ == "__main__":
    unittest.main()
