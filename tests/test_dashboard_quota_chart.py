"""「模型用量与成本」页上的逐小时堆叠柱的回归测试。

用户 2026-09-20 的要求:按业务 key 看 5000 额度被谁占了多少,再下钻到
backlog / live。难点不在画图,在**两个口径不能混算**:网关记的是模型调用次数,
refinery 记的是观测条数,实测差约 20 倍(一条观测要抽实体/关系/去重/摘要)。
把两者堆在同一根柱子上、或者共用一个"单位"标签,就会产出一个看起来精确的假比例。
"""
from __future__ import annotations

import re
import sys as _sys
from pathlib import Path
import unittest

_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "kg_hub_server.py").read_text("utf-8")
TEMPLATE = SERVER[SERVER.index('_DASH_GATEWAY_USAGE_HTML = """'):
                 SERVER.index('</script></body></html>"""',
                              SERVER.index('_DASH_GATEWAY_USAGE_HTML = """'))]
HANDLER = SERVER.split("async def dashboard_gateway_usage", 1)[1].split("\nasync def ", 1)[0]


class PayloadTests(unittest.TestCase):
    def test_handler_ships_the_three_new_fields(self):
        """图要画出来,三样缺一不可:分母、去向账、账的日期。"""
        for field in ('"limits"', '"budget_hourly"', '"budget_day"'):
            self.assertIn(field, HANDLER, f"payload 缺 {field}")

    def test_denominator_is_effective_limits_not_approval_ceiling(self):
        """占比的分母必须是当前有效额度。

        审批上限(ceilings)不是正在执行的那一个 —— 拿它当分母会把"已用 100%"
        显示成"已用 4%"。
        """
        self.assertIn("effective_limits", HANDLER)
        seg = HANDLER.split('data["limits"]', 1)[1][:300]
        self.assertNotIn("ceilings", seg)
        self.assertIn("daily_requests", seg)

    def test_drilldown_reads_the_refinery_not_the_gateway(self):
        """backlog / live 的拆分只有 refinery 知道,网关那边只看得到一个 key。"""
        seg = HANDLER.split('budget = rstatus.get("budget_today")', 1)
        self.assertEqual(len(seg), 2, "下钻数据必须取自 refinery status")


class ChartTemplateTests(unittest.TestCase):
    def test_two_series_declare_different_units(self):
        """核心约束:换维度时单位跟着换,而且都写在页面上。"""
        by_key = TEMPLATE.split("function seriesByKey", 1)[1].split("function seriesByLine", 1)[0]
        by_line = TEMPLATE.split("function seriesByLine", 1)[1].split("let MODE", 1)[0]
        self.assertIn("调用次数", by_key)
        self.assertIn("观测条数", by_line)
        unit = by_line.split("unit:", 1)[1].split("',", 1)[0].lstrip("'")
        self.assertTrue(unit.startswith("观测条数"),
                        f"下钻那一档的单位必须先声明自己是观测条数,实际是 {unit!r}")
        self.assertIn("不是调用次数", unit,
                      "还要明说它和上面那张图不是一回事——否则看的人会直接相减")
        # 单位必须真的被渲染出来,而不是只写在注释里
        self.assertIn("chartUnit", TEMPLATE)
        self.assertIn("'单位：'+S.unit", TEMPLATE)

    def test_drilldown_warns_the_two_counts_are_not_comparable(self):
        by_line = TEMPLATE.split("function seriesByLine", 1)[1].split("let MODE", 1)[0]
        self.assertTrue(re.search(r"不是调用次数", by_line),
                        "下钻单位说明必须点明它与上面那张图不可直接比较")

    def test_quota_share_only_shown_where_the_denominator_applies(self):
        """额度占比只在按 key 那一档给 —— 观测条数没有"占 5000 的百分之几"这回事。"""
        legend = TEMPLATE.split("S.keys.forEach(function(k,ki){", 1)
        legend = TEMPLATE.split("lg.append(sp)", 1)[0]
        self.assertIn("if(MODE==='key'){const cap=", legend)

    def test_quota_share_numerator_is_same_day_only(self):
        """占比的分子分母必须落在同一个 UTC 配额日。

        2026-09-20 第一版就栽在这里:柱子画近 24 小时、会跨两个配额日,分子拿
        跨日合计、分母拿日上限,页面上显示成「10000 / 5000（200%）」。
        额度按 UTC 日重置,跨日合计除以日上限没有任何意义。
        """
        legend = TEMPLATE.split("S.keys.forEach((k,ki)=>", 1)[1].split("lg.append(sp)", 1)[0]
        self.assertIn("dayOf(h)===today", legend,
                      "分子必须先按当日过滤,不能直接用 24h 合计")
        share = legend.split("cap?", 1)[1][:120]
        self.assertIn("dayTot", share, "百分比要用当日合计算,不是 tot")
        self.assertNotIn("tot*100/cap", legend, "不许再用跨日合计算占比")

    def test_the_bold_number_says_what_period_it_covers(self):
        """粗体那个数字是 24h 合计,不是当日 —— 页面上要说清楚,否则又是一个混淆源。"""
        self.assertIn("近 24h 合计", TEMPLATE)
        self.assertIn("近 24 小时", TEMPLATE)

    def test_missing_data_says_why_instead_of_drawing_an_empty_chart(self):
        self.assertIn("暂无去向账", TEMPLATE)
        self.assertIn("暂无用量快照", TEMPLATE)

    def test_labels_are_written_as_text_not_html(self):
        """business_key 来自见证库;看板不该是第二道信任边界(沿用本页既有约定)。"""
        self.assertNotIn("innerHTML", TEMPLATE)


if __name__ == "__main__":
    unittest.main()
