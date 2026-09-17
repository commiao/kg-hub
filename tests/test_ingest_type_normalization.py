"""obs.type 归一化:别再因为一对方括号丢观测。

2026-09-17 查 ingest_decisions.jsonl（18004 条决策）时发现的:hard_gate 拒掉的
254 条里,有一批的理由是 `type='[ discovery ]' not in whitelist`。同类变体还有
`'[discovery]'`、`'[decision]'`、`'[feature]'` —— 跟白名单里的 discovery /
decision / feature 是同一个类型,只因为上游模型顺手加了方括号就被整条丢掉。

obs.type 是模型写出来的自由文本,不是枚举,所以这种写法漂移是必然会发生的,
不是偶发脏数据。判定前归一化一次就够。

本测试同时钉住两头:
  * 带包裹符的变体必须被救回来(且拿到正确的 type_weight,不能只过白名单);
  * 真正不在白名单里的类型(sensitive / gotcha)必须照拒 —— 归一化不是放水。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.ingest_filter import evaluate, compute_score, normalize_type  # noqa: E402

CFG = json.loads((Path(__file__).resolve().parent.parent
                  / "config" / "ingest_filter.json").read_text("utf-8"))


def _obs(type_, **kw):
    """一条足够体面、除了 type 之外哪一关都过得去的观测。"""
    base = {
        "id": 1,
        "type": type_,
        "narrative": "这是一条足够长的叙述,用来确保它不会卡在 min_narrative_chars "
                     "这一关上,从而把失败原因唯一地留给 type 白名单判定。" * 2,
        "facts": json.dumps(["事实一", "事实二"]),
        "files_modified": json.dumps(["a.py"]),
        "concepts": json.dumps(["x"]),
        "platform_source": "claude",
        "project": "workspace_claudeCode",
        "relevance_count": 3,
        "generated_by_model": "deepseek-v4-flash-0731",
    }
    base.update(kw)
    return base


class NormalizeTypeTests(unittest.TestCase):

    def test_wrappers_and_case_are_stripped(self):
        for raw, want in (
            ("[ discovery ]", "discovery"),
            ("[discovery]", "discovery"),
            ("[decision]", "decision"),
            ("[ feature ]", "feature"),
            ("  bugfix  ", "bugfix"),
            ("Discovery", "discovery"),
            ("「change」", "change"),
            ("<refactor>", "refactor"),
            ("discovery", "discovery"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_type(raw), want)

    def test_empty_and_non_string_do_not_blow_up(self):
        for raw in (None, "", "   ", "[]", 0):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_type(raw), "")


class BracketedTypesAreRescuedTests(unittest.TestCase):

    def test_bracketed_variant_passes_the_whitelist(self):
        d = evaluate(_obs("[ discovery ]"), CFG)
        self.assertNotEqual(d.layer, "hard_gate",
                            f"带方括号的 discovery 不该被白名单拒:{d.reasons}")
        self.assertEqual(d.obs_type, "discovery")

    def test_rescued_type_also_gets_its_score_weight(self):
        """只过白名单不够:compute_score 若还读原始写法,type_weight 会是 0。

        那样的话观测换个地方再死一次 —— 白名单放行了,分数线上又被判死。
        """
        plain, _ = compute_score(_obs("decision"), CFG["scoring"])
        wrapped, _ = compute_score(_obs("[decision]"), CFG["scoring"])
        self.assertEqual(wrapped, plain, "归一化后的打分必须和规范写法完全一致")
        self.assertGreater(wrapped, 0)

    def test_raw_spelling_is_kept_in_the_decision(self):
        """归一化一上线,带方括号的变体就从 obs_type 里消失了。

        不留原始写法的话,以后回答不了"这个修到底救回多少条"。
        """
        d = evaluate(_obs("[ discovery ]"), CFG)
        self.assertEqual(d.raw_type, "[ discovery ]")

    def test_normal_spelling_leaves_no_raw_type_noise(self):
        d = evaluate(_obs("discovery"), CFG)
        self.assertIsNone(d.raw_type, "写法本来就规范的,不该背一个 raw_type")


class NormalizationIsNotAmnestyTests(unittest.TestCase):

    def test_types_genuinely_outside_the_whitelist_still_rejected(self):
        """sensitive / gotcha 确实不在白名单里 —— 那是类型策略,不归归一化管。"""
        for t in ("sensitive", "gotcha", "note", "[sensitive]"):
            with self.subTest(type=t):
                d = evaluate(_obs(t), CFG)
                self.assertEqual(d.layer, "hard_gate")
                self.assertFalse(d.would_accept)
                self.assertTrue(any("whitelist" in r for r in d.reasons))

    def test_empty_type_still_rejected(self):
        d = evaluate(_obs(""), CFG)
        self.assertEqual(d.layer, "hard_gate")
        self.assertFalse(d.would_accept)


if __name__ == "__main__":
    unittest.main()
