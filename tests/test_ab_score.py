"""ab_score：A/B 并排评分必须与复盘口径一致、能从账本索引取每条调用数、并给出可核验的最优列。

保证：路由解析拒绝重复/残缺；图库加载按 group_id 过滤且形状与 digest_retro.graph_metrics 吻合；
账本只按 (source_description, source_obs_id, business_key) 取 completed 调用；渲染表每行一个最优，
并列或无方向行留空；CLI 缺 --sample 时拒绝 --journal。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools import ab_score as ab


class FakeDriver:
    """按 cypher 里的标签分发三条只读查询，并记录传入的 group_id。"""

    def __init__(self, eps, edges, ents):
        self.eps, self.edges, self.ents = eps, edges, ents
        self.groups = []

    async def execute_query(self, cy, **p):
        self.groups.append(p.get("g"))
        if "MATCH (e:Episodic)" in cy:
            rows = self.eps
        elif "MATCH (a)-[r:RELATES_TO]->(b)" in cy:
            rows = self.edges
        elif "MATCH (n:Entity)" in cy:
            rows = self.ents
        else:
            raise AssertionError(cy)
        assert "=~" not in cy  # FalkorDB 不支持正则
        return rows, None, None


def _journal(path: Path, rows):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE model_attempts (idempotency_key TEXT PRIMARY KEY, business_key TEXT,
        source_description TEXT, source_obs_id TEXT, step_id TEXT, request_digest TEXT, phase TEXT,
        provider_call_started INTEGER, result_json TEXT, gateway_identity TEXT, gateway_http_status INTEGER,
        http_started_at TEXT, stage TEXT, created_at TEXT, updated_at TEXT)""")
    for i, (bk, sd, sid, phase, stage, a, b) in enumerate(rows):
        db.execute("INSERT INTO model_attempts (idempotency_key, business_key, source_description, source_obs_id, "
                   "step_id, request_digest, phase, stage, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (f"k{i}", bk, sd, sid, f"s{i}", "d", phase, stage, a, b))
    db.commit(); db.close()


class ParseTest(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(ab.parse_pairs(["a=kg_hub_ab_a", "b = kg_hub_ab_b "], "route"),
                         {"a": "kg_hub_ab_a", "b": "kg_hub_ab_b"})
        for bad in (["a"], ["=x"], ["a="], ["a=1", "a=2"]):
            with self.assertRaises(ValueError):
                ab.parse_pairs(bad, "route")


class GraphTest(unittest.TestCase):
    def test_load_group_graph_filters_by_group_and_matches_metric_shape(self):
        drv = FakeDriver(
            eps=[{"u": "e1", "entities": 10, "edges": 8}, {"u": "e2", "entities": 4, "edges": 0}],
            edges=[{"name": "belongs_to", "count": 5, "dups": 1}, {"name": "binds", "count": 3, "dups": 0}],
            ents=[{"name": "sd-server", "summary_len": 100, "l": ["Entity", "Tool"]},
                  {"name": "commit 94a9779", "summary_len": 10, "l": ["Entity"]}])
        eps, edges, ents = asyncio.run(ab.load_group_graph(drv, "kg_hub_ab_x"))
        self.assertEqual(set(drv.groups), {"kg_hub_ab_x"})
        self.assertEqual(len(drv.groups), 3)
        self.assertEqual(ents[0]["typed"], True)
        self.assertEqual(ents[1]["typed"], False)
        m = ab.score_route(eps, edges, ents, expected=4)
        self.assertEqual(m["coverage"], 0.5)
        self.assertAlmostEqual(m["edge_in_schema"], 5 / 8)
        self.assertEqual(m["dirty_name_share"], 0.5)
        self.assertEqual(m["offschema_top"], [("binds", 3)])

    def test_offschema_sorted_and_capped(self):
        edges = [{"name": "relates_to", "count": 9}, {"name": "exposes", "count": 2},
                 {"name": "binds", "count": 2}, {"name": "unreachable_at", "count": 5}]
        self.assertEqual(ab.offschema_edges(edges, top=2), [("unreachable_at", 5), ("binds", 2)])


class JournalTest(unittest.TestCase):
    def test_calls_wall_and_stage_per_business_key(self):
        sd, sid = "claude-mem obs id=1 project=p type=t platform=claude", "hash1"
        with tempfile.TemporaryDirectory() as d:
            j = Path(d) / "j.sqlite3"
            _journal(j, [
                ("kg_hub.entity_extract", sd, sid, "completed", "node_extraction",
                 "2026-10-18T00:00:00+00:00", "2026-10-18T00:00:30+00:00"),
                ("kg_hub.entity_extract", sd, sid, "completed", "edge_phase",
                 "2026-10-18T00:00:30+00:00", "2026-10-18T00:01:00+00:00"),
                ("kg_hub.entity_extract", sd, sid, "prepared", "edge_phase",  # 未完成的不算
                 "2026-10-18T00:01:00+00:00", "2026-10-18T00:01:00+00:00"),
                ("kg_hub.entity_extract_ab", sd, sid, "completed", "node_extraction",  # 另一条路由
                 "2026-10-18T00:00:00+00:00", "2026-10-18T00:05:00+00:00"),
            ])
            items = [{"source_description": sd, "source_obs_id": sid},
                     {"source_description": "none", "source_obs_id": "none"}]
            m = ab.journal_route_metrics(j, "kg_hub.entity_extract", items)
            self.assertEqual(m["obs_with_calls"], 1)
            self.assertEqual(m["calls_mean"], 2.0)
            self.assertEqual(m["wall_med_s"], 60.0)
            self.assertEqual(m["stage_share"], {"edge_phase": 0.5, "node_extraction": 0.5})
            m2 = ab.journal_route_metrics(j, "kg_hub.entity_extract_ab", items)
            self.assertEqual((m2["calls_mean"], m2["wall_med_s"]), (1.0, 300.0))
            self.assertIsNone(ab.journal_route_metrics(j, "kg_hub.nothing", items)["calls_mean"])


class RenderTest(unittest.TestCase):
    def test_best_label_direction_and_ties(self):
        self.assertEqual(ab.best_label("edge_in_schema", "higher", {"a": 0.3, "b": 0.6}), "b")
        self.assertEqual(ab.best_label("wall_med_s", "lower", {"a": 100, "b": 250}), "a")
        self.assertIsNone(ab.best_label("x", "lower", {"a": 1, "b": 1}))
        self.assertIsNone(ab.best_label("x", None, {"a": 1, "b": 2}))
        self.assertIsNone(ab.best_label("x", "higher", {"a": 1, "b": None}))

    def test_table_side_by_side(self):
        routes = {
            "deepseek": {"coverage": 1.0, "edge_in_schema": 0.36, "edge_distinct_names": 40, "wall_med_s": 271,
                         "episodes": 50, "entities": 600, "edges": 490, "offschema_top": [("binds", 7)],
                         "stage_share": {"edge_phase": 0.4, "node_extraction": 0.6}},
            "claude": {"coverage": 0.98, "edge_in_schema": 0.8, "edge_distinct_names": 14, "wall_med_s": 200,
                       "episodes": 49, "entities": 500, "edges": 400, "offschema_top": []},
        }
        md = ab.render_table(routes)
        self.assertIn("| 指标 | deepseek | claude | 最优 |", md)
        self.assertIn("| 边名合规率 | 36.0% | 80.0% | claude |", md)
        self.assertIn("| 覆盖率（入图/样本） | 100.0% | 98.0% | deepseek |", md)
        self.assertIn("| 实体/条 | — | — | — |", md)
        self.assertIn("`binds`×7", md)
        self.assertIn("edge_phase 40%", md)
        self.assertIn("## claude", md)


class CliTest(unittest.TestCase):
    def test_journal_requires_sample(self):
        with self.assertRaises(SystemExit):
            ab.main(["--route", "a=kg_hub_ab_a", "--journal", "/nonexistent.sqlite3"])

    def test_bad_route_rejected_before_touching_graph(self):
        with self.assertRaises(SystemExit):
            ab.main(["--route", "broken"])


if __name__ == "__main__":
    unittest.main()
