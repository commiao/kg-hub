"""Opt-in real FalkorDB repair regressions, never the production/default graph.

Run only against a disposable loopback container using BOTH
KG_HUB_TEST_FALKOR_PORT and KG_HUB_TEST_FALKOR_ACK=temporary-loopback-fixture.
Each test owns a fresh t0358_verification_* graph and deletes it in teardown.
Normal offline runs explicitly skip instead of opening any database connection.
"""
from __future__ import annotations

from copy import deepcopy
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import extraction_repair as repair


ENABLED = bool(os.environ.get("KG_HUB_TEST_FALKOR_PORT"))
VECTOR = [1.0] + [0.0] * 383
GROUP = "kg_hub"
BODY = "The Component implementation belongs to claude-mem; its supported observer is Integration Observer."


class Embedder:
    async def create_batch(self, texts):
        return [list(VECTOR) for _ in texts]


class FaultDriver:
    """Delegate to the real graph; interrupt AFTER a selected actual mutation."""
    def __init__(self, driver, boundary):
        self.driver, self.boundary = driver, boundary
        self.fired = False

    async def execute_query(self, query, **params):
        result = await self.driver.execute_query(query, **params)
        if not self.fired and "/* " + self.boundary + " */" in query:
            self.fired = True
            raise RuntimeError("interrupted after actual " + self.boundary)
        return result


