"""/api/search 的标识符腿（T-0339 子任务）。

2026-10-21 向量模型换成 multilingual-MiniLM 后，人写记忆集里带英文标识符的问法从 4/5 掉到 2/5：
「launchctl bootout 之后马上 bootstrap 报 Input/output error」「model-attempts.sqlite3 全表扫描会不会
影响入库任务」。子串腿要求整句或所有词都在事实里，中文虚词让它永远不中；所以把查询里的 ASCII 片段
单独拿出来走 RELATES_TO.fact 的全文索引，按 idf 加权的覆盖度提权。

另一个坑：全文索引不在中文标点/汉字处断词，「XAUTOCLAIM；Stream」「sqlite3，约」被存成一个词，
精确词查不到人写的中文事实，所以 ≥4 字符的词要用前缀查。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tools.search_terms import (
    IDENT_DF_MAX, ident_coverage, ident_fulltext_query, ident_units, ident_weights,
)

try:
    import graphiti_core  # noqa: F401
    HAVE_GRAPHITI = True
except ImportError:
    HAVE_GRAPHITI = False

LOGGER = "kg_hub.server"
LAUNCHCTL_Q = "launchctl bootout 之后马上 bootstrap 报 Input/output error"


class IdentUnits(unittest.TestCase):
    def test_ascii_runs_inside_chinese_sentence(self):
        self.assertEqual(ident_units(LAUNCHCTL_Q), (
            ("launchctl",), ("bootout",), ("bootstrap",), ("input", "output"), ("error",)))

    def test_dotted_and_dashed_identifier_is_one_unit(self):
        self.assertEqual(ident_units("model-attempts.sqlite3 全表扫描会不会影响入库任务"),
                         (("model", "attempts", "sqlite3"),))

    def test_noise_tokens_dropped(self):
        # 单字母、停用词、两位以内的纯数字命中面太大；三位数（如 503）保留
        self.assertEqual(ident_units("deploy-standard 准则 31 说的是什么"), (("deploy", "standard"),))
        self.assertEqual(ident_units("T-0314 是 a 的 the 6.2 版本 503"), (("0314",), ("503",)))

    def test_pure_chinese_and_duplicates(self):
        self.assertEqual(ident_units("在 NAS 线上账本库上跑统计要注意什么"), (("nas",),))
        self.assertEqual(ident_units("给监控定告警阈值"), ())
        self.assertEqual(ident_units("Redis redis REDIS"), (("redis",),))

    def test_unit_count_bounded(self):
        self.assertEqual(len(ident_units(" ".join(f"word{i}" for i in range(40)))), 12)


class IdentFulltextQuery(unittest.TestCase):
    def test_prefix_for_long_tokens_exact_for_short_and_digits(self):
        self.assertEqual(ident_fulltext_query(("xautoclaim",)), "@fact:(xautoclaim*)")
        self.assertEqual(ident_fulltext_query(("nas",)), "@fact:(nas)")
        self.assertEqual(ident_fulltext_query(("0314",)), "@fact:(0314)")
        self.assertEqual(ident_fulltext_query(("model", "attempts", "sqlite3")),
                         "@fact:(model* attempts* sqlite3*)")

    def test_empty(self):
        self.assertEqual(ident_fulltext_query(()), "")

    def test_query_syntax_cannot_be_injected(self):
        for unit in ident_units('foo@fact:(bar|*) -baz "qux" ~x {y} $lim'):
            body = ident_fulltext_query(unit)[len("@fact:("):-1]
            self.assertTrue(all(ch.isalnum() or ch in "_* " for ch in body), body)


class IdentCoverage(unittest.TestCase):
    W = {("launchctl",): 6.0, ("bootout",): 7.0, ("bootstrap",): 5.0, ("input", "output"): 8.0,
         ("error",): 3.0}

    def test_full_and_partial_coverage(self):
        self.assertAlmostEqual(ident_coverage(
            "launchctl bootout then bootstrap fails with error 5 Input/output error", self.W), 1.0)
        self.assertAlmostEqual(ident_coverage("任何 bootout/bootstrap 成对操作都要等到 `launchctl print` 查不到",
                                              self.W), 18 / 29)

    def test_ascii_glued_to_chinese_still_counts(self):
        w = {("xautoclaim",): 7.0}
        self.assertEqual(ident_coverage("cerberus 线上 Redis(<6.2)不支持XAUTOCLAIM；Stream 消费者", w), 1.0)
        w = {("model", "attempts", "sqlite3"): 8.0}
        self.assertEqual(ident_coverage("账本库（/backup/model-attempts.sqlite3，约 16 GiB）", w), 1.0)

    def test_multi_token_unit_needs_every_token(self):
        self.assertEqual(ident_coverage("the input was empty", {("input", "output"): 1.0}), 0.0)

    def test_whole_words_only_plural_allowed(self):
        # 召回用了前缀，覆盖度按完整词判：「Invalid Type」不算中 InvalidCollector
        self.assertEqual(ident_coverage("InvalidCollector assigned to a List type", {("invalid",): 1.0}), 0.0)
        self.assertEqual(ident_coverage("bootstrapping the daemon", {("bootstrap",): 1.0}), 0.0)
        self.assertEqual(ident_coverage("two errors and three statuses", {("error",): 1.0, ("status",): 1.0}), 1.0)
        self.assertEqual(ident_coverage("a nasty bug", {("nas",): 1.0}), 0.0)
        self.assertEqual(ident_coverage("port 5030 open", {("503",): 1.0}), 0.0)

    def test_weights_drop_units_absent_from_graph_and_rank_rare_higher(self):
        w = ident_weights([("bootout",), ("error",), ("nosuchword",)],
                          {("bootout",): 161, ("error",): 4204, ("nosuchword",): 0}, 172188)
        self.assertEqual(set(w), {("bootout",), ("error",)})
        self.assertGreater(w[("bootout",)], 1.5 * w[("error",)])


def edge(fact, eps):
    return SimpleNamespace(uuid=f"u-{fact}", fact=fact, episodes=eps, source_node_uuid="s",
                           target_node_uuid="t", valid_at=None, created_at=None,
                           invalid_at=None, expired_at=None)


class FakeGraphiti:
    def __init__(self, ranked):
        self.ranked = ranked
        self.driver = SimpleNamespace(execute_query=AsyncMock(return_value=([], None, None)))

    async def _search(self, query, config, **kwargs):
        return SimpleNamespace(edges=self.ranked[:config.limit])


class StatusDriver:
    """子串腿全不中；全文索引：df 按表回答，单片段查询按「各词都是事实里某词的前缀」返回 uuid。"""

    def __init__(self, dfs, rows, fail_ident=False):
        self.dfs, self.rows, self.fail_ident = dfs, rows, fail_ident
        self.ftq = []

    def _index_hits(self, ftq):
        import re
        tokens = ftq[len("@fact:("):-1].replace("*", "").split()
        return [r["uuid"] for r in self.rows
                if all(any(w.startswith(t) for w in re.findall(r"[a-z0-9_]+", r["fact"].lower()))
                       for t in tokens)]

    async def execute_query(self, cypher, **params):
        if "queryRelationships" in cypher and self.fail_ident:
            raise RuntimeError("fulltext down")
        if "UNWIND $qs" in cypher:
            return [{"s": s, "n": self.dfs.get(s, 0)} for s in params["qs"]], None, None
        if "count(e) AS n" in cypher:
            return [{"n": 172188}], None, None
        if "$ftq" in cypher:
            self.ftq.append(params["ftq"])
            return [{"u": u} for u in self._index_hits(params["ftq"])][:params["lim"]], None, None
        if "e.uuid IN $ids" in cypher:
            return [r for r in self.rows if r["uuid"] in params["ids"]], None, None
        return [], None, None


def row(fact, eps=("mem",)):
    return {"uuid": "u-" + fact, "fact": fact, "su": "s", "tu": "t", "va": None, "ia": None,
            "xa": None, "ca": None, "eps": list(eps)}


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class SearchEndpointIdentLeg(unittest.TestCase):
    DFS = {"@fact:(launchctl*)": 281, "@fact:(bootout*)": 188, "@fact:(bootstrap*)": 653,
           "@fact:(input* output*)": 66, "@fact:(error*)": 4204}
    # 语义腿排在第 21 名（中文问中文，语义只算中等），标识符覆盖 0.62 把它推到第一
    GOLD = "任何 bootout/bootstrap 成对操作都要等到 `launchctl print` 查不到"
    # 语义池外的新候选：全覆盖时补进前 10
    FULL = "launchctl bootout then bootstrap fails with error 5 Input/output error"
    NOISE_ONLY_ERROR = "some error happened while bootstrapping nothing"

    def run_search(self, driver, q=LAUNCHCTL_Q):
        import kg_hub_server
        ranked = [edge(f"semantic noise {i}", ["n"]) for i in range(40)]
        ranked.insert(20, edge(self.GOLD, ["mem"]))
        req = SimpleNamespace(query_params={"q": q, "num_results": "10", "bump": "0"})
        with patch.object(kg_hub_server, "get_graphiti", AsyncMock(return_value=FakeGraphiti(ranked))), \
                patch.object(kg_hub_server, "get_status_driver", return_value=driver):
            resp = asyncio.run(kg_hub_server.search(req))
        return [r["fact"] for r in json.loads(resp.body)["results"]]

    def test_identifier_hit_outranks_semantic_noise(self):
        d = StatusDriver(self.DFS, [row(self.NOISE_ONLY_ERROR), row(self.FULL), row(self.GOLD)])
        facts = self.run_search(d)
        self.assertEqual(facts[0], self.GOLD)
        self.assertIn(self.FULL, facts)
        # 只沾了泛词 error（bootstrapping 不算 bootstrap）的弱命中低于覆盖度门槛，不补入
        self.assertNotIn(self.NOISE_ONLY_ERROR, facts)
        # 泛词 error（df > 上限）不拿来召回，稀有片段逐个查
        self.assertEqual(d.ftq, ["@fact:(launchctl*)", "@fact:(bootout*)", "@fact:(bootstrap*)",
                                 "@fact:(input* output*)"])
        self.assertGreater(self.DFS["@fact:(error*)"], IDENT_DF_MAX)

    def test_leg_returns_only_rows_above_coverage_threshold(self):
        import kg_hub_server
        d = StatusDriver(self.DFS, [row(self.NOISE_ONLY_ERROR), row(self.GOLD), row(self.FULL)])
        out = asyncio.run(kg_hub_server._ident_leg(d, LAUNCHCTL_Q, 100))
        got = {r["fact"]: round(cov, 2) for r, cov in out}
        self.assertEqual(got, {self.GOLD: 0.62, self.FULL: 1.0})

    def test_full_coverage_survives_many_single_unit_hits(self):
        """2026-10-21 线上：一次 OR 查询按索引分取前 100，全覆盖的事实反而排不进。
        逐片段取、按 idf 之和排，命中片段多的先进候选。"""
        import kg_hub_server
        # 噪声只命中最先查询的 launchctl，且在索引里排在前面
        noise = [row(f"launchctl list step {i} done") for i in range(5)]
        d = StatusDriver(self.DFS, noise + [row(self.FULL)])
        out = asyncio.run(kg_hub_server._ident_leg(d, LAUNCHCTL_Q, 1))
        self.assertEqual([r["fact"] for r, _ in out], [self.FULL])

    def test_without_ident_leg_gold_stays_out_of_top10(self):
        """对照：标识符腿不出结果时，GOLD 停在语义第 21 名。"""
        facts = self.run_search(StatusDriver({}, []))
        self.assertNotIn(self.GOLD, facts)

    def test_generic_only_query_skips_candidate_lookup(self):
        d = StatusDriver({"@fact:(nas)": 7322}, [row("NAS is a NAS")])
        facts = self.run_search(d, q="在 NAS 线上账本库上跑统计要注意什么")
        self.assertEqual(d.ftq, [])
        self.assertNotIn(self.GOLD, facts)

    def test_ident_leg_failure_keeps_other_legs(self):
        d = StatusDriver(self.DFS, [row(self.GOLD)], fail_ident=True)
        with self.assertLogs(LOGGER, "ERROR"):
            facts = self.run_search(d)
        self.assertEqual(facts, [f"semantic noise {i}" for i in range(10)])


if __name__ == "__main__":
    unittest.main()
