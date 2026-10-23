"""入库闸的正文长度：汉字按 cjk_char_weight 个字符计。

2026-10-24 抽样（近 30 天 223 条，子代理盲标高 / 中 / 低价值）：被现状挡住的 6 条高价值观测里有几条是中文写的、
分数差一点（如 59.45 < 60）。一个汉字的信息量约等于两个英文字符，按字符数计分对中文吃亏。同一样本上折算为 2 后
高价值保留 94%→95%、中价值 92%→93%，低价值不多放一条。

钉住：打分、最短正文门槛、长正文豁免三处用同一个有效长度；英文正文不受影响；线上配置的系数是 2。
"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.ingest_filter import compute_score, evaluate, narrative_length  # noqa: E402

CFG = json.loads((Path(__file__).resolve().parent.parent / "config" / "ingest_filter.json").read_text("utf-8"))


def cfg_with(weight):
    c = copy.deepcopy(CFG)
    c["scoring"]["cjk_char_weight"] = weight
    return c


def obs(narrative, type_="discovery", facts=("事实一", "事实二"), files=("a.py",)):
    return {"id": 1, "type": type_, "narrative": narrative, "facts": json.dumps(list(facts), ensure_ascii=False),
            "files_modified": json.dumps(list(files)), "concepts": json.dumps(["x"]), "platform_source": "claude",
            "project": "sd-server", "relevance_count": 0, "generated_by_model": "deepseek-v4-flash-0731"}


class NarrativeLengthTests(unittest.TestCase):
    def test_counts_cjk_by_weight_and_leaves_others_alone(self):
        self.assertEqual(narrative_length("abc 123", 2), 7)
        self.assertEqual(narrative_length("快照保留时长", 2), 12)
        self.assertEqual(narrative_length("TTL 改为 10 分钟", 2), 12 + 4)        # 12 个字符里 4 个汉字各多算 1
        self.assertEqual(narrative_length("㐀丌", 2), 4)                          # 扩展 A 区也算汉字
        self.assertEqual(narrative_length("快照保留时长", 1), 6)
        self.assertEqual(narrative_length("", 2), 0)
        self.assertEqual(narrative_length(None, 2), 0)

    def test_live_config_uses_weight_two(self):
        self.assertEqual(CFG["scoring"]["cjk_char_weight"], 2)


class GateTests(unittest.TestCase):
    def test_score_counts_chinese_narrative_double(self):
        zh = "排查发现详情页把用户整段描述存成了触发词" * 5                       # 20 字 × 5 = 100 个汉字
        s1, b1 = compute_score(obs(zh), cfg_with(1)["scoring"])
        s2, b2 = compute_score(obs(zh), cfg_with(2)["scoring"])
        self.assertEqual((b1["narrative"], b2["narrative"]), (5.0, 10.0))
        self.assertEqual(round(s2 - s1, 2), 5.0)

    def test_english_narrative_score_unchanged(self):
        en = "The completed snapshot TTL was restored to one hour because the memory pressure was transient. " * 3
        self.assertEqual(compute_score(obs(en), cfg_with(1)["scoring"]), compute_score(obs(en), cfg_with(2)["scoring"]))

    def test_min_narrative_gate_uses_weighted_length(self):
        zh = "触发词存的是用户整段描述而非模型标签" * 3                              # 54 个汉字 < 80 字符，折算后 108
        d1 = evaluate(obs(zh, type_="decision"), cfg_with(1), None)
        d2 = evaluate(obs(zh, type_="decision"), cfg_with(2), None)
        self.assertFalse(d1.accept)
        self.assertTrue(any("narrative too short" in r for r in d1.reasons))
        self.assertTrue(d2.accept)

    def test_substantive_narrative_bypass_uses_weighted_length(self):
        zh = "完结快照的保留时长曾因内存压力缩短后又恢复为一小时" * 8              # 200 个汉字，折算后 400 >= 300
        no_facts = obs(zh, type_="decision", facts=(), files=())
        d1 = evaluate(no_facts, cfg_with(1), None)
        d2 = evaluate(no_facts, cfg_with(2), None)
        self.assertFalse(d1.accept)
        self.assertTrue(any("insufficient facts" in r for r in d1.reasons))
        self.assertTrue(d2.accept)

    def test_missing_weight_defaults_to_no_folding(self):
        c = copy.deepcopy(CFG)
        c["scoring"].pop("cjk_char_weight")
        zh = "触发词存的是用户整段描述而非模型标签" * 3
        self.assertFalse(evaluate(obs(zh, type_="decision"), c, None).accept)
        self.assertEqual(compute_score(obs(zh), c["scoring"]), compute_score(obs(zh), cfg_with(1)["scoring"]))


if __name__ == "__main__":
    unittest.main()