@unittest.skipUnless(ENABLED, "opt-in disposable FalkorDB port and acknowledgement required")
class RealRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if os.environ.get("KG_HUB_TEST_FALKOR_ACK") != "temporary-loopback-fixture":
            self.fail("explicit disposable-fixture acknowledgement is required")
        port = int(os.environ["KG_HUB_TEST_FALKOR_PORT"])
        if not 1024 < port < 65536 or port == 6379:
            self.fail("use a random non-default loopback test-container port")
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        self.database = "t0358_verification_" + uuid.uuid4().hex
        self.driver = FalkorDriver(host="127.0.0.1", port=port, database=self.database)
        # Await the driver's actual index setup, rather than relying on timing.
        await self.driver.build_indices_and_constraints()
        self.g = SimpleNamespace(driver=self.driver, embedder=Embedder())

    async def asyncTearDown(self):
        try:
            await self.driver.client.select_graph(self.database).delete()
            remaining = await self.driver.client.connection.execute_command("GRAPH.LIST")
            self.assertNotIn(self.database, [x.decode() if isinstance(x, bytes) else x for x in remaining])
        finally:
            await self.driver.close()

    async def rows(self, query, **params):
        rows, _, _ = await self.driver.execute_query(query, **params)
        return rows

    async def fixture(self, *, shared=False, empty=False):
        await self.rows("MATCH (n) DETACH DELETE n")
        key = {"source_description": "fixture-source", "source_obs_id": "one", "name": "fixture-observation",
               "status": "empty" if empty else "ok", "empty_by_model": "original-fixture-model",
               "empty_by_tool": "codex", "empty_run_id": "fixture-run", "nodes": 0 if empty else 3,
               "edges": 0 if empty else 2}
        if not empty:
            key["episode_uuid"] = "episode"
        await self.rows("CREATE (k:IngestedKey) SET k=$props", props=key)
        for uid, name, kind in (("component", "Component", "Tool"),
                                ("wrong-project", "credvault", "Project"),
                                ("right-project", "claude-mem", "Project")):
            await self.rows("CREATE (n:Entity:" + kind + ") SET n=$props, n.name_embedding=vecf32($v)",
                props={"uuid": uid, "name": name, "group_id": GROUP, "summary": "original supported summary"}, v=VECTOR)
        if empty:
            return
        await self.rows("CREATE (e:Episodic) SET e=$props", props={"uuid": "episode", "name": "fixture-observation",
            "group_id": GROUP, "content": BODY, "source_description": "fixture-source",
            "extractor_model": "original-fixture-model", "extractor_tool": "codex", "run_id": "fixture-run",
            "prompt_version": "agent-v4", "entity_edges": ["wrong", "keep"], "custom_metadata": "keep",
            "valid_at": "2026-10-07T00:00:00+00:00"})
        for node in ("component", "wrong-project", "right-project"):
            await self.rows("MATCH (e:Episodic {uuid:'episode'}),(n:Entity {uuid:$node}) "
                            "CREATE (e)-[m:MENTIONS {uuid:$mid}]->(n)", node=node, mid="original-" + node)
        for uid, target, relation, fact, episodes in (
            ("wrong", "wrong-project", "belongs_to", "Wrong original fixture attribution", ["episode", "other"] if shared else ["episode"]),
            ("keep", "right-project", "relates_to", "A separate supported historical fixture mechanism", ["episode"]),
        ):
            await self.rows("MATCH (a:Entity {uuid:'component'}),(b:Entity {uuid:$target}) "
                            "CREATE (a)-[r:RELATES_TO]->(b) SET r=$props,r.fact_embedding=vecf32($v)",
                target=target, props={"uuid": uid, "group_id": GROUP, "name": relation, "fact": fact, "episodes": episodes}, v=VECTOR)

    async def request(self, rid="fixture-repair", *, observer=False):
        snap = await repair.snapshot(self.driver, "fixture-source", "one", GROUP)
        payload = {"name": "fixture-observation", "source_description": "fixture-source", "source_obs_id": "one",
            "episode_body": BODY, "reference_time": "2026-10-07T00:00:00Z", "extractor_model": "fixture-corrector",
            "extractor_tool": "codex", "run_id": "fixture-run", "prompt_version": "agent-v5",
            "entities": [{"name": "Component", "type": "Tool", "summary": "supported fixture component"},
                         {"name": "claude-mem", "type": "Project", "summary": "supported fixture project"}],
            "edges": [{"source": "Component", "target": "claude-mem", "name": "belongs_to",
                       "fact": "Component implementation belongs to claude-mem according to the historical fixture source."}]}
        if observer:
            payload["entities"].append({"name": "Integration Observer", "type": "Tool", "summary": "supported fixture observer"})
            payload["edges"].append({"source": "Component", "target": "Integration Observer", "name": "relates_to",
                                     "fact": "Component uses Integration Observer as a supported observer in the fixture source."})
        return {"repair_id": rid, "source_description": "fixture-source", "source_obs_id": "one",
            "expected_snapshot_sha256": snap["snapshot_sha256"], "reason": "Correct fixture recording-label attribution",
            "reviewer": "independent integration verifier", "remove_edge_uuids": [] if snap["key"]["status"] == "empty" else ["wrong"],
            "replacement": payload}

    async def counts(self):
        nodes = await self.rows("MATCH (n) RETURN count(n) AS n")
        edges = await self.rows("MATCH ()-[r]->() RETURN count(r) AS n")
        return nodes[0]["n"], edges[0]["n"]

    async def test_preview_is_read_only_on_real_graph(self):
        await self.fixture()
        before = await self.counts()
        result = await repair.preview(self.g, {"source_description": "fixture-source", "source_obs_id": "one"}, GROUP,
                                      quality_validator=lambda p: [])
        self.assertEqual(result["snapshot"]["episode"]["custom_metadata"], "keep")
        self.assertEqual(before, await self.counts())
        self.assertEqual(len(result["snapshot"]["edges"]), 2)

    async def test_shared_edge_preserves_other_endorsements_and_original_source(self):
        await self.fixture(shared=True)
        before = await repair.snapshot(self.driver, "fixture-source", "one", GROUP)
        req = await self.request()
        result = await repair.apply(self.g, req, GROUP, quality_validator=lambda p: [])
        self.assertTrue(result["verified"])
        proof = result["shared_edge_verification"]
        self.assertEqual(proof["status"], "verified")
        self.assertEqual(proof["before_snapshot_sha256"], before["snapshot_sha256"])
        self.assertEqual(proof["count"], 1)
        self.assertEqual(proof["edges"][0]["uuid"], "wrong")
        wrong = await self.rows("MATCH ()-[r:RELATES_TO {uuid:'wrong'}]->() RETURN r.episodes AS episodes")
        self.assertEqual(wrong, [{"episodes": ["other"]}])
        after = await repair.snapshot(self.driver, "fixture-source", "one", GROUP)
        for field in ("name", "content", "source_description", "extractor_model", "extractor_tool", "run_id", "prompt_version", "custom_metadata"):
            self.assertEqual(after["episode"][field], before["episode"][field])
        self.assertNotIn("wrong", after["episode"]["entity_edges"])
        self.assertEqual(len(after["edges"]), 2)
        saved = await repair.audit(self.driver, req["repair_id"])
        self.assertEqual(saved["state"], "applied")
        self.assertEqual(saved["before"]["episode"], before["episode"])

    async def test_shared_other_source_corruption_blocks_success_on_real_graph(self):
        for corruption in ("missing", "episodes", "fact", "endpoint"):
            with self.subTest(corruption=corruption):
                await self.fixture(shared=True)
                request = await self.request("corrupt-" + corruption)
                driver = self.driver
                mutations = {
                    "missing": "MATCH ()-[r:RELATES_TO {uuid:'wrong'}]->() DELETE r",
                    "episodes": "MATCH ()-[r:RELATES_TO {uuid:'wrong'}]->() SET r.episodes=['other-changed']",
                    "fact": "MATCH ()-[r:RELATES_TO {uuid:'wrong'}]->() SET r.fact='tampered original fact'",
                    "endpoint": "MATCH (a:Entity)-[r:RELATES_TO {uuid:'wrong'}]->() "
                        "WITH a,properties(r) AS props,r DELETE r WITH a,props "
                        "MATCH (b:Entity {uuid:'right-project'}) CREATE (a)-[r:RELATES_TO]->(b) SET r=props",
                }
                class CorruptSharedReadback:
                    async def execute_query(self, query, **params):
                        if "/* repair:shared-readback */" in query:
                            await driver.execute_query(mutations[corruption])
                        return await driver.execute_query(query, **params)
                g = SimpleNamespace(driver=CorruptSharedReadback(), embedder=Embedder())
                with self.assertRaisesRegex(repair.RepairError, "shared fact"):
                    await repair.apply(g, request, GROUP, quality_validator=lambda payload: [])
                saved = await repair.audit(self.driver, request["repair_id"])
                self.assertEqual(saved["state"], "pending")
                self.assertNotIn("result", saved)

    async def test_orphan_removal_new_node_vectors_and_audit_materialize(self):
        await self.fixture()
        # Exercise the documented FalkorDB vector-index API as well as raw
        # vector properties: https://falkordb.github.io/docs-staging/cypher/indexing/vector-index/
        graph = self.driver.client.select_graph(self.database)
        await graph.create_node_vector_index("Entity", "name_embedding", dim=384, similarity_function="cosine")
        await graph.create_edge_vector_index("RELATES_TO", "fact_embedding", dim=384, similarity_function="cosine")
        req = await self.request(observer=True)
        result = await repair.apply(self.g, req, GROUP, quality_validator=lambda p: [])
        self.assertTrue(result["verified"])
        self.assertEqual(await self.rows("MATCH ()-[r:RELATES_TO {uuid:'wrong'}]->() RETURN r.uuid AS uuid"), [])
        actual = await self.rows("MATCH (n:Entity {name:'Integration Observer'}) RETURN vec.cosineDistance(n.name_embedding,vecf32($v)) AS distance", v=VECTOR)
        self.assertAlmostEqual(actual[0]["distance"], 0.0, places=5)
        saved = await repair.audit(self.driver, req["repair_id"])
        self.assertEqual(len(saved["before"]["edges"]), 2)
        for edge in saved["plan"]["edges"]:
            rows = await self.rows("MATCH ()-[r:RELATES_TO {uuid:$uid}]->() RETURN r.fact AS fact, "
                "vec.cosineDistance(r.fact_embedding,vecf32($v)) AS distance", uid=edge["uuid"], v=VECTOR)
            self.assertEqual(rows[0]["fact"], edge["fact"])
            self.assertAlmostEqual(rows[0]["distance"], 0.0, places=5)
        indexed_nodes = await self.rows("CALL db.idx.vector.queryNodes('Entity','name_embedding',10,vecf32($v)) "
                                       "YIELD node RETURN node.name AS name", v=VECTOR)
        self.assertIn("Integration Observer", {r["name"] for r in indexed_nodes})
        indexed_edges = await self.rows("CALL db.idx.vector.queryRelationships('RELATES_TO','fact_embedding',10,vecf32($v)) "
                                       "YIELD relationship RETURN relationship.uuid AS uuid", v=VECTOR)
        self.assertTrue({e["uuid"] for e in saved["plan"]["edges"]}.issubset({r["uuid"] for r in indexed_edges}))
        self.assertNotIn("wrong", {r["uuid"] for r in indexed_edges})

    async def test_empty_upgrade_preserves_key_and_prior_model(self):
        await self.fixture(empty=True)
        req = await self.request()
        result = await repair.apply(self.g, req, GROUP, quality_validator=lambda p: [])
        self.assertTrue(result["upgraded_empty"])
        snap = await repair.snapshot(self.driver, "fixture-source", "one", GROUP)
        self.assertEqual(snap["key"]["status"], "ok")
        self.assertEqual(snap["key"]["source_obs_id"], "one")
        self.assertEqual(snap["key"]["empty_by_model"], "original-fixture-model")
        self.assertEqual(snap["episode"]["extractor_model"], "original-fixture-model")
        self.assertEqual(snap["episode"]["content"], BODY)
        self.assertTrue(snap["episode"]["original_empty"])

    async def test_request_replay_conflict_and_stale_cas(self):
        await self.fixture()
        req = await self.request()
        first = await repair.apply(self.g, req, GROUP, quality_validator=lambda p: [])
        counts = await self.counts()
        second = await repair.apply(self.g, req, GROUP, quality_validator=lambda p: [])
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(counts, await self.counts())
        wrong = deepcopy(req)
        wrong["reason"] = "different operation"
        with self.assertRaisesRegex(repair.RepairError, "another request"):
            await repair.apply(self.g, wrong, GROUP, quality_validator=lambda p: [])
        fresh = await self.request("stale-repair")
        fresh["remove_edge_uuids"] = []
        await self.rows("MATCH (e:Episodic {uuid:'episode'}) SET e.content=e.content+' changed'")
        with self.assertRaisesRegex(repair.RepairError, "changed after preview"):
            await repair.apply(self.g, fresh, GROUP, quality_validator=lambda p: [])
        self.assertIsNone(await repair.audit(self.driver, "stale-repair"))

    async def test_existing_reference_time_is_preserved_with_timezone_equivalence(self):
        await self.fixture()
        req = await self.request("changed-time")
        req["replacement"]["reference_time"] = "2026-10-08T00:00:00Z"
        before = await self.counts()
        with self.assertRaises(repair.RepairError):
            await repair.apply(self.g, req, GROUP, quality_validator=lambda p: [])
        self.assertEqual(before, await self.counts())
        self.assertIsNone(await repair.audit(self.driver, "changed-time"))
        for value in ("2026-10-07T00:00:00Z", "2026-10-07T02:00:00+02:00"):
            with self.subTest(value=value):
                await self.fixture()
                equivalent = await self.request("equivalent-time")
                equivalent["replacement"]["reference_time"] = value
                result = await repair.apply(self.g, equivalent, GROUP, quality_validator=lambda p: [])
                self.assertTrue(result["verified"])
                after = await repair.snapshot(self.driver, "fixture-source", "one", GROUP)
                self.assertEqual(after["episode"]["valid_at"], "2026-10-07T00:00:00+00:00")

    async def test_source_key_counters_match_actual_distinct_source_relationships(self):
        for empty, observer, expected in ((False, False, (2, 2)), (False, True, (3, 3)), (True, False, (2, 1))):
            with self.subTest(empty=empty, observer=observer):
                await self.fixture(empty=empty)
                req = await self.request(observer=observer)
                result = await repair.apply(self.g, req, GROUP, quality_validator=lambda p: [])
                self.assertTrue(result["verified"])
                after = await repair.snapshot(self.driver, "fixture-source", "one", GROUP)
                ep = after["episode"]["uuid"]
                nodes = await self.rows("MATCH (e:Episodic {uuid:$ep})-[:MENTIONS]->(n:Entity) "
                                        "RETURN count(DISTINCT n) AS n", ep=ep)
                edges = await self.rows("MATCH ()-[r:RELATES_TO]->() WHERE $ep IN coalesce(r.episodes,[]) "
                                        "RETURN count(DISTINCT r) AS n", ep=ep)
                actual = (nodes[0]["n"], edges[0]["n"])
                self.assertEqual(actual, expected)
                self.assertEqual((after["key"]["nodes"], after["key"]["edges"]), actual)

    async def test_interruption_after_real_mutation_can_replay_original_plan(self):
        for boundary in ("repair:claim", "repair:edge", "repair:detach", "repair:episode-finish", "repair:complete", "repair:release"):
            with self.subTest(boundary=boundary):
                await self.fixture()
                req = await self.request("interrupted-" + boundary.split(":")[1])
                proxy = FaultDriver(self.driver, boundary)
                interrupted = SimpleNamespace(driver=proxy, embedder=Embedder())
                with self.assertRaisesRegex(RuntimeError, "interrupted after actual"):
                    await repair.apply(interrupted, req, GROUP, quality_validator=lambda p: [])
                self.assertTrue(proxy.fired)
                result = await repair.apply(self.g, req, GROUP, quality_validator=lambda p: [])
                self.assertTrue(result["verified"])
                self.assertEqual(len((await repair.snapshot(self.driver, "fixture-source", "one", GROUP))["edges"]), 2)
                self.assertEqual((await repair.audit(self.driver, req["repair_id"]))["state"], "applied")

    async def test_existing_mentions_are_not_duplicated_by_first_or_followup_repairs(self):
        await self.fixture()
        async def counts():
            return await self.rows("MATCH (e:Episodic {uuid:'episode'})-[m:MENTIONS]->(n:Entity) "
                                   "RETURN n.uuid AS node,count(m) AS count ORDER BY node")
        before = await counts()
        first = await self.request()
        await repair.apply(self.g, first, GROUP, quality_validator=lambda p: [])
        after_first = await counts()
        await repair.apply(self.g, first, GROUP, quality_validator=lambda p: [])
        after_replay = await counts()
        second = await self.request("followup-repair")
        second["remove_edge_uuids"] = []
        await repair.apply(self.g, second, GROUP, quality_validator=lambda p: [])
        after_followup = await counts()
        self.assertTrue(all(row["count"] == 1 for row in before))
        self.assertEqual([row["count"] for row in after_first], [1, 1], after_first)
        self.assertEqual(after_first, after_replay)
        self.assertEqual(after_first, after_followup)

    async def test_http_v5_preview_apply_and_status_use_real_graph(self):
        # ASGITransport deliberately does not start the application's lifespan;
        # every database getter and the writer lock are isolated below.
        import httpx
        with patch.dict(os.environ, {"KG_HUB_API_TOKEN": "synthetic-admin-token"}):
            import kg_hub_server as server
        from utils import extraction_quality as quality
        await self.fixture()
        req = await self.request("http-roundtrip", observer=True)
        payload = req["replacement"]
        payload["edges"][0:0] = [
            {"source": "Component", "target": "claude-mem", "name": "belongs_to", "fact": BODY},
            {"source": "Component", "target": "claude-mem", "name": "belongs_to",
             "fact": "Component 的实现属于 claude-mem，其受支持的观察器是 Integration Observer。"},
        ]
        item = {"id": 1, **{k: payload[k] for k in
                ("name", "source_description", "source_obs_id", "reference_time", "episode_body")}}
        batch = {"batch_id": "fixture-http-batch", "prompt_version": "agent-v5", "items": [item], "lookahead": []}
        results = {"items": [{"id": 1, "entities": payload["entities"], "edges": payload["edges"]}]}
        prompt = "agent-v5 synthetic integration fixture rules"
        identity = quality.extraction_identity(payload["extractor_model"], payload["extractor_tool"], payload["run_id"])
        review = quality.review_sheet(batch, results, prompt, identity)
        # Test-only approval of the fully known synthetic fixture, not live data.
        review["reviewer"] = "known-synthetic-fixture-oracle"
        receipt = review["items"][0]
        receipt.update(verdict="ok", translation_complete=True, facts_complete=True,
                       core_covered=True, ownership_checked=True)
        for edge in receipt["edges"]:
            edge["quote"] = BODY
        for summary in receipt["summaries"]:
            summary["quote"] = BODY
        payload.update(quality_review=review, quality_context={"batch": batch, "results": results,
                       "prompt_text": prompt, "prompt_version": "agent-v5"})
        self.assertEqual(quality.validate_payload_quality(payload), [])
        mutex = asyncio.Lock()
        @asynccontextmanager
        async def isolated_lock(**kw):
            self.assertEqual(kw["owner"], "extraction-repair")
            async with mutex:
                yield
        with patch.object(server, "API_TOKEN", "synthetic-admin-token"), \
                patch.object(server, "GROUP_ID", GROUP), \
                patch.object(server, "get_graphiti", AsyncMock(return_value=self.g)) as graph_provider, \
                patch.object(server, "get_status_driver", return_value=self.driver) as status_provider, \
                patch.object(server, "_ingest_execution_lock", isolated_lock), \
                patch.object(server, "drain_seconds_left", return_value=0), \
                patch.object(server.app.router, "lifespan_context", side_effect=AssertionError("production lifespan must not run")) as lifespan:
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://isolated-fixture") as client:
                denied = await client.get("/api/extraction/capabilities")
                self.assertEqual(denied.status_code, 401)
                client.headers["Authorization"] = "Bearer synthetic-admin-token"
                capabilities = await client.get("/api/extraction/capabilities")
                self.assertEqual(capabilities.status_code, 200)
                self.assertIn("agent-v5", capabilities.json()["prompt_versions"])
                before = await self.counts()
                preview = await client.post("/api/extraction/repair/preview", json=req)
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(before, await self.counts())
                relabeled = deepcopy(req)
                relabeled["replacement"]["extractor_model"] = "unreviewed-model"
                rejected = await client.post("/api/extraction/repair", json=relabeled)
                self.assertEqual(rejected.status_code, 400, rejected.text)
                self.assertEqual(rejected.json()["code"], "quality_review_failed")
                self.assertEqual(before, await self.counts())
                result = await client.post("/api/extraction/repair", json=req)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertTrue(result.json()["verified"])
                status = await client.get("/api/extraction/repair", params={"repair_id": req["repair_id"]})
                self.assertEqual(status.status_code, 200, status.text)
                self.assertEqual(status.json()["repair"]["state"], "applied")
                replay = await client.post("/api/extraction/repair", json=req)
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertTrue(replay.json()["duplicate"])
            self.assertGreater(graph_provider.await_count, 0)
            status_provider.assert_called_once()
            lifespan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
