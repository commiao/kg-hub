"""kg_search 不再召回治理退休的知识（T-0281）。

事故：/api/knowledge_feedback 的 auto_retire 把 episode 标 archived=true，但语义检索
search_edges_semantic() 不看 archived，于是 MCP kg_search 和 tools/kg_eval 照样返回
只由已退休 episode 支撑的边；只有 /api/search 的打分环节会剔除它们。

同时约束反方向：graphiti 的失效判定（expired_at/invalid_at）误伤严重——2026-10-14
实测 22% 的边被判失效，抽样多数仍成立（例如 921b8609「Neo4j 被拒、改用 FalkorDB」）。
所以已失效的边必须照常返回，只把 invalid_at/expired_at 带给使用方。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import graphiti_core  # noqa: F401
    HAVE_GRAPHITI = True
except ImportError:
    HAVE_GRAPHITI = False

T = datetime(2026, 6, 1, 12, 12, 28, tzinfo=timezone.utc)


def edge(fact, eps, invalid=False):
    return SimpleNamespace(uuid=f"u-{fact}", fact=fact, episodes=eps,
                           source_node_uuid="s", target_node_uuid="t",
                           valid_at=None, created_at=None,
                           invalid_at=T if invalid else None, expired_at=T if invalid else None)


class FakeDriver:
    """只回答 archived 查询：按 $u 里的 uuid 返回 {u, arch}；库里没有的 uuid 不返回。"""

    def __init__(self, archived, missing=(), error=None):
        self.archived = set(archived)
        self.missing = set(missing)
        self.error = error
        self.queries = []

    async def execute_query(self, cypher, **params):
        self.queries.append((cypher, params))
        if self.error is not None:
            raise self.error
        if "ep.archived" in cypher and "u" in params:
            return [{"u": u, "arch": u in self.archived}
                    for u in params["u"] if u not in self.missing], None, None
        return [], None, None


class FakeGraphiti:
    """按相关度排好的边列表；_search 按 config.limit 截取，记录每次的 limit。"""

    def __init__(self, ranked, driver):
        self.ranked = ranked
        self.driver = driver
        self.limits = []

    async def _search(self, query, config, **kwargs):
        self.limits.append(config.limit)
        return SimpleNamespace(edges=self.ranked[:config.limit])


def run(coro):
    return asyncio.run(coro)


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class RetiredFilter(unittest.TestCase):
    def search(self, g, limit):
        from graphiti_client import search_edges_semantic
        return [e.fact for e in run(search_edges_semantic(g, "q", limit))]

    def test_all_archived_backing_is_dropped_others_kept(self):
        ranked = [
            edge("retired", ["a1", "a2"]),       # 背书全退休 → 剔除
            edge("mixed", ["a1", "live"]),       # 仍有活背书 → 保留
            edge("no-backing", []),              # 无背书 → 保留（与 /api/search 一致）
            edge("ghost", ["gone"]),             # 背书 episode 已不存在 → 保留
            edge("plain", ["live"]),
        ]
        g = FakeGraphiti(ranked, FakeDriver(archived={"a1", "a2"}, missing={"gone"}))
        self.assertEqual(self.search(g, 10), ["mixed", "no-backing", "ghost", "plain"])

    def test_expired_edges_are_not_filtered(self):
        """graphiti 判失效的边照常返回（误判太多），只由 archived 决定退休。"""
        ranked = [edge("neo4j rejected for falkordb", ["live"], invalid=True),
                  edge("other", ["live"])]
        g = FakeGraphiti(ranked, FakeDriver(archived=set()))
        self.assertEqual(self.search(g, 2), ["neo4j rejected for falkordb", "other"])

    def test_headroom_backfills_to_limit_in_one_scan(self):
        from graphiti_client import RETIRED_HEADROOM
        ranked = [edge(f"r{i}", ["dead"]) if i in (0, 2, 4) else edge(f"f{i}", ["live"])
                  for i in range(40)]
        g = FakeGraphiti(ranked, FakeDriver(archived={"dead"}))
        out = self.search(g, 5)
        self.assertEqual(out, ["f1", "f3", "f5", "f6", "f7"])   # 顺序即排名，不打乱
        self.assertEqual(g.limits, [5 + RETIRED_HEADROOM])      # 常见情况只扫一次

    def test_refetches_when_retired_exceed_headroom(self):
        from graphiti_client import RETIRED_HEADROOM
        n_dead = RETIRED_HEADROOM + 5
        ranked = [edge(f"r{i}", ["dead"]) for i in range(n_dead)] + \
                 [edge(f"f{i}", ["live"]) for i in range(30)]
        g = FakeGraphiti(ranked, FakeDriver(archived={"dead"}))
        out = self.search(g, 5)
        self.assertEqual(out, [f"f{i}" for i in range(5)])
        self.assertEqual(g.limits, [5 + RETIRED_HEADROOM, 2 * (5 + RETIRED_HEADROOM)])

    def test_stops_when_graph_has_no_more_edges(self):
        ranked = [edge("r0", ["dead"]), edge("f0", ["live"])]
        g = FakeGraphiti(ranked, FakeDriver(archived={"dead"}))
        self.assertEqual(self.search(g, 5), ["f0"])
        self.assertEqual(len(g.limits), 1)                       # 图里没更多了，不再补取

    def test_refetch_is_bounded(self):
        ranked = [edge(f"r{i}", ["dead"]) for i in range(500)]
        g = FakeGraphiti(ranked, FakeDriver(archived={"dead"}))
        self.assertEqual(self.search(g, 5), [])
        self.assertEqual(len(g.limits), 3)

    def test_archived_lookup_failure_does_not_break_search(self):
        ranked = [edge("retired", ["dead"]), edge("plain", ["live"])]
        g = FakeGraphiti(ranked, FakeDriver(archived={"dead"}, error=RuntimeError("timeout")))
        with self.assertLogs("kg_hub.search", "WARNING"):
            self.assertEqual(self.search(g, 5), ["retired", "plain"])

    def test_kg_eval_sees_the_same_filter(self):
        import tools.kg_eval as K
        ranked = [edge("retired falkordb", ["dead"]), edge("live fact", ["live"])]
        g = FakeGraphiti(ranked, FakeDriver(archived={"dead"}))
        item = {"id": "graph-backend", "q": "backend", "keywords": ["falkordb"]}
        r = run(K.run_question(g, item, {}, verbose=False))
        self.assertEqual(r["n_edges"], 1)
        self.assertEqual(r["top_edge"], "live fact")


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class SemanticEndpoint(unittest.TestCase):
    def call(self, g, **params):
        import kg_hub_server
        req = SimpleNamespace(query_params={"q": "graph backend", **params})
        status = SimpleNamespace(execute_query=AsyncMock(return_value=([], None, None)))
        with patch.object(kg_hub_server, "get_graphiti", AsyncMock(return_value=g)), \
                patch.object(kg_hub_server, "get_status_driver", return_value=status):
            resp = run(kg_hub_server.search_semantic(req))
        return json.loads(resp.body), status

    def test_retired_dropped_and_expiry_exposed(self):
        ranked = [edge("retired", ["dead"]), edge("superseded?", ["live"], invalid=True),
                  edge("current", ["live"])]
        g = FakeGraphiti(ranked, FakeDriver(archived={"dead"}))
        body, _ = self.call(g, num_results="2", bump="0")
        self.assertEqual([o["fact"] for o in body["results"]], ["superseded?", "current"])
        self.assertEqual(body["results"][0]["invalid_at"], T.isoformat())
        self.assertEqual(body["results"][0]["expired_at"], T.isoformat())
        self.assertIsNone(body["results"][1]["invalid_at"])

    def test_bump_skips_archived_episodes(self):
        g = FakeGraphiti([edge("current", ["live"])], FakeDriver(archived=set()))
        _, status = self.call(g, num_results="1")
        cypher = status.execute_query.await_args.args[0]
        self.assertIn("NOT coalesce(ep.archived, false)", cypher)


if __name__ == "__main__":
    unittest.main()
