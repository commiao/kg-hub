"""kg_eval 的边检索必须和线上 kg_search 走同一条路径（T-0273）。

事故：kg_eval 用 Graphiti.search() 的默认配置（bm25 全文 + cosine）。bm25 腿在线上
约 16 万条边的图上每次撞 FalkorDB 30s 查询超时，异常被 run_question 吞掉，于是
每道题 edges=0，周报的召回率只剩 episode 全文那一半，结论失真。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import unittest
from types import SimpleNamespace

try:
    import graphiti_core  # noqa: F401
    HAVE_GRAPHITI = True
except ImportError:
    HAVE_GRAPHITI = False


class FakeDriver:
    async def execute_query(self, cypher, **params):
        return [{"content": "episode mentions falkordb"}], None, None


class FakeGraphiti:
    """默认 search() 一被调用就失败；_search() 记录收到的配置。"""

    def __init__(self, facts=None, error=None):
        self.driver = FakeDriver()
        self.facts = facts if facts is not None else ["kg-hub stores its graph in FalkorDB"]
        self.error = error
        self.configs = []

    async def search(self, *args, **kwargs):
        raise AssertionError("kg_eval 不该走 Graphiti.search() 的默认混合检索")

    async def _search(self, query, config, **kwargs):
        self.configs.append(config)
        if self.error is not None:
            raise self.error
        edges = [SimpleNamespace(fact=f, source_node_uuid="s", target_node_uuid="t",
                                 valid_at=None, invalid_at=None, expired_at=None,
                                 created_at=None) for f in self.facts]
        return SimpleNamespace(edges=edges[:config.limit])


def _method_names(cfg):
    return [str(getattr(m, "value", m)) for m in cfg.edge_config.search_methods]


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class SharedConfig(unittest.TestCase):
    def test_vec_only_config_has_no_fulltext_leg(self):
        from graphiti_client import EDGE_VEC_ONLY_SEARCH
        self.assertEqual(_method_names(EDGE_VEC_ONLY_SEARCH), ["cosine_similarity"])

    def test_server_semantic_search_uses_the_shared_config(self):
        """kg_search 的真实入口 /api/search_semantic 也只走 cosine——kg_eval 镜像的就是它。"""
        import json
        from unittest.mock import AsyncMock, patch
        import graphiti_client
        import kg_hub_server
        g = FakeGraphiti(facts=["a", "b", "c"])
        req = SimpleNamespace(query_params={"q": "graph backend", "num_results": "2", "bump": "0"})
        with patch.object(kg_hub_server, "get_graphiti", AsyncMock(return_value=g)):
            resp = asyncio.run(kg_hub_server.search_semantic(req))
        body = json.loads(resp.body)
        self.assertEqual([o["fact"] for o in body["results"]], ["a", "b"])
        self.assertEqual(len(g.configs), 1)
        self.assertEqual(_method_names(g.configs[0]), ["cosine_similarity"])
        # 多取 RETIRED_HEADROOM 条抵扣已退休的边（T-0281），返回仍按请求条数截断
        self.assertEqual(g.configs[0].limit, 2 + graphiti_client.RETIRED_HEADROOM)

    def test_kg_eval_uses_the_shared_function(self):
        import graphiti_client
        import tools.kg_eval as K
        self.assertIs(K.search_edges_semantic, graphiti_client.search_edges_semantic)


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class RunQuestion(unittest.TestCase):
    ITEM = {"id": "graph-backend", "q": "what database backs the kg-hub knowledge graph",
            "keywords": ["falkordb"]}

    def test_edges_come_from_vec_only_search(self):
        import tools.kg_eval as K
        from graphiti_client import RETIRED_HEADROOM
        g = FakeGraphiti(facts=[f"fact {i} falkordb" for i in range(40)])
        r = asyncio.run(K.run_question(g, self.ITEM, {"num_edge_results": 50}, verbose=False))
        self.assertEqual(r["n_edges"], 30)          # 与 /api/search_semantic 一样封顶 30
        self.assertEqual(r["edge_error"], "")
        self.assertEqual(len(g.configs), 1)
        self.assertEqual(_method_names(g.configs[0]), ["cosine_similarity"])
        self.assertEqual(g.configs[0].limit, 30 + RETIRED_HEADROOM)

    def test_shared_config_is_not_mutated_by_limit(self):
        import tools.kg_eval as K
        from graphiti_client import EDGE_VEC_ONLY_SEARCH
        before = EDGE_VEC_ONLY_SEARCH.limit
        asyncio.run(K.run_question(FakeGraphiti(), self.ITEM, {"num_edge_results": 7}, False))
        self.assertEqual(EDGE_VEC_ONLY_SEARCH.limit, before)

    def test_edge_search_error_is_reported_not_silenced(self):
        import tools.kg_eval as K
        g = FakeGraphiti(error=RuntimeError("Query timed out"))
        r = asyncio.run(K.run_question(g, self.ITEM, {}, verbose=False))
        self.assertEqual(r["n_edges"], 0)
        self.assertIn("Query timed out", r["edge_error"])
        md = K.render([r], 0.7)
        self.assertIn("1/1 questions hit a search error", md)
        self.assertIn("| graph-backend | ✓ | 1/1 | ERR | 1 |", md)
        self.assertIn("graph-backend edges: `RuntimeError: Query timed out`", md)

    def test_clean_run_has_no_error_banner(self):
        import tools.kg_eval as K
        r = asyncio.run(K.run_question(FakeGraphiti(), self.ITEM, {}, verbose=False))
        md = K.render([r], 0.7)
        self.assertIn("| graph-backend | ✓ | 1/1 | 1 | 1 |", md)
        self.assertNotIn("search error", md)


if __name__ == "__main__":
    unittest.main()
