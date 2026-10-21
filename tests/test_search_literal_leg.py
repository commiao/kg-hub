"""/api/search 的子串腿不再扫全图（T-0343）。

原来子串腿每一步都是 toLower(e.fact) CONTAINS 扫全部 17 万条边：整句一次、不中再按 bounded_terms
全词一次，2026-10-21 线上剖析各约 1.1 s；合并后按 fact 补 episodes 的 e.fact IN $facts 也没有索引，
再扫一次 0.3–0.4 s。整次检索 3.6 s，NAS 跑一条就从 68°C 升到 81°C。

现在：已有候选（语义池 + 标识符腿）在 Python 里判字面；新候选只走 RELATES_TO.fact 全文索引，
取回后按子串核一遍；episodes 直接用语义腿边自己的。全文索引只支持前缀、不在汉字处断词，
「公众号 阅读量」这种落在汉字串中间的词补不进新候选——离线 231 条评测里补入对 top-10 零贡献，
起作用的只有给语义候选提权，那部分完整保留。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tools.search_terms import contains_all_terms, literal_fulltext_query

try:
    import graphiti_core  # noqa: F401
    HAVE_GRAPHITI = True
except ImportError:
    HAVE_GRAPHITI = False

LOGGER = "kg_hub.server"


class LiteralFulltextQuery(unittest.TestCase):
    def test_ascii_follows_ident_rules_cjk_is_prefix(self):
        self.assertEqual(literal_fulltext_query(("tailscale",)), "@fact:(tailscale*)")
        self.assertEqual(literal_fulltext_query(("sls 日志查询",)), "@fact:(日志查询* sls)")
        self.assertEqual(literal_fulltext_query(("接口迁移",)), "@fact:(接口迁移*)")
        self.assertEqual(literal_fulltext_query(("redis 6.2",)), "@fact:(redis*)")

    def test_terms_are_split_like_the_index_and_deduplicated(self):
        # 索引在 ASCII 标点处断词；同一片段只查一次
        self.assertEqual(literal_fulltext_query(("model-attempts.sqlite3", "MODEL")),
                         "@fact:(attempts* sqlite3* model*)")

    def test_keeps_only_the_longest_pieces(self):
        q = literal_fulltext_query(("aa bbb cccc ddddd eeeeee fffffff gggggggg",))
        self.assertEqual(q, "@fact:(gggggggg* fffffff* eeeeee* ddddd* cccc* bbb)")

    def test_nothing_usable(self):
        self.assertEqual(literal_fulltext_query(("a",)), "")
        self.assertEqual(literal_fulltext_query(("！？ 。",)), "")
        self.assertEqual(literal_fulltext_query(()), "")

    def test_query_syntax_cannot_be_injected(self):
        q = literal_fulltext_query(('foo@fact:(bar|*) -baz "qux" ~x {y} $lim 中文%',))
        body = q[len("@fact:("):-1]
        self.assertTrue(all(ch.isalnum() or ch in "_* " for ch in body), body)


class ContainsAllTerms(unittest.TestCase):
    def test_case_insensitive_substring_like_the_old_scan(self):
        self.assertTrue(contains_all_terms("文章的阅读量在公众号后台", ("公众号", "阅读量")))
        self.assertTrue(contains_all_terms("Mac uses TAILSCALE", ("tailscale",)))
        # 子串而非整词：与原 CONTAINS 一致
        self.assertTrue(contains_all_terms("bootouts", ("bootout",)))
        self.assertFalse(contains_all_terms("公众号文章", ("公众号", "阅读量")))
        self.assertFalse(contains_all_terms(None, ("x",)))


def row(fact, eps=("lit",)):
    return {"fact": fact, "su": "s", "tu": "t", "va": None, "ia": None, "xa": None, "ca": None,
            "eps": list(eps)}


class IndexDriver:
    """全文索引：每个片段都要是事实里某个索引词的前缀（ASCII 标点、空白断词，汉字不断）。"""

    def __init__(self, facts=(), fail=False):
        self.facts = list(facts)
        self.fail = fail
        self.queries = []

    @staticmethod
    def _index_words(fact):
        return [w for w in re.split(r"[\s,.<>{}\[\]\"':;!@#$%^&*()\-+=~/|?`\\]+", fact.lower()) if w]

    async def execute_query(self, cypher, **params):
        self.queries.append((cypher, params))
        if self.fail:
            raise RuntimeError("fulltext down")
        if "startNode(e)" in cypher:
            pieces = params["ftq"][len("@fact:("):-1].replace("*", "").split()
            hits = [row(f) for f in self.facts
                    if all(any(w.startswith(p) for w in self._index_words(f)) for p in pieces)]
            return hits[:params["lim"]], None, None
        return [], None, None

    def ftqs(self):
        return [p["ftq"] for c, p in self.queries if "startNode(e)" in c]


def literal_leg(driver, query, known=(), limit=40):
    import kg_hub_server
    return asyncio.run(kg_hub_server._literal_leg(driver, query, list(known), limit))


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class LiteralLeg(unittest.TestCase):
    def test_cjk_inside_a_run_is_flagged_on_known_candidates(self):
        """「接口迁移」落在汉字串中间：索引查不到，但语义候选照样被判为字面命中。"""
        known = ["怎么确认接口迁移真的完成了", "无关事实"]
        d = IndexDriver(["怎么确认接口迁移真的完成了"])
        hits, rows, applied = literal_leg(d, "接口迁移", known)
        self.assertEqual(hits, {"怎么确认接口迁移真的完成了"})
        self.assertEqual((rows, applied), ([], []))

    def test_index_rows_are_verified_before_being_added(self):
        """索引按片段 AND 前缀匹配，比子串宽：取回后要按整句再核一遍。"""
        d = IndexDriver(["tailscale ping works but ssh does not",
                         "ping over tailscale works", "tailscale"])
        hits, rows, _ = literal_leg(d, "tailscale ping")
        self.assertEqual(hits, set())
        self.assertEqual([r["fact"] for r in rows], ["tailscale ping works but ssh does not"])

    def test_phrase_hit_stops_before_all_terms_step(self):
        d = IndexDriver(["see 公众号 阅读量 trend"])
        hits, rows, _ = literal_leg(d, "公众号 阅读量")
        self.assertEqual([r["fact"] for r in rows], ["see 公众号 阅读量 trend"])
        self.assertEqual(len(d.ftqs()), 1)

    def test_falls_back_to_all_terms_when_phrase_misses(self):
        known = ["公众号文章发布后阅读量断崖", "公众号改版"]
        d = IndexDriver([])
        hits, rows, _ = literal_leg(d, "公众号 阅读量", known)
        self.assertEqual(hits, {"公众号文章发布后阅读量断崖"})
        self.assertEqual(d.ftqs(), ["@fact:(公众号* 阅读量*)", "@fact:(公众号* 阅读量*)"])

    def test_known_facts_are_not_added_again_and_new_rows_are_capped(self):
        facts = [f"tailscale fact {i}" for i in range(10)]
        hits, rows, _ = literal_leg(IndexDriver(facts), "tailscale", known=facts[:2], limit=3)
        self.assertEqual(hits, set(facts[:2]))
        self.assertEqual([r["fact"] for r in rows], facts[2:5])

    def test_aliases_only_when_direct_steps_miss(self):
        from tools.retrieval_aliases import query_aliases
        q = "mcp 内存 涨"
        self.assertTrue(query_aliases(q))
        d = IndexDriver(["muxcp child processes leak memory"])
        hits, rows, applied = literal_leg(d, q)
        self.assertEqual([r["fact"] for r in rows], ["muxcp child processes leak memory"])
        self.assertEqual(applied, ["mcp-memory-to-muxcp"])
        # 直接命中时不用别名
        d = IndexDriver(["mcp 内存 涨 again", "muxcp child processes leak memory"])
        _, rows, applied = literal_leg(d, q)
        self.assertEqual(([r["fact"] for r in rows], applied), (["mcp 内存 涨 again"], []))

    def test_never_scans_the_graph(self):
        d = IndexDriver(["anything"])
        literal_leg(d, "公众号 流量 怎么办", known=["x"])
        for cypher, _ in d.queries:
            self.assertNotIn("CONTAINS", cypher)
            self.assertIn("db.idx.fulltext.queryRelationships", cypher)


def edge(fact, eps):
    return SimpleNamespace(uuid=f"u-{fact}", fact=fact, episodes=eps, source_node_uuid="s",
                           target_node_uuid="t", valid_at=None, created_at=None,
                           invalid_at=None, expired_at=None)


class FakeGraphiti:
    def __init__(self, ranked, fail=False):
        self.ranked, self.fail = ranked, fail
        self.driver = SimpleNamespace(execute_query=AsyncMock(return_value=([], None, None)))

    async def _search(self, query, config, **kwargs):
        if self.fail:
            raise RuntimeError("embedder down")
        return SimpleNamespace(edges=self.ranked[:config.limit])


class EndpointDriver(IndexDriver):
    """子串腿走索引；episode 属性按 uuid 返回 origin_project。其余查询（标识符腿）不中。"""

    def __init__(self, facts=(), projects=None, fail=False):
        super().__init__(facts, fail)
        self.projects = projects or {}

    async def execute_query(self, cypher, **params):
        if "MATCH (ep:Episodic)" in cypher:
            self.queries.append((cypher, params))
            return [{"u": u, "prov": "", "dur": "", "ver": False, "uc": 0, "kind": "",
                     "proj": self.projects.get(u, ""), "arch": False, "nm": "", "ref": ""}
                    for u in params["u"]], None, None
        if "startNode(e)" in cypher or self.fail:
            return await super().execute_query(cypher, **params)
        self.queries.append((cypher, params))
        return [], None, None


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class SearchEndpoint(unittest.TestCase):
    def run_search(self, driver, ranked, q, sem_fail=False, **params):
        import kg_hub_server
        req = SimpleNamespace(query_params={"q": q, "num_results": "10", "bump": "0", **params})
        g = FakeGraphiti(ranked, fail=sem_fail)
        with patch.object(kg_hub_server, "get_graphiti", AsyncMock(return_value=g)), \
                patch.object(kg_hub_server, "get_status_driver", return_value=driver):
            return json.loads(asyncio.run(kg_hub_server.search(req)).body)

    def test_no_full_graph_scan_in_a_search(self):
        """回归守卫：一次检索里不再有 CONTAINS 扫描，也不再按 fact 文本查边（无索引）。"""
        d = EndpointDriver(["公众号 阅读量 断崖"])
        ranked = [edge(f"semantic {i}", [f"e{i}"]) for i in range(40)]
        self.run_search(d, ranked, "公众号 阅读量")
        self.run_search(d, ranked, "launchctl bootout 之后马上 bootstrap 报 Input/output error")
        self.assertTrue(d.queries)
        for cypher, _ in d.queries:
            self.assertNotIn("CONTAINS", cypher)
            self.assertNotIn("e.fact IN", cypher)

    def test_literal_match_lifts_a_semantic_candidate(self):
        """语义第 11 名的事实字面含整句：+0.15 把它推进前 10（排名分步长 1/40）。"""
        ranked = [edge(f"semantic {i}", [f"e{i}"]) for i in range(40)]
        ranked.insert(10, edge("怎么确认接口迁移真的完成了", ["gold"]))
        body = self.run_search(EndpointDriver([]), ranked, "接口迁移")
        facts = [r["fact"] for r in body["results"]]
        self.assertLess(facts.index("怎么确认接口迁移真的完成了"), 10)
        # 对照：去掉字面词，同一名次留在前 10 之外
        ranked[10] = edge("怎么确认迁移真的完成了", ["gold"])
        facts = [r["fact"] for r in self.run_search(EndpointDriver([]), ranked, "接口迁移")["results"]]
        self.assertNotIn("怎么确认迁移真的完成了", facts)

    def test_semantic_episodes_come_from_the_edge_itself(self):
        """不再按 fact 补 episodes：facet 过滤与背书都用语义腿边自带的 episodes。"""
        ranked = [edge("fact in project a", ["ep-a"]), edge("fact in project b", ["ep-b"])]
        d = EndpointDriver([], projects={"ep-a": "a", "ep-b": "b"})
        body = self.run_search(d, ranked, "something", project="b")
        self.assertEqual([r["fact"] for r in body["results"]], ["fact in project b"])

    def test_semantic_failure_still_answers_from_the_index(self):
        d = EndpointDriver(["Tailscale DERP relay fallback on the Mac"])
        with self.assertLogs(LOGGER, "ERROR"):
            body = self.run_search(d, [], "tailscale derp", sem_fail=True)
        self.assertEqual(body["mode"], "substring_fallback")
        self.assertEqual([r["fact"] for r in body["results"]],
                         ["Tailscale DERP relay fallback on the Mac"])

    def test_literal_leg_failure_keeps_semantic_results(self):
        ranked = [edge(f"semantic {i}", [f"e{i}"]) for i in range(40)]
        with self.assertLogs(LOGGER, "ERROR"):
            body = self.run_search(EndpointDriver([], fail=True), ranked, "接口迁移")
        self.assertEqual([r["fact"] for r in body["results"]], [f"semantic {i}" for i in range(10)])


if __name__ == "__main__":
    unittest.main()
