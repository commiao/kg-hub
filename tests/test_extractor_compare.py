"""extractor_compare：按 extractor_model / legacy 月份分组的并排指标，口径与报告第 3 节一致。"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import unittest

from tools import extractor_compare as xc


class AggregateTest(unittest.TestCase):
    def test_groups_and_metrics(self):
        eps = [{"src": "legacy:2026-10", "eps": 2, "mentions": 20, "edges": 18, "zero_entities": 0},
               {"src": "claude-haiku-5-5", "eps": 4, "mentions": 24, "edges": 16, "zero_entities": 1}]
        edges = [{"src": "legacy:2026-10", "name": "binds", "count": 10, "dups": 2},
                 {"src": "legacy:2026-10", "name": "belongs_to", "count": 8, "dups": 0},
                 {"src": "claude-haiku-5-5", "name": "relates_to", "count": 16, "dups": 0}]
        ents = [{"src": "legacy:2026-10", "name": "commit 94a9779", "summary_len": 10, "l": ["Entity", "Tool"]},
                {"src": "legacy:2026-10", "name": "kg-hub", "summary_len": 90, "l": ["Entity"]},
                {"src": "claude-haiku-5-5", "name": "FalkorDB", "summary_len": 60, "l": ["Entity", "Tool"]}]
        t = xc.aggregate(eps, edges, ents)
        lg, hk = t["legacy:2026-10"], t["claude-haiku-5-5"]
        self.assertEqual((lg["entities_per_episode"], lg["edges_per_episode"]), (10.0, 9.0))
        self.assertAlmostEqual(lg["edge_in_schema"], 8 / 18, places=3)
        self.assertAlmostEqual(lg["dup_triple_share"], 2 / 18, places=3)
        self.assertEqual((lg["untyped_share"], lg["dirty_name_share"], lg["summary_len_avg"]), (0.5, 0.5, 50.0))
        self.assertEqual((hk["edge_in_schema"], hk["zero_entity_share"], hk["edge_distinct_names"]), (1.0, 0.25, 1))
        md = xc.render(t)
        self.assertTrue(md.splitlines()[2].startswith("| legacy:2026-10 |"))
        self.assertIn("| claude-haiku-5-5 | 4 | 6.0 | 4.0 | 25.0% | — | 100.0% | 1 |", md)



class GroupByTest(unittest.TestCase):
    """框架 §8-1：按 extractor_tool / run_id 分组；旧内容没有这两个字段 → unknown，不回落月份。"""

    def test_source_expr_and_queries(self):
        self.assertIn("coalesce(e.extractor_model, 'legacy:'", xc.source_expr("e"))
        self.assertEqual(xc.source_expr("r", "extractor_tool"), "coalesce(r.extractor_tool, 'unknown')")
        self.assertEqual(xc.source_expr("n", "run_id"), "coalesce(n.run_id, 'unknown')")
        with self.assertRaises(ValueError):
            xc.source_expr("e", "created_at")
        ep, edges, ents = xc.queries("run_id")
        for q in (ep, edges, ents):
            self.assertIn("run_id, 'unknown') AS src", q)
            self.assertNotIn("legacy:", q)
        self.assertIn("AS zero_edges", ep)
        self.assertEqual(xc.queries("extractor_model"), (xc._EP, xc._EDGES, xc._ENTS))

    def test_collect_passes_group_by_queries_to_driver(self):
        class D:
            def __init__(self):
                self.seen = []

            async def execute_query(self, cy, **p):
                self.seen.append((cy, p))
                return [], None, None
        d = D()
        import asyncio
        t = asyncio.run(xc.collect(d, "kg_hub", "extractor_tool"))
        self.assertEqual(t, {})
        self.assertEqual(len(d.seen), 3)
        self.assertTrue(all("extractor_tool, 'unknown') AS src" in cy and p == {"g": "kg_hub"} for cy, p in d.seen))

    def test_zero_edge_share_and_render_by_tool(self):
        eps = [{"src": "unknown", "eps": 4, "mentions": 8, "edges": 6, "zero_entities": 0, "zero_edges": 1},
               {"src": "codex", "eps": 2, "mentions": 10, "edges": 9, "zero_entities": 0}]   # 旧 json 没有 zero_edges
        t = xc.aggregate(eps, [], [])
        self.assertEqual(t["unknown"]["zero_edge_share"], 0.25)
        self.assertIsNone(t["codex"]["zero_edge_share"])
        md = xc.render(t, "extractor_tool")
        lines = md.splitlines()
        self.assertTrue(lines[0].startswith("| 执行器 | episodes | 实体/条 | 边/条 | 零实体 | 零边 |"))
        self.assertTrue(lines[2].startswith("| unknown | 4 | 2.0 | 1.5 | 0.0% | 25.0% |"))   # 没打标的排前面
        self.assertTrue(lines[3].startswith("| codex | 2 | 5.0 | 4.5 | 0.0% | — |"))
        self.assertTrue(xc.render(t, "run_id").startswith("| run |"))

    def test_gateway_single_is_its_own_group(self):
        """网关单次抽取与 graphiti 归一后的内容共用 extractor_model 标签，必须按 extraction_mode 分开。"""
        for cypher in (xc._EP, xc._EDGES, xc._ENTS):
            self.assertIn("extraction_mode = 'gateway_single'", cypher)
            self.assertIn("'gateway_single:' + coalesce(", cypher)
        src = "gateway_single:gateway:kg_hub.entity_extract"
        t = xc.aggregate(
            [{"src": "gateway:kg_hub.entity_extract", "eps": 2, "mentions": 18, "edges": 13, "zero_entities": 0},
             {"src": src, "eps": 2, "mentions": 11, "edges": 8, "zero_entities": 0}],
            [{"src": src, "name": "depends_on", "count": 8, "dups": 0}],
            [{"src": src, "name": "FalkorDB", "summary_len": 40, "l": ["Entity", "Tool"]}])
        self.assertEqual((t[src]["edges_per_episode"], t[src]["edge_in_schema"]), (4.0, 1.0))
        self.assertEqual(t["gateway:kg_hub.entity_extract"]["edges_per_episode"], 6.5)
        rows = [l.split(" | ")[0].strip("| ") for l in xc.render(t).splitlines()[2:]]
        self.assertEqual(rows, ["gateway:kg_hub.entity_extract", src])


if __name__ == "__main__":
    unittest.main()
