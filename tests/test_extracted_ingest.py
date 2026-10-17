"""预抽取入库的纯逻辑与写图语句：归一纪律指标、消解复用、边复用、来源标记。

这条路不调模型，所以测试不需要 graphiti_core：driver 只要 execute_query，embedder 只要
create_batch。写图语句必须与 graphiti 0.29.0 的 FalkorDB save 一致（vecf32、SET n = $entity_data、
MENTIONS/RELATES_TO），否则检索和画像看不到这些数据。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import unittest
from datetime import datetime, timezone

from utils import extracted_ingest as xi

ENTITY_TYPES = {"Person", "Project", "File", "Tool", "Concept", "Issue", "Fix", "Config",
                "Session", "Observation", "Capsule", "KnowledgeDoc", "Lesson"}
EDGE_TYPES = {"caused_by", "fixed_by", "verified_by", "references", "modified", "depends_on",
              "supersedes", "derived_from", "occurred_in", "belongs_to", "extracted_from",
              "relates_to", "documented_in", "diagnosed_by", "implemented_as"}


def ents(*items):
    return [xi.ExtractedEntity(name=n, type=t, summary=s) for n, t, s in items]


def edges(*items):
    return [xi.ExtractedEdge(source=a, name=r, target=b, fact=f) for a, r, b, f in items]


class NormalizeTest(unittest.TestCase):
    def test_schema_mapping_and_discipline_stats(self):
        n = xi.normalize_extraction(
            ents(("kg-hub", "Project", "repo"), ("KG-HUB", "project", "dup by case"),
                 ("commit 94a9779", "tool", "a commit"), ("/Users/mac/x", "Widget", "path")),
            edges(("kg-hub", "BELONGS_TO", "commit 94a9779", "f1"),
                  ("kg-hub", "IS_ANCESTOR_OF", "/Users/mac/x", "f2"),
                  ("kg-hub", "is_ancestor_of", "/Users/mac/x", "f2 again"),
                  ("kg-hub", "references", "ghost", "dangling"),
                  ("kg-hub", "relates_to", "kg-hub", "self")),
            ENTITY_TYPES, EDGE_TYPES)
        self.assertEqual([e.name for e in n.entities], ["kg-hub", "commit 94a9779", "/Users/mac/x"])
        self.assertEqual([e.type for e in n.entities], ["Project", "Tool", "Concept"])
        self.assertEqual([e.name for e in n.edges], ["belongs_to", "relates_to"])
        self.assertEqual(n.edges[1].raw_name, "IS_ANCESTOR_OF")
        s = n.stats
        self.assertEqual((s["dup_entity"], s["unknown_type"], s["dirty_name"]), (1, 1, 2))
        self.assertEqual((s["offschema_edge"], s["dup_edge"], s["dangling_edge"], s["self_edge"]), (2, 1, 1, 1))
        self.assertEqual((s["entities_out"], s["edges_out"]), (3, 2))

    def test_limits(self):
        with self.assertRaises(ValueError):
            xi.normalize_extraction(ents(*[(f"e{i}", "Tool", "") for i in range(61)]), [],
                                    ENTITY_TYPES, EDGE_TYPES)


class LexicalTest(unittest.TestCase):
    def test_vector_candidates_need_lexical_support(self):
        same = xi._lexically_same
        # 第 2 轮后的取舍：代码类标识符只认完全相等（分隔符有意义），宁可多一个节点也不并错近名
        self.assertFalse(same("qwen.token_plan", "qwen-token-plan"))
        self.assertTrue(same("qwen.token_plan", "QWEN.TOKEN_PLAN"))
        self.assertTrue(same("tests/test_effective_quota_deploy.py", "test_effective_quota_deploy.py"))
        self.assertTrue(same("model-gateway-private 网络", "model-gateway-private"))
        self.assertTrue(same("Writer Lock", "writer_lock"))
        # 2026-10-17 冒烟里的两次误并
        self.assertFalse(same("实际额度来源修正发布（T-0046）", "反思改进项落实专项 (REF-001~005)"))
        self.assertFalse(same("effective-quota 发布脚本不入仓导致本机测试 FileNotFoundError", "docs/effective-quota"))
        self.assertFalse(same("kg-hub", "kg-hub-server 镜像"))  # 短名不能靠包含吃掉长名的一半以下
        # 第 2 轮（opus）复盘：同类型近名误并
        self.assertFalse(same("tests/test_phase1m.py", "tests/test_phase1p.py", "File"))
        self.assertFalse(same("deploy-standard 准则 19–22", "deploy-standard 准则 26–28"))
        self.assertFalse(same("2026-09-19 网关付费请求全 503", "2026-09-18 早间网关全量 503"))
        self.assertFalse(same("kg-hub:deploy/nas/release.sh", "deploy/nas-release.sh", "File"))
        self.assertFalse(same("credvault:deploy/nas-release.sh", "deploy/nas-release.sh", "File"))  # 仓库:路径 是另一仓库的文件
        self.assertTrue(same("credvault/deploy/nas-release.sh", "deploy/nas-release.sh", "File"))   # 全路径 vs 子路径
        self.assertFalse(same("deploy/nas/release.sh", "deploy/nas-release.sh", "File"))
        self.assertFalse(same("max_tokens 路由上限裁剪", "max_tokens"))
        self.assertFalse(same("candidate_source_boundary", "same_candidate_source_boundary"))
        self.assertFalse(same("credvault dashboard 发布作业恢复", "credvault dashboard 安全删除凭证"))
        self.assertTrue(same("claude-mem 502", "claude-mem 502 错误"))   # 占比 ≥0.8 的包含仍算同物
        self.assertTrue(same("Writer Lock", "writer_lock"))

    def test_dry_run_resolves_without_writing(self):
        n = xi.normalize_extraction(ents(("kg-hub", "Project", ""), ("Brand New Thing", "Concept", "")), [],
                                    ENTITY_TYPES, EDGE_TYPES)
        d = FakeDriver(existing_names={"kg-hub": "u-kghub"})
        r = asyncio.run(xi.write_extraction(d, FakeEmbedder(), n, name="x", episode_body="b", source_description="sd",
                                            reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc),
                                            extractor_model="m", group_id="kg_hub", dry_run=True))
        self.assertEqual((r.nodes_new, r.nodes_reused, r.episode_uuid), (1, 1, ""))
        self.assertEqual(r.resolved["kg-hub"]["uuid"], "u-kghub")
        self.assertIsNone(r.resolved["brand new thing"]["uuid"])
        writes = [c for c, _ in d.calls if c.startswith(("MERGE", "MATCH (source", "MATCH (e:Episodic"))]
        self.assertEqual(writes, [])


class FakeEmbedder:
    async def create_batch(self, texts):
        return [[float(len(t))] * 3 for t in texts]


class FakeDriver:
    """按语句类型回放：存在的实体名 / 已有边由测试预置。"""

    def __init__(self, existing_names=None, similar=None, existing_edges=None):
        self.existing = {k.lower(): v for k, v in (existing_names or {}).items()}
        self.similar = similar or {}            # name_key -> (uuid, name)
        self.existing_edges = existing_edges or {}  # (src,name,dst) -> uuid
        self.calls = []

    async def execute_query(self, cypher, **params):
        self.calls.append((cypher, params))
        if cypher.startswith("MATCH (n:Entity) WHERE n.group_id = $g AND toLower(n.name)"):
            u = self.existing.get(params["lname"])
            return ([{"uuid": u, "name": params["lname"], "labels": ["Entity"]}] if u else []), None, None
        if "vec.cosineDistance(n.name_embedding" in cypher:
            # 用向量长度反查哪个实体（FakeEmbedder 用名字长度编码）
            for key, (u, name) in self.similar.items():
                if params["v"][0] == float(len(name)):
                    return [{"uuid": u, "name": name, "labels": ["Entity", "Tool"], "score": 0.97}], None, None
            return [], None, None
        if cypher.startswith("MATCH (a:Entity {uuid: $src})-[e:RELATES_TO"):
            u = self.existing_edges.get((params["src"], params["name"], params["dst"]))
            return ([{"uuid": u, "episodes": ["old-ep"]}] if u else []), None, None
        return [{"uuid": params.get("uuid") or "x"}], None, None


class WriteTest(unittest.TestCase):
    def _write(self, driver, extraction):
        return asyncio.run(xi.write_extraction(
            driver, FakeEmbedder(), extraction, name="claude-mem-obs-1", episode_body="body",
            source_description="claude-mem obs id=1", reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc),
            extractor_model="claude-fable-5-1", group_id="kg_hub", extractor_session="s1",
            prompt_version="v1", episode_uuid="ep-1"))

    def test_reuse_exact_similar_and_edges_with_provenance(self):
        n = xi.normalize_extraction(
            ents(("kg-hub", "Project", ""), ("FalkorDB", "Tool", "graph db"), ("Writer Lock", "Concept", "")),
            edges(("kg-hub", "depends_on", "FalkorDB", "kg-hub stores graph in FalkorDB"),
                  ("Writer Lock", "belongs_to", "kg-hub", "lock lives in kg-hub")),
            ENTITY_TYPES, EDGE_TYPES)
        d = FakeDriver(existing_names={"kg-hub": "u-kghub"},
                       similar={"falkordb": ("u-falkor", "falkordb")},
                       existing_edges={("u-kghub", "depends_on", "u-falkor"): "e-old"})
        r = self._write(d, n)
        self.assertEqual((r.nodes_new, r.nodes_reused, r.edges_new, r.edges_reused), (1, 2, 1, 1))
        self.assertEqual(r.resolved["kg-hub"], {"uuid": "u-kghub", "reused": True, "matched_name": "kg-hub"})
        self.assertEqual(r.resolved["falkordb"]["uuid"], "u-falkor")
        saved_nodes = [p for c, p in d.calls if c.startswith("MERGE (n:Entity")]
        self.assertEqual(len(saved_nodes), 1)
        node = saved_nodes[0]["entity_data"]
        self.assertEqual((node["name"], node["extractor_model"], node["group_id"]),
                         ("Writer Lock", "claude-fable-5-1", "kg_hub"))
        self.assertIn("SET n:Concept:Entity", [c for c, _ in d.calls if c.startswith("MERGE (n:Entity")][0])
        ep = [p for c, p in d.calls if c.startswith("MERGE (e:Episodic")][0]
        self.assertEqual((ep["extractor_model"], ep["extractor_session"], ep["prompt_version"], ep["valid_at"]),
                         ("claude-fable-5-1", "s1", "v1", "2026-10-17T00:00:00+00:00"))
        mentions = [p for c, p in d.calls if "MERGE (episode)-[e:MENTIONS" in c]
        self.assertEqual({m["entity_uuid"] for m in mentions}, {"u-kghub", "u-falkor", r.resolved["writer lock"]["uuid"]})
        appended = [p for c, p in d.calls if c.startswith("MATCH ()-[e:RELATES_TO {uuid: $uuid}]->()")]
        self.assertEqual(appended, [{"uuid": "e-old", "ep": "ep-1"}])
        new_edge = [p for c, p in d.calls if c.startswith("MATCH (source:Entity")][0]["edge_data"]
        self.assertEqual((new_edge["name"], new_edge["raw_name"], new_edge["episodes"], new_edge["extractor_model"]),
                         ("belongs_to", "belongs_to", ["ep-1"], "claude-fable-5-1"))
        self.assertTrue(all("vecf32" in c for c, _ in d.calls if c.startswith("MERGE (n:Entity") or c.startswith("MATCH (source:Entity")))
        link = [p for c, p in d.calls if c.startswith("MATCH (e:Episodic {uuid: $uuid}) SET e.entity_edges")][0]
        self.assertEqual(set(link["entity_edges"]), {"e-old", new_edge["uuid"]})

    def test_type_guard_blocks_cross_type_vector_reuse(self):
        n = xi.normalize_extraction(ents(("connection-status 监控", "Tool", "探针")), [], ENTITY_TYPES, EDGE_TYPES)

        class D(FakeDriver):
            async def execute_query(self, cypher, **params):
                if "vec.cosineDistance(n.name_embedding" in cypher:
                    return [{"uuid": "u-issue", "name": "connection-status 监控误报", "labels": ["Entity", "Issue"], "score": 0.97}], None, None
                return await super().execute_query(cypher, **params)
        r = asyncio.run(xi.write_extraction(D(), FakeEmbedder(), n, name="x", episode_body="b",
                                            source_description="sd", reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc),
                                            extractor_model="m", group_id="kg_hub", episode_uuid="ep"))
        self.assertEqual((r.nodes_new, r.nodes_reused), (1, 0))   # Issue 候选不能当 Tool 复用
        r = asyncio.run(xi.write_extraction(D(), FakeEmbedder(), n, name="x", episode_body="b",
                                            source_description="sd", reference_time=datetime(2026, 10, 17, tzinfo=timezone.utc),
                                            extractor_model="m", group_id="kg_hub", episode_uuid="ep", type_guard=False))
        self.assertEqual((r.nodes_new, r.nodes_reused), (0, 1))

    def test_check_keys(self):
        class D:
            async def execute_query(self, cypher, **params):
                return [{"sd": "a", "sid": "1", "status": "ok", "episode_uuid": "u"}], None, None
        got = asyncio.run(xi.check_keys(D(), [{"source_description": "a", "source_obs_id": "1"},
                                              {"source_description": "a", "source_obs_id": "2"}]))
        self.assertEqual(got, {("a", "1"): {"status": "ok", "episode_uuid": "u"}})
        self.assertEqual(asyncio.run(xi.check_keys(D(), [])), {})


if __name__ == "__main__":
    unittest.main()
