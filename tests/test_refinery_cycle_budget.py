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


if __name__ == "__main__":
    unittest.main()
