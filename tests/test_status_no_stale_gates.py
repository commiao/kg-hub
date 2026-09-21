"""新鲜的时间戳不许盖着旧结论。

`write_status` 是 `cur.update(kw)`，而写状态有四条路径（人工断路 / 温度歇工 /
窗口外 / 正常一轮），每条只带一部分字段。没带到的那些就被一个**新鲜的 ts**
继续对外播 —— 准则 9 第三条那个形态，而且它已经骗过人了：

    upstream_error_paused  昨夜窗口内因网关 5xx 置 True，窗口一关就没人再刷它，
                           于是 refinery 明明空闲，拓扑 gates 里仍列着
                           「上游 5xx 停发」
    live_processed         停在上一个容器留下的 deferred=200 / halted=143，
                           而 live_per_cycle 已经是 30 —— 这组数在当前配置下
                           不可能发生

同一个病在 `per_cycle` 上补过一次（「三条写状态的路径都要带」），补完又在这两个
字段上复发。所以这里钉的不是「记得写」，而是**默认值只有一份**：以后新增闸门
只要进那张表，四条路径自动都对。

夹具刻意先落一份**上一轮/上一个容器写下的** status.json —— 缺了这一样，
这套测试就永远绿（准则 30）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kg_refinery as refinery  # noqa: E402
import topology  # noqa: E402

# 线上真实的陈旧快照（2026-09-21 12:00 实测）。
STALE = {
    "ts": "2026-09-20T17:47:36+00:00",
    "upstream_error_paused": True,
    "rate_limited": False,
    "quota_paused": False,
    "breaker_open": False,
    "breaker_reason": "",
    "thermal_hold": False,
    "idle_outside_window": False,
    "live_processed": {"ingested": 0, "deferred": 200,
                       "result_counts": {"409": 55, "halted": 143}},
    "backlog_processed": {"ingested": 12, "deferred": 58},
    # 非「此刻」类字段：它们是累计量/配置，必须被保留
    "backlog_remaining": 7357,
    "per_cycle": 70,
    "live_per_cycle": 30,
    "watermark": {"ingested": 8204, "rejected": 5783, "failed": 0},
    "budget_today": {"day": "2026-09-20", "lines": {"live": {"ingested": 31}}},
}


class _Bed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self._orig = (refinery.STATE_DIR, refinery.STATUS)
        refinery.STATE_DIR, refinery.STATUS = d, d / "status.json"
        refinery.STATUS.write_text(json.dumps(STALE), "utf-8")

    def tearDown(self):
        refinery.STATE_DIR, refinery.STATUS = self._orig

    def read(self) -> dict:
        return json.loads(refinery.STATUS.read_text())

    def idle_write(self):
        """窗口外那条路径原样的参数（kg_refinery 主循环里那一处）。"""
        refinery.write_status(disk_temp=48, thermal_hold=False, thermal={"holds": 0},
                              backlog_window_open=False, idle_outside_window=True,
                              last_error=None, **refinery.cycle_budget_fields())


class StaleGatesTests(_Bed):
    def test_idle_path_clears_a_gate_it_never_mentions(self):
        """窗口外那条路径压根没提 upstream_error_paused，它也必须归位。"""
        self.assertTrue(self.read()["upstream_error_paused"])   # 夹具确实是脏的
        self.idle_write()
        self.assertFalse(self.read()["upstream_error_paused"])

    def test_idle_path_clears_last_cycles_batch_readings(self):
        """本轮没跑批，就不该留着上一轮的条数。"""
        self.idle_write()
        d = self.read()
        self.assertIsNone(d["live_processed"])
        self.assertIsNone(d["backlog_processed"])

    def test_cumulative_fields_survive(self):
        """复位只针对「此刻」类字段；累计量和配置被抹掉会造成另一种假话。"""
        self.idle_write()
        d = self.read()
        self.assertEqual(d["backlog_remaining"], 7357)
        self.assertEqual(d["watermark"]["ingested"], 8204)
        self.assertEqual(d["budget_today"]["day"], "2026-09-20")
        # per_cycle 不属于「保留」那一类：它由 cycle_budget_fields() 每次显式写成
        # 进程里的现值。2026-09-20 修过的就是它停在重启前的旧值那个毛病。
        self.assertEqual(d["per_cycle"], refinery.BACKLOG_PER_CYCLE)

    def test_a_fresh_ts_never_carries_a_stale_gate(self):
        """这就是那句要钉死的话：ts 前进了，闸门就必须是本轮的结论。"""
        self.idle_write()
        d = self.read()
        self.assertNotEqual(d["ts"], STALE["ts"])
        self.assertEqual(topology.refinery_halt(d)["gates"], [],
                         "空闲时仍报出闸门——看板和人读到的就是假的")

    def test_heartbeat_only_must_not_reset_anything(self):
        """心跳只证明进程活着，不推进 ts，也就无权改写任何结论。"""
        refinery.write_status(heartbeat_only=True)
        d = self.read()
        self.assertEqual(d["ts"], STALE["ts"])
        self.assertTrue(d["upstream_error_paused"])
        self.assertEqual(d["live_processed"]["deferred"], 200)

    def test_thermal_hold_outside_the_window_keeps_both_facts(self):
        """温度门控排在窗口门控前面。两件事同时成立时，复位不许抹掉窗口那件。"""
        refinery.write_status(disk_temp=60, thermal_hold=True, thermal={"holds": 1},
                              backlog_window_open=False, idle_outside_window=True,
                              last_error=None, **refinery.cycle_budget_fields())
        d = self.read()
        self.assertTrue(d["thermal_hold"])
        self.assertTrue(d["idle_outside_window"])


class CouplingTests(unittest.TestCase):
    """下一个闸门不许再漏。"""

    def test_every_halt_gate_has_a_default(self):
        """topology 读的每一个闸门字段，都必须在那张默认值表里。

        这条是这套测试的主力：它保证「新增闸门」这件事不需要谁记得去改四条路径。
        """
        missing = [k for k, _ in topology._HALT_GATES
                   if k not in refinery._MOMENTARY_DEFAULTS]
        self.assertEqual(missing, [],
                         f"这些闸门没有默认值，窗口外会一直亮着：{missing}")

    def test_defaults_are_falsy_so_absent_means_not_gated(self):
        """默认值必须是「没有这回事」，不能是 True/非空 —— 否则复位本身就在造假。"""
        for k, v in refinery._MOMENTARY_DEFAULTS.items():
            self.assertFalse(v, f"{k} 的默认值不是假值：{v!r}")

    def test_thermal_path_declares_the_window(self):
        """温度那条路径必须显式带窗口，否则被复位抹成 False。"""
        src = (ROOT / "kg_refinery.py").read_text("utf-8")
        block = src.split("thermal_hold=True", 1)[1][:400]
        self.assertIn("idle_outside_window=not in_backlog_window()", block)


if __name__ == "__main__":
    unittest.main()
