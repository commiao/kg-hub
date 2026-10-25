"""utils/extraction_structure：结构四指标（恰 2 实体、端点对/条、其余 relates_to、跨观测复用）与「结构过薄」提醒。

两组构造数据：模板化（每条恰 2 个实体、只有一个端点对、其余边全是 relates_to、实体不跨观测）对照
非模板化（具体对象成节点、多端点对、实体跨观测复用）。数据全是合成的。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import unittest

from utils import extraction_structure as xs


def ent(name, typ="Concept"):
    return {"name": name, "type": typ, "summary": "合成实体，用于结构指标测试"}


def edge(s, t, name="relates_to", fact="f"):
    return {"source": s, "target": t, "name": name, "fact": fact}


def templated(i):
    """两节点模板：标题、译文、再一条 relates_to，全在同一对端点上。"""
    x, y = f"模板甲{i}", f"模板乙{i}"
    return {"id": i, "entities": [ent(x), ent(y, "Tool")],
            "edges": [edge(x, y), edge(x, y), edge(x, y, fact="x relates to y in the observation")]}


F = "tools/extract_campaign.py"
RICH = {
    1: {"id": 1, "entities": [ent("kg-hub", "Project"), ent(F, "File"), ent("fix-1", "Fix"), ent("cfg-1", "Config")],
        "edges": [edge("kg-hub", F), edge("kg-hub", F), edge(F, "kg-hub", "belongs_to"),
                  edge("fix-1", F), edge("cfg-1", F, "depends_on")]},
    # 大小写不同仍是同一个实体（跨观测复用按忽略大小写算）
    2: {"id": 2, "entities": [ent("KG-Hub", "Project"), ent(F, "File"), ent("fix-2", "Fix")],
        "edges": [edge("fix-2", F), edge("fix-2", F), edge("fix-2", "KG-Hub", "belongs_to"),
                  edge(F, "KG-Hub", "belongs_to")]},
    3: {"id": 3, "entities": [ent("kg-hub", "Project"), ent("lonely")],
        "edges": [edge("lonely", "kg-hub"), edge("lonely", "kg-hub")]},
    4: {"id": 4, "entities": [], "edges": []},     # 空结果：不进任何分母
}

BODY_TWO_OBJECTS = "[DISCOVERY] 合成观测\n改了 `tools/extract_campaign.py`，提交 3d6f574 修好了。"


def report(items):
    ms = [xs.structure_metrics(it) for it in items]
    row = xs.structure_aggregate(ms)
    keysets = [k for k in (xs.entity_keys(it) for it in items) if k]
    row["reuse_pct"] = xs.reuse_pct(*xs.reuse_counts(keysets, xs.count_obs(keysets)))
    return row


class StructureMetricsTest(unittest.TestCase):
    def test_templated_results(self):
        row = report([templated(i) for i in (1, 2, 3)])
        self.assertEqual((row["two_entity_pct"], row["pairs_per_obs"], row["rest_relates_to_pct"], row["reuse_pct"]),
                         (100.0, 1.0, 100.0, 0.0))

    def test_rich_results(self):
        row = report(list(RICH.values()))
        # 恰 2 实体：只有第 3 条（1/3）；端点对 (4+3+1)/3；其余边 3+2+0 条里 relates_to 1 条；
        # 复用：9 次实体出现里 kg-hub 3 次 + 文件 2 次在 ≥2 条观测出现
        self.assertEqual((row["two_entity_pct"], row["pairs_per_obs"], row["rest_relates_to_pct"], row["reuse_pct"]),
                         (33.3, 2.67, 20.0, 55.6))
        self.assertEqual(row["_struct_n"], 3)

    def test_empty_denominators_are_none(self):
        self.assertEqual(report([RICH[4], None]),
                         {"_struct_n": 0, "_struct_two": 0, "_struct_pairs": 0, "_struct_rest": 0, "_struct_rel": 0,
                          "two_entity_pct": None, "pairs_per_obs": None, "rest_relates_to_pct": None, "reuse_pct": None})
        # 只有标题与译文两条边：没有「其余边」，relates_to 占比给 None 而不是 0
        self.assertIsNone(report([RICH[3]])["rest_relates_to_pct"])

    def test_merge_recomputes_from_counts(self):
        a = xs.structure_aggregate([xs.structure_metrics(templated(i)) for i in (1, 2, 3)])
        b = xs.structure_aggregate([xs.structure_metrics(it) for it in RICH.values()])
        m = xs.merge_structure([a, b])
        self.assertEqual(m, {"two_entity_pct": round(100 * 4 / 6, 1), "pairs_per_obs": round(11 / 6, 2),
                             "rest_relates_to_pct": round(100 * 4 / 8, 1)})

    def test_reuse_counts_against_a_wider_pool(self):
        # 参照池比被统计的观测多：被统计观测里的名字只要在池里出现于 ≥2 条观测就算复用
        own, later = [{"a", "b"}], [{"a"}]
        self.assertEqual(xs.reuse_counts(own, xs.count_obs(own)), (0, 2))
        self.assertEqual(xs.reuse_counts(own, xs.count_obs(own + later)), (1, 2))


class ThinStructureWarningTest(unittest.TestCase):
    def test_named_objects_are_conservative(self):
        self.assertEqual(xs.named_objects(BODY_TWO_OBJECTS + " 配置键 `KG_HUB_RUN_ID`；另见 utils/x.py 与 README.md"),
                         [F, "3d6f574", "KG_HUB_RUN_ID", "utils/x.py", "README.md"])
        # 比例、框架名、UUID 分段、纯数字、无数字的十六进制单词、带空格的代码片段、URL 里的路径都不算
        self.assertEqual(xs.named_objects("1.7/2.0；Node.js；a1b2c3d4-e5f6-4a7b-8c9d-0123456789ab；20261025；"
                                          "deadbeef；`a b`；https://example.com/docs/guide.html"), [])
        # 反引号里的路径与裸写同一路径、路径与它末尾的文件名只算一个
        self.assertEqual(xs.named_objects(f"`{F}` 即 {F}，也就是 extract_campaign.py"), [F])

    def test_warns_on_templated_but_not_on_rich(self):
        rows = [(1, "t", "b1"), (2, "t", "b1")]
        res = {1: templated(1), 2: RICH[1]}
        warns = xs.thin_structure_warnings(rows, res, {1: BODY_TWO_OBJECTS, 2: BODY_TWO_OBJECTS})
        self.assertEqual(len(warns), 1)
        self.assertTrue(warns[0].startswith("b1 1: 结构过薄"))
        self.assertIn(F, warns[0])
        self.assertIn("3d6f574", warns[0])
        self.assertIn("2 个实体、1 个端点对", warns[0])

    def test_trigger_conditions(self):
        one_obj = "[DISCOVERY] 只点名 `tools/extract_campaign.py`"
        self.assertIsNone(xs.thin_structure(one_obj, templated(1)))             # 只点名 1 个对象
        self.assertIsNone(xs.thin_structure(BODY_TWO_OBJECTS, RICH[4]))         # 空结果不提醒
        self.assertIsNone(xs.thin_structure(BODY_TWO_OBJECTS, None))
        self.assertIsNone(xs.thin_structure(BODY_TWO_OBJECTS, RICH[2]))         # 3 个实体、3 个端点对
        # 3 个实体但只有 1 个端点对：仍提醒
        three_one_pair = {"entities": [ent("a"), ent("b"), ent("c")], "edges": [edge("a", "b"), edge("a", "b")]}
        self.assertEqual(xs.thin_structure(BODY_TWO_OBJECTS, three_one_pair)["endpoint_pairs"], 1)
        # 2 个实体、两个方向两个端点对：仍提醒（实体只有 2 个）
        two_two_pairs = {"entities": [ent("a"), ent("b")], "edges": [edge("a", "b"), edge("b", "a")]}
        self.assertEqual(xs.thin_structure(BODY_TWO_OBJECTS, two_two_pairs)["entities"], 2)


if __name__ == "__main__":
    unittest.main()
