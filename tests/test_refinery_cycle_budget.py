"""每轮名额分配的回归测试。

2026-09-19 夜实测：日配额 5000 次模型调用、每条观测约 20 次 ≈ 250 条/天，而每轮
名额是 backlog 8 + live 200 = 208 条 —— 配额只够 1.2 轮。于是 10 小时窗口里真正
干活的只有头 3 分钟，积压整晚只分到 15 条（7452→7437），live 拿走 94%。
按那个速度 7437 条积压要 496 晚，**不是慢，是结构上到不了**。

根子不在数值大小，在**两条线的名额不是同一种东西**：backlog 是环境变量，live 是
源码切片里的字面量 `[:200]`。一个能调、一个不能调，比例就没人看得见、也没人能审。
这里钉住的是那件事，不是具体数字。
"""
from __future__ import annotations

import re
import sys as _sys
from pathlib import Path
import unittest

_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kg_refinery as refinery
import topology

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "kg_refinery.py").read_text("utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text("utf-8")


class CycleBudgetTests(unittest.TestCase):
    def test_both_lines_take_their_budget_from_a_knob(self):
        """live 和 backlog 都必须来自环境变量，不许有一个是写死的。"""
        for name in ("LIVE_PER_CYCLE", "BACKLOG_PER_CYCLE"):
            self.assertTrue(hasattr(refinery, name), f"{name} 不存在")
            self.assertIsInstance(getattr(refinery, name), int)
        self.assertIn('os.environ.get("KG_HUB_REFINERY_LIVE_PER_CYCLE"', SOURCE)
        self.assertIn('os.environ.get("KG_HUB_REFINERY_BACKLOG_PER_CYCLE"', SOURCE)

    def test_live_batch_is_not_sliced_by_a_literal(self):
        """`[:200]` 这类字面量切片正是当初藏住 25:1 的地方。"""
        body = SOURCE.split("live_ids = ", 1)[1][:300]
        self.assertIn("[:LIVE_PER_CYCLE]", body)
        self.assertIsNone(re.search(r"\[:\d+\]", body),
                          "live 批量不许用字面量切片——写死的数字没人审得到")

    def test_neither_line_can_starve_the_other(self):
        """默认比例必须是可解释的。

        这条不钉死 50:50，只拒绝"一条线吃掉配额、另一条永远轮不到"那种比例。
        配额按观测条数分摊，任一条线拿走四分之三以上就说明另一条已经没有意义。
        """
        live, backlog = refinery.LIVE_PER_CYCLE, refinery.BACKLOG_PER_CYCLE
        total = live + backlog
        self.assertGreater(total, 0)
        for name, share in (("live", live / total), ("backlog", backlog / total)):
            self.assertLessEqual(
                share, 0.75,
                f"{name} 独占 {share:.0%} 的每轮名额——另一条线会饿死（09-19 实测 live 占 94%）")

    def test_compose_passes_both_knobs(self):
        """线上取值经 compose 注入；漏掉一个就会悄悄回落到默认值。"""
        for var in ("KG_HUB_REFINERY_BACKLOG_PER_CYCLE",
                    "KG_HUB_REFINERY_LIVE_PER_CYCLE"):
            self.assertIn(var, COMPOSE, f"compose 没有注入 {var}")


class BudgetTelemetryTests(unittest.TestCase):
    """当日去向账:看板要能在窗口里看出钱烧到哪,而不是事后靠 watermark 差值反推。"""

    def setUp(self):
        refinery._budget_today.update(day="", lines={})

    def test_accumulates_across_cycles_instead_of_overwriting(self):
        refinery.note_budget("live", {"ingested": 2, "deferred": 5,
                                      "result_counts": {"halted": 5}})
        out = refinery.note_budget("live", {"ingested": 3, "deferred": 1,
                                            "result_counts": {"halted": 1, "409": 4}})
        live = out["lines"]["live"]
        self.assertEqual(live["ingested"], 5)          # 2+3,不是被后一轮覆盖成 3
        self.assertEqual(live["deferred"], 6)
        self.assertEqual(live["result_counts"], {"halted": 6, "409": 4})

    def test_lines_are_kept_apart(self):
        refinery.note_budget("live", {"ingested": 10, "rejected": 1})
        out = refinery.note_budget("backlog", {"ingested": 2})
        self.assertEqual(out["lines"]["live"]["ingested"], 10)
        self.assertEqual(out["lines"]["backlog"]["ingested"], 2)
        self.assertEqual(out["terminal_total"], 13)    # 10+1+2

    def test_day_rollover_clears_the_tally(self):
        refinery.note_budget("live", {"ingested": 9})
        refinery._budget_today["day"] = "1999-01-01"   # 假装跨日
        out = refinery.note_budget("live", {"ingested": 1})
        self.assertEqual(out["lines"]["live"]["ingested"], 1)
        self.assertNotEqual(out["day"], "1999-01-01")

    def test_unit_is_declared_and_never_silently_mixed(self):
        """网关数的是模型调用次数,这里数的是观测条数,实测差约 20 倍。

        两边口径混算会得出一个看起来精确的假比例,所以单位必须写在数据里,
        而缺任一边时看板不许给出比率。
        """
        out = refinery.note_budget("live", {"ingested": 4})
        self.assertEqual(out["unit"], "observations")

        lines, _ = topology.budget_detail({"budget_today": out}, None)
        self.assertTrue(lines, "有账就该出文案")
        self.assertFalse([l for l in lines if "调用/观测" in l],
                         "拿不到网关调用数时不许给出每条几次")

        lines, _ = topology.budget_detail({"budget_today": out}, 80)
        self.assertTrue([l for l in lines if "调用/观测 ≈ 20.0" in l],
                        "两边都有时才换算,且要把两个原始数都带出来")

    def test_no_tally_means_no_section(self):
        self.assertEqual(topology.budget_detail({}, 5000), ([], {}))
        self.assertEqual(topology.budget_detail({"budget_today": {"lines": {}}}, 5000), ([], {}))

    def test_refinery_records_each_batch_once_per_cycle(self):
        """累加必须发生在整批结束处,不能放进按条回调的 on_progress。"""
        src = SOURCE.split("snapshot(live_processed=s_live", 1)[0][-600:]
        self.assertIn('note_budget("backlog", s_back)', src)
        self.assertIn('note_budget("live", s_live)', src)
        self.assertNotIn("note_budget", SOURCE.split("on_progress=lambda st: snapshot(", 1)[1][:200])


if __name__ == "__main__":
    unittest.main()
