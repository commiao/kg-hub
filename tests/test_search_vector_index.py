"""语义边检索走 FalkorDB 向量索引（T-0345）。

graphiti 的 cosine 腿 MATCH 全部 RELATES_TO 逐条算距离，17 万条边每次 0.6–1.1 s。
改走 RELATES_TO.fact_embedding 的 HNSW 索引取候选、再用同一公式精确重排。约束：
- 默认关（KG_HUB_SEARCH_VECTOR_INDEX=1 才开）；非 FalkorDB 驱动照旧走 graphiti。
- 退休剔除与补取逻辑不变，补取时不重复算查询向量。
- FalkorDB 4.18 两条向量查询并发会返回重复边：进程内串行（与 ingest 共用一把锁），
  候选有重复判为损坏，重试；一直坏或索引不可用退回 graphiti 全扫；其他图错误照常抛出。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import graphiti_core  # noqa: F401
    HAVE_GRAPHITI = True
except ImportError:
    HAVE_GRAPHITI = False

ON = {"KG_HUB_SEARCH_VECTOR_INDEX": "1"}


def record(fact, eps=("live",), hits=100, distinct=100):
    return {"hits": hits, "distinct_hits": distinct, "uuid": f"u-{fact}",
            "source_node_uuid": "s", "target_node_uuid": "t", "group_id": "kg_hub",
            "created_at": "2026-10-01T00:00:00+00:00", "name": "REL", "fact": fact,
            "episodes": list(eps), "expired_at": None, "valid_at": None, "invalid_at": None,
            "attributes": {}}


class FakeDriver:
    """向量索引查询按排好的记录截取 $limit；archived 查询按集合回答。

    corrupt：前几次索引查询返回带重复的结果（distinct < hits）。
    error：索引查询抛的异常。delay：索引查询内 await 的秒数（测串行）。
    """

    def __init__(self, ranked, archived=(), corrupt=0, error=None, delay=0.0, provider="falkordb"):
        from graphiti_core.driver.driver import GraphProvider
        self.provider = GraphProvider.FALKORDB if provider == "falkordb" else GraphProvider.NEO4J
        self.ranked, self.archived = ranked, set(archived)
        self.corrupt, self.error, self.delay = corrupt, error, delay
        self.index_calls, self.inflight, self.max_inflight = [], 0, 0

    async def execute_query(self, cypher, **params):
        if "db.idx.vector.queryRelationships" in cypher:
            self.index_calls.append(params)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                await asyncio.sleep(self.delay)
                if self.error is not None:
                    raise self.error
                rows = [dict(r) for r in self.ranked[:params["limit"]]]
                if self.corrupt:
                    self.corrupt -= 1
                    for r in rows:
                        r["distinct_hits"] = r["hits"] - 7
                return rows, None, None
            finally:
                self.inflight -= 1
        if "ep.archived" in cypher:
            return [{"u": u, "arch": u in self.archived} for u in params["u"]], None, None
        raise AssertionError(f"unexpected query: {cypher[:80]}")


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    async def create(self, input_data):
        self.calls += 1
        return [0.1] * 4


class FakeGraphiti:
    """_search 是 graphiti 的全扫路径：记录被调用的 limit，返回固定结果。"""

    def __init__(self, driver, exact=None):
        self.driver, self.embedder = driver, FakeEmbedder()
        self.exact = exact or []
        self.exact_limits = []

    async def _search(self, query, config, **kwargs):
        self.exact_limits.append(config.limit)
        return SimpleNamespace(edges=self.exact[:config.limit])


def facts(edges):
    return [e.fact for e in edges]


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class VectorIndexSearch(unittest.TestCase):
    def search(self, g, limit=5, env=ON):
        from graphiti_client import search_edges_semantic
        with patch.dict(_os.environ, env or {}):
            if env is None:
                _os.environ.pop("KG_HUB_SEARCH_VECTOR_INDEX", None)
            return asyncio.run(search_edges_semantic(g, "q", limit))

    def test_off_by_default_uses_graphiti_scan(self):
        g = FakeGraphiti(FakeDriver([record("idx")]), exact=[SimpleNamespace(fact="scan", episodes=[])])
        self.assertEqual(facts(self.search(g, env=None)), ["scan"])
        self.assertEqual(facts(self.search(g, env={"KG_HUB_SEARCH_VECTOR_INDEX": "0"})), ["scan"])
        self.assertEqual(g.driver.index_calls, [])

    def test_non_falkordb_driver_uses_graphiti_scan(self):
        g = FakeGraphiti(FakeDriver([record("idx")], provider="neo4j"),
                         exact=[SimpleNamespace(fact="scan", episodes=[])])
        self.assertEqual(facts(self.search(g)), ["scan"])
        self.assertEqual(g.driver.index_calls, [])

    def test_index_result_keeps_rank_order_and_query_params(self):
        from graphiti_client import EDGE_VEC_ONLY_SEARCH, RETIRED_HEADROOM, SEMANTIC_CANDIDATES
        g = FakeGraphiti(FakeDriver([record(f"f{i}") for i in range(40)]))
        self.assertEqual(facts(self.search(g, 5)), [f"f{i}" for i in range(5)])
        self.assertEqual(g.exact_limits, [])
        (p,) = g.driver.index_calls
        self.assertEqual(p["limit"], 5 + RETIRED_HEADROOM)
        self.assertGreaterEqual(p["candidates"], SEMANTIC_CANDIDATES)
        self.assertEqual(p["min_score"], EDGE_VEC_ONLY_SEARCH.edge_config.sim_min_score)
        self.assertEqual(p["routing_"], "r")

    def test_candidates_scale_with_refetch(self):
        from graphiti_client import RETIRED_HEADROOM, SEMANTIC_CANDIDATES
        ranked = [record(f"r{i}", eps=["dead"]) for i in range(RETIRED_HEADROOM + 5)] + \
                 [record(f"f{i}") for i in range(100)]
        g = FakeGraphiti(FakeDriver(ranked, archived={"dead"}))
        self.assertEqual(facts(self.search(g, 60)), [f"f{i}" for i in range(60)])
        limits = [p["limit"] for p in g.driver.index_calls]
        self.assertEqual(limits, [60 + RETIRED_HEADROOM, 2 * (60 + RETIRED_HEADROOM)])
        cands = [p["candidates"] for p in g.driver.index_calls]
        self.assertEqual(cands[0], SEMANTIC_CANDIDATES)
        self.assertGreater(cands[1], cands[0])          # 补取放大了 limit，候选也跟着放大
        self.assertGreaterEqual(cands[1], 10 * limits[1])

    def test_retired_dropped_and_refetched_without_reembedding(self):
        from graphiti_client import RETIRED_HEADROOM
        n_dead = RETIRED_HEADROOM + 5
        ranked = [record(f"r{i}", eps=["dead"]) for i in range(n_dead)] + \
                 [record(f"f{i}") for i in range(30)]
        g = FakeGraphiti(FakeDriver(ranked, archived={"dead"}))
        self.assertEqual(facts(self.search(g, 5)), [f"f{i}" for i in range(5)])
        self.assertEqual([p["limit"] for p in g.driver.index_calls],
                         [5 + RETIRED_HEADROOM, 2 * (5 + RETIRED_HEADROOM)])
        self.assertEqual(g.embedder.calls, 1)
        self.assertEqual(g.exact_limits, [])

    def test_duplicates_are_retried_then_trusted_when_clean(self):
        g = FakeGraphiti(FakeDriver([record(f"f{i}") for i in range(20)], corrupt=1))
        self.assertEqual(facts(self.search(g, 3)), ["f0", "f1", "f2"])
        self.assertEqual(len(g.driver.index_calls), 2)
        self.assertEqual(g.exact_limits, [])

    def test_persistent_duplicates_fall_back_to_scan(self):
        from graphiti_client import RETIRED_HEADROOM, _INDEX_TRIES
        g = FakeGraphiti(FakeDriver([record("idx")], corrupt=99),
                         exact=[SimpleNamespace(fact="scan", episodes=[])])
        self.assertEqual(facts(self.search(g, 3)), ["scan"])
        self.assertEqual(len(g.driver.index_calls), _INDEX_TRIES)
        self.assertEqual(g.exact_limits, [3 + RETIRED_HEADROOM])

    def test_index_error_falls_back_to_scan(self):
        g = FakeGraphiti(FakeDriver([], error=RuntimeError("Vector index not found")),
                         exact=[SimpleNamespace(fact="scan", episodes=[])])
        self.assertEqual(facts(self.search(g, 3)), ["scan"])
        self.assertEqual(len(g.driver.index_calls), 1)

    def test_other_graph_errors_are_raised(self):
        g = FakeGraphiti(FakeDriver([], error=ConnectionError("connection reset by peer")),
                         exact=[SimpleNamespace(fact="scan", episodes=[])])
        with self.assertRaises(ConnectionError):
            self.search(g, 3)
        self.assertEqual(g.exact_limits, [])

    def test_empty_index_result_is_returned_as_is(self):
        g = FakeGraphiti(FakeDriver([]), exact=[SimpleNamespace(fact="scan", episodes=[])])
        self.assertEqual(self.search(g, 3), [])
        self.assertEqual(g.exact_limits, [])

    def test_concurrent_searches_never_overlap_index_queries(self):
        from graphiti_client import search_edges_semantic
        driver = FakeDriver([record(f"f{i}") for i in range(20)], delay=0.02)

        async def both():
            g1, g2 = FakeGraphiti(driver), FakeGraphiti(driver)
            return await asyncio.gather(search_edges_semantic(g1, "a", 3),
                                        search_edges_semantic(g2, "b", 3),
                                        search_edges_semantic(g1, "c", 3))

        with patch.dict(_os.environ, ON):
            outs = asyncio.run(both())
        self.assertEqual([facts(o) for o in outs], [["f0", "f1", "f2"]] * 3)
        self.assertEqual(len(driver.index_calls), 3)
        self.assertEqual(driver.max_inflight, 1)

    def test_shares_lock_with_ingest_vector_reads(self):
        """ingest 的向量读（utils/graphiti_parallel）持锁时，检索要等它放锁。"""
        from graphiti_client import search_edges_semantic
        from utils.graphiti_parallel import _vector_read_lock
        driver = FakeDriver([record("f0")])

        async def run():
            order = []
            async with _vector_read_lock():
                task = asyncio.ensure_future(search_edges_semantic(FakeGraphiti(driver), "q", 1))
                await asyncio.sleep(0.02)
                order.append(("ingest-done", len(driver.index_calls)))
            await task
            order.append(("search-done", len(driver.index_calls)))
            return order

        with patch.dict(_os.environ, ON):
            self.assertEqual(asyncio.run(run()), [("ingest-done", 0), ("search-done", 1)])


class IndexCheckCompare(unittest.TestCase):
    """tools/vector_index_check 的判定：重复即坏；并列分截断不同不算漏；只卡前 30 名。"""

    def compare(self, got, ref, hits=1024, distinct=1024):
        from tools.vector_index_check import compare
        return compare([(u, s, hits, distinct) for u, s in got], ref)

    def test_candidates_match_search(self):
        """验收用的候选数必须等于线上检索用的，否则验收的不是会被执行的那条路径。"""
        from graphiti_client import SEMANTIC_CANDIDATES
        from tools.vector_index_check import CANDIDATES
        self.assertEqual(CANDIDATES, SEMANTIC_CANDIDATES)

    def test_identical_passes(self):
        rows = [(f"u{i}", 0.9 - i / 1000) for i in range(50)]
        self.assertEqual(self.compare(rows, rows), {"ok": True, "duplicates": False, "missed_ranks": []})

    def test_tie_at_cutoff_is_not_a_miss(self):
        ref = [(f"u{i}", 0.9 - i / 1000) for i in range(10)] + [("dup-a", 0.5)]
        got = ref[:10] + [("dup-b", 0.5)]
        self.assertEqual(self.compare(got, ref)["missed_ranks"], [])

    def test_top_miss_fails_and_reports_rank(self):
        ref = [(f"u{i}", 0.9 - i / 1000) for i in range(50)]
        got = [r for r in ref if r[0] != "u3"] + [("x", 0.8)]
        res = self.compare(got, ref)
        self.assertFalse(res["ok"])
        self.assertEqual(res["missed_ranks"], [4])

    def test_tail_miss_is_reported_but_passes(self):
        ref = [(f"u{i}", 0.9 - i / 1000) for i in range(50)]
        got = [r for r in ref if r[0] != "u44"] + [("x", 0.5)]
        res = self.compare(got, ref)
        self.assertTrue(res["ok"])
        self.assertEqual(res["missed_ranks"], [45])

    def test_short_index_result_counts_misses(self):
        ref = [(f"u{i}", 0.9 - i / 1000) for i in range(50)]
        res = self.compare(ref[:10], ref)
        self.assertFalse(res["ok"])
        self.assertEqual(res["missed_ranks"], list(range(11, 51)))

    def test_duplicates_fail(self):
        rows = [(f"u{i}", 0.9 - i / 1000) for i in range(50)]
        res = self.compare(rows, rows, hits=1024, distinct=300)
        self.assertFalse(res["ok"])
        self.assertTrue(res["duplicates"])


if __name__ == "__main__":
    unittest.main()
