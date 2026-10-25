"""Opt-in real FalkorDB node-governance regressions, never the production graph.

Run only against a disposable loopback container using BOTH
KG_HUB_TEST_FALKOR_PORT and KG_HUB_TEST_FALKOR_ACK=temporary-loopback-fixture.
Each test owns a fresh t0362_governance_* graph and deletes it in teardown.
Normal offline runs explicitly skip instead of opening any database connection.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schema import EDGE_TYPES, ENTITY_TYPES
from utils import graph_governance as gov


ENABLED = bool(os.environ.get("KG_HUB_TEST_FALKOR_PORT"))
GROUP = "kg_hub"
VECTOR = [0.25] + [0.0] * 383


class Embedder:
    async def create_batch(self, texts):
        return [[0.5] + [0.0] * 383 for _ in texts]


class FaultDriver:
    """Delegate to the real graph; interrupt AFTER a selected actual mutation."""
    def __init__(self, driver, boundary):
        self.driver, self.boundary, self.fired = driver, boundary, False

    async def execute_query(self, query, **params):
        result = await self.driver.execute_query(query, **params)
        if not self.fired and "/* " + self.boundary + " */" in query:
            self.fired = True
            raise RuntimeError("interrupted after actual " + self.boundary)
        return result


class ReadOnlyGuard:
    def __init__(self, driver):
        self.driver, self.tags = driver, []

    async def execute_query(self, query, **params):
        import re
        tag = re.search(r"/\* (gov:[^ ]+) \*/", query)
        self.tags.append(tag[1] if tag else query)
        if not tag or tag[1] not in gov._READ_TAGS:
            raise AssertionError("preview issued a non-read query: " + query[:80])
        return await self.driver.execute_query(query, **params)


@unittest.skipUnless(ENABLED, "opt-in disposable FalkorDB port and acknowledgement required")
class RealGovernanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if os.environ.get("KG_HUB_TEST_FALKOR_ACK") != "temporary-loopback-fixture":
            self.fail("explicit disposable-fixture acknowledgement is required")
        port = int(os.environ["KG_HUB_TEST_FALKOR_PORT"])
        if not 1024 < port < 65536 or port == 6379:
            self.fail("use a random non-default loopback test-container port")
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        self.database = "t0362_governance_" + uuid.uuid4().hex
        self.driver = FalkorDriver(host="127.0.0.1", port=port, database=self.database)
        await self.driver.build_indices_and_constraints()
        self.g = SimpleNamespace(driver=self.driver, embedder=Embedder())

    async def asyncTearDown(self):
        try:
            await self.driver.client.select_graph(self.database).delete()
        finally:
            await self.driver.close()

    async def rows(self, query, **params):
        rows, _, _ = await self.driver.execute_query(query, **params)
        return rows

    async def dump(self):
        nodes = await self.rows("MATCH (n) RETURN labels(n) AS l, properties(n) AS p ORDER BY n.uuid")
        rels = await self.rows("MATCH (a)-[r]->(b) RETURN type(r) AS t, a.uuid AS a, b.uuid AS b, "
                               "properties(r) AS p ORDER BY t, a, b, r.uuid")
        return repr(nodes) + repr(rels)

    async def node(self, uid, kind, name, summary=""):
        await self.rows("CREATE (n:Entity:" + kind + ") SET n=$p, n.name_embedding=vecf32($v)",
                        p={"uuid": uid, "name": name, "group_id": GROUP, "summary": summary,
                           "created_at": "2026-10-01T00:00:00+00:00"}, v=VECTOR)

    async def edge(self, uid, src, dst, fact, episodes, created, name="relates_to"):
        await self.rows("MATCH (a:Entity {uuid:$src}),(b:Entity {uuid:$dst}) CREATE (a)-[r:RELATES_TO]->(b) "
                        "SET r=$p, r.fact_embedding=vecf32($v)", src=src, dst=dst, v=[0.125] * 384,
                        p={"uuid": uid, "group_id": GROUP, "name": name, "fact": fact, "episodes": episodes,
                           "created_at": created, "valid_at": created, "extractor_model": "origin-" + uid})

    async def episode(self, uid, edges, mentions):
        await self.rows("CREATE (e:Episodic) SET e=$p", p={"uuid": uid, "name": "obs-" + uid, "group_id": GROUP,
                        "source_description": "claude-mem:" + uid, "content": "body", "entity_edges": edges,
                        "valid_at": "2026-10-01T00:00:00+00:00"})
        for node in mentions:
            await self.rows("MATCH (e:Episodic {uuid:$e}),(n:Entity {uuid:$n}) CREATE (e)-[m:MENTIONS]->(n) "
                            "SET m.uuid=$m, m.group_id=$g", e=uid, n=node, m=f"m-{uid}-{node}", g=GROUP)

    async def merge_fixture(self):
        await self.node("canon", "File", "credvault:reconciliation_mailbox.py", "credvault mailbox")
        await self.node("dup", "File", "reconciliation_mailbox.py", "bare name")
        await self.node("x", "Project", "CredVault")
        await self.node("y", "Concept", "ReconciliationMailbox")
        await self.edge("old-dup", "dup", "x", "The mailbox belongs to credvault.", ["e1"], "2026-09-01T00:00:00Z", "belongs_to")
        await self.edge("new-canon", "canon", "x", "The mailbox belongs to credvault.", ["e2"], "2026-10-20T00:00:00Z", "belongs_to")
        await self.edge("dup-y", "dup", "y", "The file defines ReconciliationMailbox.", ["e1"], "2026-09-02T00:00:00Z")
        await self.edge("loop", "canon", "dup", "The qualified and bare names are the same file.", ["e3"], "2026-10-21T00:00:00Z")
        await self.episode("e1", ["old-dup", "dup-y"], ["dup", "x", "y"])
        await self.episode("e2", ["new-canon"], ["canon", "x"])
        await self.episode("e3", ["loop"], ["canon", "dup"])

    async def reviewed(self, scope, gid="merge-1"):
        body = await gov.preview(self.g, scope, GROUP)
        return {**body["scope"], "governance_id": gid, "reason": "same file", "reviewer": "unit",
                "expected_snapshot_sha256": body["snapshot"]["snapshot_sha256"]}, body

    async def test_merge_preview_is_read_only_and_apply_preserves_origin(self):
        await self.merge_fixture()
        scope = {"operation": "merge", "canonical": {"name": "credvault:reconciliation_mailbox.py"},
                 "merged": [{"name": "Reconciliation_Mailbox.py"}]}
        before = await self.dump()
        guard = ReadOnlyGuard(self.driver)
        body = await gov.preview(SimpleNamespace(driver=guard, embedder=Embedder()), scope, GROUP)
        self.assertEqual(before, await self.dump())
        plan = body["plan"]
        actions = {e["uuid"]: e["action"] for e in plan["edges"]}
        self.assertEqual(actions, {"old-dup": "move", "new-canon": "absorb", "dup-y": "move", "loop": "drop_self_loop"})
        survivor = next(e for e in plan["edges"] if e["uuid"] == "old-dup")
        self.assertEqual(survivor["episodes_after"], ["e1", "e2"])
        self.assertEqual({s["episode_uuid"] for s in plan["sources"]}, {"e1", "e2", "e3"})
        original = (await self.rows("MATCH ()-[r:RELATES_TO {uuid:'old-dup'}]->() RETURN properties(r) AS p"))[0]["p"]

        request, _ = await self.reviewed(scope)
        result = await gov.apply(self.g, request, GROUP)
        self.assertTrue(result["verified"])
        self.assertEqual(result["plan_sha256"], plan["plan_sha256"])
        moved = await self.rows("MATCH (a:Entity)-[r:RELATES_TO {uuid:'old-dup'}]->(b:Entity) "
                                "RETURN a.uuid AS a, b.uuid AS b, properties(r) AS p")
        self.assertEqual((moved[0]["a"], moved[0]["b"]), ("canon", "x"))
        self.assertEqual(moved[0]["p"]["episodes"], ["e1", "e2"])
        self.assertEqual({k: v for k, v in moved[0]["p"].items() if k != "episodes"},
                         {k: v for k, v in original.items() if k != "episodes"})  # vector and first-source kept
        self.assertEqual(await self.rows("MATCH ()-[r:RELATES_TO]->() WHERE r.uuid IN ['new-canon','loop'] RETURN r.uuid"), [])
        tomb = await self.rows("MATCH (n {uuid:'dup'}) RETURN labels(n) AS l, n.merged_into AS into, n.summary AS s")
        self.assertEqual((tomb[0]["l"], tomb[0]["into"], tomb[0]["s"]), (["MergedEntity"], "canon", "bare name"))
        refs = {r["u"]: r["e"] for r in await self.rows("MATCH (e:Episodic) RETURN e.uuid AS u, e.entity_edges AS e")}
        self.assertEqual(refs, {"e1": ["old-dup", "dup-y"], "e2": ["old-dup"], "e3": []})
        mentions = {(r["e"], r["n"]) for r in await self.rows(
            "MATCH (e:Episodic)-[:MENTIONS]->(n) RETURN e.uuid AS e, n.uuid AS n")}
        self.assertEqual(mentions, {("e1", "canon"), ("e1", "x"), ("e1", "y"), ("e2", "canon"), ("e2", "x"), ("e3", "canon")})
        canon = (await self.rows("MATCH (n:Entity {uuid:'canon'}) RETURN properties(n) AS p"))[0]["p"]
        self.assertEqual((canon["merged_from"], canon["merged_names"], canon["governance_ids"]),
                         (["dup"], ["reconciliation_mailbox.py"], ["merge-1"]))
        audit = await gov.audit(self.driver, "merge-1")
        self.assertEqual(audit["state"], "applied")
        self.assertEqual({e["props"]["uuid"] for e in audit["deleted"]}, {"new-canon", "loop"})
        self.assertEqual(len(audit["deleted"][0]["props"]["fact_embedding"]), 384)
        again = await gov.apply(self.g, request, GROUP)
        self.assertTrue(again["duplicate"])
        with self.assertRaises(gov.GovernanceError) as caught:
            await gov.preview(self.g, scope, GROUP)
        self.assertEqual(caught.exception.code, "node_not_found")  # the tombstone is invisible to name lookup

    async def test_stale_snapshot_is_refused_without_writes(self):
        await self.merge_fixture()
        request, _ = await self.reviewed({"operation": "merge", "canonical": {"name": "credvault:reconciliation_mailbox.py"},
                                          "merged": [{"name": "reconciliation_mailbox.py"}]})
        await self.edge("late", "dup", "y", "A later ingest wrote this.", ["e9"], "2026-10-24T00:00:00Z")
        before = await self.dump()
        with self.assertRaises(gov.GovernanceError) as caught:
            await gov.apply(self.g, request, GROUP)
        self.assertEqual(caught.exception.code, "stale_snapshot")
        self.assertEqual(before, await self.dump())

    async def test_interrupted_apply_replays_the_frozen_plan(self):
        for boundary in ("gov:edge-move", "gov:edge-absorb", "gov:mention-move", "gov:node-update"):
            with self.subTest(boundary=boundary):
                await self.rows("MATCH (n) DETACH DELETE n")
                await self.merge_fixture()
                request, _ = await self.reviewed({"operation": "merge", "canonical": {"uuid": "canon"},
                                                  "merged": [{"uuid": "dup"}]}, gid="replay-" + boundary.split(":")[1])
                faulty = SimpleNamespace(driver=FaultDriver(self.driver, boundary), embedder=Embedder())
                with self.assertRaises(RuntimeError):
                    await gov.apply(faulty, request, GROUP)
                self.assertEqual((await gov.audit(self.driver, request["governance_id"]))["state"], "pending")
                result = await gov.apply(self.g, request, GROUP)
                self.assertTrue(result["verified"])
                self.assertFalse(result["duplicate"])

    async def test_split_mixed_repository_node_by_source(self):
        await self.node("pom", "File", "pom.xml")
        await self.node("sd", "Project", "sd-server")
        await self.node("libtv", "Project", "libtv-m")
        await self.node("dep", "Concept", "spring-boot 3.2")
        await self.edge("a", "pom", "sd", "pom.xml belongs to sd-server.", ["ea"], "2026-09-01T00:00:00Z", "belongs_to")
        await self.edge("b", "pom", "libtv", "pom.xml belongs to libtv-m.", ["eb"], "2026-09-02T00:00:00Z", "belongs_to")
        await self.edge("c", "pom", "dep", "pom.xml pins spring-boot 3.2.", ["ea", "eb"], "2026-09-03T00:00:00Z")
        await self.episode("ea", ["a", "c"], ["pom", "sd", "dep"])
        await self.episode("eb", ["b", "c"], ["pom", "libtv", "dep"])
        scope = {"operation": "split", "node": {"name": "pom.xml"}, "rename_to": "sd-server:pom.xml",
                 "partitions": [{"new_node": {"name": "libtv-m:pom.xml", "summary": "libtv-m build file"},
                                 "episode_uuids": ["eb"]}]}
        with self.assertRaises(gov.GovernanceError) as caught:
            await gov.preview(self.g, scope, GROUP)
        self.assertEqual(caught.exception.code, "ambiguous_edge")   # edge c is endorsed by both repositories
        scope["keep_edge_uuids"] = ["c"]
        request, body = await self.reviewed(scope, gid="split-pom")
        self.assertEqual(body["plan"]["residual_edges"], ["a", "c"])
        result = await gov.apply(self.g, request, GROUP)
        self.assertTrue(result["verified"])
        new_uuid = next(n["uuid"] for n in body["plan"]["nodes"] if n["action"] == "create")
        where = {r["u"]: (r["a"], r["b"]) for r in await self.rows(
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) RETURN r.uuid AS u, a.uuid AS a, b.uuid AS b")}
        self.assertEqual(where, {"a": ("pom", "sd"), "b": (new_uuid, "libtv"), "c": ("pom", "dep")})
        names = {r["u"]: (r["n"], r["l"]) for r in await self.rows(
            "MATCH (n:Entity) WHERE n.uuid IN ['pom', $new] RETURN n.uuid AS u, n.name AS n, labels(n) AS l", new=new_uuid)}
        self.assertEqual(names["pom"][0], "sd-server:pom.xml")
        self.assertEqual(names[new_uuid], ("libtv-m:pom.xml", ["Entity", "File"]))
        mentions = {(r["e"], r["n"]) for r in await self.rows(
            "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) WHERE n.uuid IN ['pom', $new] RETURN e.uuid AS e, n.uuid AS n",
            new=new_uuid)}
        self.assertEqual(mentions, {("ea", "pom"), ("eb", "pom"), ("eb", new_uuid)})  # eb still endorses c on pom

    async def test_retype_and_rename_conflict(self):
        await self.node("znas", "Person", "ZNAS3")
        await self.node("other", "Tool", "deploy-standard skill")
        await self.node("dup", "Tool", "deploy-standard（部署准则 skill）")
        request, body = await self.reviewed({"operation": "retype", "node": {"name": "znas3"}, "retype_to": "Tool"},
                                            gid="retype-znas")
        self.assertEqual(body["plan"]["nodes"][0]["labels_remove"], ["Person"])
        await gov.apply(self.g, request, GROUP)
        labels = (await self.rows("MATCH (n:Entity {uuid:'znas'}) RETURN labels(n) AS l, n.previous_types AS t"))[0]
        self.assertEqual((sorted(labels["l"]), labels["t"]), (["Entity", "Tool"], ["Person"]))
        with self.assertRaises(gov.GovernanceError) as caught:
            await gov.preview(self.g, {"operation": "rename", "node": {"uuid": "dup"},
                                       "rename_to": "Deploy-Standard skill"}, GROUP)
        self.assertEqual(caught.exception.code, "name_conflict")

    async def ingest(self, *names, kind="File", dry_run=False):
        from datetime import datetime, timezone
        from utils import extracted_ingest as xi
        extraction = xi.normalize_extraction([xi.ExtractedEntity(name=n, type=kind, summary="") for n in names], [],
                                             set(ENTITY_TYPES), set(EDGE_TYPES))
        return await xi.write_extraction(self.driver, Embedder(), extraction, name="obs-late", episode_body="b",
                                         source_description="claude-mem:late",
                                         reference_time=datetime(2026, 10, 26, tzinfo=timezone.utc),
                                         extractor_model="unit", group_id=GROUP, episode_uuid="late",
                                         dry_run=dry_run)

    async def test_ingest_reuses_canonical_for_merged_and_renamed_names(self):
        """合并 / 改名之后，入图消解（_FIND_ALIAS / _FIND_RETIRED 在真 FalkorDB 上）认旧名，不再长回重复节点。"""
        await self.merge_fixture()
        request, _ = await self.reviewed({"operation": "merge", "canonical": {"uuid": "canon"},
                                          "merged": [{"uuid": "dup"}]})
        await gov.apply(self.g, request, GROUP)
        preview = await self.ingest("Reconciliation_Mailbox.py", dry_run=True)
        hit = preview.resolved["reconciliation_mailbox.py"]
        self.assertEqual((hit["uuid"], hit["match"], hit["alias_via"], hit["matched_alias"]),
                         ("canon", "alias", "merged_names", "reconciliation_mailbox.py"))
        # 近名不吃：被并节点的向量（与规范节点同向量）也不会被 _FIND_SIMILAR 看到
        near = await self.ingest("reconciliation_mailboxes.py", dry_run=True)
        self.assertFalse(near.resolved["reconciliation_mailboxes.py"]["reused"])
        before = await self.rows("MATCH (n:Entity) RETURN count(n) AS c")
        result = await self.ingest("reconciliation_mailbox.py")
        self.assertEqual((result.nodes_new, result.nodes_reused), (0, 1))
        self.assertEqual(await self.rows("MATCH (n:Entity) RETURN count(n) AS c"), before)
        self.assertEqual(await self.rows("MATCH (:Episodic {uuid:'late'})-[:MENTIONS]->(n) RETURN n.uuid AS u"),
                         [{"u": "canon"}])

        # 改名留下的 previous_names；再把改过名的节点并掉，连环旧名顺 merged_into 走到最终规范节点
        await self.node("job", "File", "Publication_Jobs.py")   # 存着的旧名带大写：两边都要 toLower
        request, _ = await self.reviewed({"operation": "rename", "node": {"uuid": "job"},
                                          "rename_to": "deploy/publication_jobs.py"}, gid="rename-job")
        await gov.apply(self.g, request, GROUP)
        hit = (await self.ingest("PUBLICATION_JOBS.PY", dry_run=True)).resolved["publication_jobs.py"]
        self.assertEqual((hit["uuid"], hit["alias_via"], hit["matched_alias"]),
                         ("job", "previous_names", "Publication_Jobs.py"))
        await self.node("job2", "File", "deploy/publication-jobs module")
        request, _ = await self.reviewed({"operation": "merge", "canonical": {"uuid": "job2"},
                                          "merged": [{"uuid": "job"}]}, gid="merge-job")
        await gov.apply(self.g, request, GROUP)
        hit = (await self.ingest("publication_jobs.py", dry_run=True)).resolved["publication_jobs.py"]
        self.assertEqual((hit["uuid"], hit["alias_via"], hit["matched_alias"]),
                         ("job2", "merged_into", "Publication_Jobs.py"))

        # 现存同名 Entity 优先于别名；dry-run 标出被挡住的规范节点
        await self.node("regrown", "File", "reconciliation_mailbox.py")
        hit = (await self.ingest("reconciliation_mailbox.py", dry_run=True)).resolved["reconciliation_mailbox.py"]
        self.assertEqual((hit["uuid"], hit["match"], hit["shadows_alias_of"]),
                         ("regrown", "exact", [{"uuid": "canon", "name": "credvault:reconciliation_mailbox.py"}]))


if __name__ == "__main__":
    unittest.main()
