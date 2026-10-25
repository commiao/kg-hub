"""Real repair state machine against a fault-injecting graph adapter.

The adapter asserts the destructive Cypher guards as well as graph effects;
removing a shared-source guard cannot leave these tests green.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import struct
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from utils import extracted_ingest as xi
from utils import extraction_repair as repair
from tools import extraction_repair as cli


class Embedder:
    async def create_batch(self, values):
        return [[1.0, 0.0, 0.0] for _ in values]


class Driver:
    def __init__(self, *, shared=False, empty=False):
        self.key = {"source_description": "source", "source_obs_id": "one", "name": "obs-one",
                    "status": "empty" if empty else "ok", "episode_uuid": None if empty else "episode",
                    "nodes": 0 if empty else 3, "edges": 0 if empty else 2,
                    "empty_by_model": "original-model", "empty_by_tool": "codex", "empty_run_id": "run-one"}
        self.episode = None if empty else {"uuid": "episode", "name": "obs-one", "group_id": "kg_hub",
            "content": "The component implementation belongs to claude-mem; the recording project is credvault.",
            "source_description": "source", "extractor_model": "original-model", "prompt_version": "agent-v4",
            "valid_at": "2026-10-07T00:00:00+00:00",
            "entity_edges": ["wrong", "keep"], "custom_metadata": "preserve me"}
        self.nodes = {"component": {"uuid": "component", "name": "Component", "summary": "original summary", "type": "Tool"},
                      "wrong-project": {"uuid": "wrong-project", "name": "credvault", "type": "Project"},
                      "right-project": {"uuid": "right-project", "name": "claude-mem", "type": "Project"}}
        self.edges = {} if empty else {
            "wrong": {"uuid": "wrong", "source_uuid": "component", "target_uuid": "wrong-project",
                      "name": "belongs_to", "fact": "Wrong original project attribution", "group_id": "kg_hub",
                      "fact_embedding": [1.0, 0.0, 0.0], "episodes": ["episode", "other"] if shared else ["episode"]},
            "keep": {"uuid": "keep", "source_uuid": "component", "target_uuid": "right-project",
                     "name": "relates_to", "fact": "A separate supported historical mechanism", "group_id": "kg_hub",
                     "episodes": ["episode"]}}
        self.mentions = set() if empty else {"component", "wrong-project", "right-project"}
        self.audits, self.mutations = {}, []
        self.explicit_edge_props = set()
        self.fail_at = None

    async def execute_query(self, query, **p):
        import re
        tag = (re.search(r"/\* (repair:[^ ]+) \*/", query) or [None, None])[1]
        if tag and tag not in {"repair:snapshot-key", "repair:snapshot-episode", "repair:snapshot-edges", "repair:snapshot-entities", "repair:source-counts", "repair:shared-readback", "repair:edge-origin", "repair:audit-read"}:
            self.mutations.append(tag)
        if self.fail_at is not None and self.fail_at == tag:
            self.fail_at = None
            raise RuntimeError("injected interruption")
        if tag == "repair:snapshot-key":
            result = [{"props": deepcopy(self.key)}] if (p["sd"], p["sid"]) == ("source", "one") else []
        elif tag == "repair:snapshot-episode":
            result = [{"props": deepcopy(self.episode)}] if self.episode and self.episode["uuid"] == p["ep"] else []
        elif tag == "repair:snapshot-edges":
            result = [{"props": self.edge_properties(e),
                       "source_uuid": e["source_uuid"], "target_uuid": e["target_uuid"],
                       "source_name": self.nodes[e["source_uuid"]]["name"],
                       "target_name": self.nodes[e["target_uuid"]]["name"]}
                      for e in sorted(self.edges.values(), key=lambda x: x["uuid"]) if p["ep"] in e["episodes"]]
        elif tag == "repair:edge-origin":
            e = self.edges.get(p["uid"])
            result = [{"props": self.edge_properties(e), "source_uuid": e["source_uuid"], "target_uuid": e["target_uuid"]}] if e else []
        elif tag == "repair:shared-readback":
            self.assert_guard(query, "e.uuid IN $ids")
            self.assert_guard(query, "e.group_id=$group_id")
            result = [{"props": self.edge_properties(e),
                       "source_uuid": e["source_uuid"], "target_uuid": e["target_uuid"]}
                      for e in self.edges.values() if e["uuid"] in p["ids"]]
        elif tag == "repair:snapshot-entities":
            result = [{"props": deepcopy(self.nodes[n]), "labels": ["Entity", self.nodes[n]["type"]]} for n in sorted(self.mentions)]
        elif tag == "repair:audit-read":
            result = [{"props": deepcopy(self.audits[p["rid"]])}] if p["rid"] in self.audits else []
        elif tag == "repair:prepare":
            self.audits.setdefault(p["rid"], {"repair_id": p["rid"], "request_sha256": p["request_sha"],
                "request_json": p["request_json"], "before_json": p["before_json"], "plan_json": p["plan_json"],
                "state": "pending", "created_at": p["now"]})
            result = [{"request_sha256": self.audits[p["rid"]]["request_sha256"]}]
        elif tag == "repair:claim":
            self.assert_guard(query, "k.repair_pending_id IS NULL OR k.repair_pending_id=$rid")
            if self.key.get("repair_pending_id") not in (None, p["rid"]):
                result = []
            else:
                self.key["repair_pending_id"] = p["rid"]
                result = [{"sid": "one"}]
        elif tag == "repair:node":
            self.assert_guard(query, "ON CREATE SET")
            self.nodes.setdefault(p["uid"], {**deepcopy(p["props"]), "type": "Tool"})
            result = [{"uuid": p["uid"]}]
        elif tag == "repair:episode":
            if self.episode is None:
                self.episode = deepcopy(p["props"])
            result = [{"uuid": p["ep"]}]
        elif tag == "repair:mention":
            self.assert_guard(query, "MERGE (e)-[m:MENTIONS]->(n) ON CREATE SET m.uuid=$mid")
            self.mentions.add(p["node"])
            result = [{"uuid": p["mid"]}]
        elif tag == "repair:edge-reuse":
            self.assert_guard(query, "MATCH (a:Entity {uuid:$src})-[e:RELATES_TO {uuid:$uid}]->(b:Entity {uuid:$dst})")
            if "MERGE" in query or "ON CREATE" in query:
                raise AssertionError("reuse must never recreate a missing global edge")
            e = self.edges.get(p["uid"])
            if e and (e["source_uuid"], e["target_uuid"], e["name"], e["fact"]) == (p["src"], p["dst"], p["name"], p["fact"]):
                if p["ep"] not in e["episodes"]:
                    e["episodes"].append(p["ep"])
                result = [{"uuid": p["uid"]}]
            else:
                result = []
        elif tag == "repair:edge":
            if p["src"] not in self.nodes or p["dst"] not in self.nodes:
                result = []
            else:
                if p["uid"] not in self.edges:
                    self.explicit_edge_props.add(p["uid"])
                if p["uid"] not in self.edges:
                    self.edges[p["uid"]] = {**deepcopy(p["props"]), "fact_embedding": [struct.unpack("!f", struct.pack("!f", x))[0] for x in p["embedding"]]}
                e = self.edges[p["uid"]]
                if p["ep"] not in e["episodes"]:
                    e["episodes"].append(p["ep"])
                result = [{"uuid": p["uid"]}]
        elif tag == "repair:detach":
            self.assert_guard(query, "WHERE e.fact=$fact AND e.name=$name")
            self.assert_guard(query, "[x IN coalesce(e.episodes,[]) WHERE x<>$ep]")
            e = self.edges.get(p["uid"])
            if e and e["fact"] == p["fact"] and e["name"] == p["name"]:
                e["episodes"] = [x for x in e["episodes"] if x != p["ep"]]
            result = []
        elif tag == "repair:delete-orphan":
            self.assert_guard(query, "WHERE size(coalesce(e.episodes,[]))=0 DELETE e")
            if p["uid"] in self.edges and not self.edges[p["uid"]]["episodes"]:
                del self.edges[p["uid"]]
            result = []
        elif tag == "repair:mentions-clean":
            for n in p["old_nodes"]:
                if not any(p["ep"] in e["episodes"] and n in (e["source_uuid"], e["target_uuid"]) for e in self.edges.values()):
                    self.mentions.discard(n)
            result = []
        elif tag == "repair:episode-finish":
            self.assert_guard(query, "WITH e,[x IN coalesce(e.entity_edges,[]) WHERE NOT x IN $removed] AS kept")
            self.assert_guard(query, "SET e.entity_edges=kept+[x IN $added WHERE NOT x IN kept]")
            self.episode["entity_edges"] = [x for x in self.episode["entity_edges"] if x not in p["removed"]]
            self.episode["entity_edges"] += [x for x in p["added"] if x not in self.episode["entity_edges"]]
            if p["rid"] not in self.episode.setdefault("repair_ids", []):
                self.episode["repair_ids"].append(p["rid"])
            result = [{"uuid": p["ep"]}]
        elif tag == "repair:source-counts":
            self.assert_guard(query, "count(DISTINCT n)")
            self.assert_guard(query, "count(DISTINCT r)")
            result = [{"nodes": len(self.mentions), "edges": sum(p["ep"] in e["episodes"] for e in self.edges.values())}]
        elif tag == "repair:key-finish":
            self.assert_guard(query, "WHERE k.repair_pending_id=$rid")
            self.key.update(status="ok", episode_uuid=p["ep"], nodes=p["nodes"], edges=p["edges"], updated_at=p["now"], last_repair_id=p["rid"])
            result = [{"uuid": p["ep"]}]
        elif tag == "repair:failure":
            self.audits[p["rid"]].update(last_error=p["kind"], last_error_at=p["now"])
            result = [{"repair_id": p["rid"]}]
        elif tag == "repair:complete":
            self.audits[p["rid"]].update(state="applied", result_json=p["result_json"], applied_at=p["now"])
            result = [{"repair_id": p["rid"]}]
        elif tag == "repair:release":
            if self.key.get("repair_pending_id") == p["rid"]:
                self.key.pop("repair_pending_id")
            result = [{"sid": "one"}]
        elif query in (xi._FIND_ALIAS, xi._FIND_RETIRED, xi._FOLLOW_RETIRED, xi._LIVE_BY_UUID):
            result = []   # 节点治理旧名（repair 计划复用入图消解）：这张假图里没有合并/改名过的节点
        elif "toLower(n.name) = $lname" in query:
            result = [{"uuid": n["uuid"], "name": n["name"], "labels": ["Entity", n["type"]]}
                      for n in self.nodes.values() if n["name"].lower() == p["lname"]]
        elif "vec.cosineDistance" in query:
            result = []
        elif "WHERE e.group_id = $g AND e.fact = $fact" in query:
            result = [{"uuid": e["uuid"], "episodes": deepcopy(e["episodes"])} for e in self.edges.values()
                      if (e["source_uuid"], e["target_uuid"], e["name"], e["fact"]) == (p["src"], p["dst"], p["name"], p["fact"])]
        else:
            raise AssertionError(query)
        return result, None, None

    def edge_properties(self, edge):
        return {key: deepcopy(value) for key, value in edge.items()
                if edge["uuid"] in self.explicit_edge_props or key not in {"source_uuid", "target_uuid"}}

    @staticmethod
    def assert_guard(query, guard):
        if guard not in query:
            raise AssertionError("missing destructive-write guard: " + guard)


class RepairTests(unittest.IsolatedAsyncioTestCase):
    def graph(self, **kw):
        return SimpleNamespace(driver=Driver(**kw), embedder=Embedder())

    async def request(self, g, rid="repair-one"):
        snap = await repair.snapshot(g.driver, "source", "one", "kg_hub")
        body = (snap.get("episode") or {}).get("content") or "The original historical source contains a new concrete implementation fact."
        payload = {"name": "obs-one", "source_description": "source", "source_obs_id": "one",
            "episode_body": body, "reference_time": "2026-10-07T00:00:00Z", "extractor_model": "corrector-model",
            "extractor_tool": "codex", "run_id": "run-one", "prompt_version": "agent-v5",
            "entities": [{"name": "Component", "type": "Tool", "summary": "supported component"},
                         {"name": "claude-mem", "type": "Project", "summary": "supported project"}],
            "edges": [{"source": "Component", "target": "claude-mem", "name": "belongs_to",
                       "fact": "Component implementation belongs to claude-mem according to the source."}]}
        return {"repair_id": rid, "source_description": "source", "source_obs_id": "one",
                "expected_snapshot_sha256": snap["snapshot_sha256"], "reason": "Correct recording-label attribution",
                "reviewer": "independent reviewer", "remove_edge_uuids": [] if snap["key"]["status"] == "empty" else ["wrong"],
                "replacement": payload}

    async def reused_fixture(self):
        g = self.graph()
        request = await self.request(g)
        fact = request["replacement"]["edges"][0]["fact"]
        g.driver.edges["first-origin"] = {"uuid": "first-origin", "source_uuid": "component", "target_uuid": "right-project",
            "name": "belongs_to", "fact": fact, "group_id": "kg_hub", "episodes": ["earlier-source"],
            "valid_at": "2026-10-06T13:21:29.858Z", "reference_time": "2026-10-06T13:21:29.858Z",
            "created_at": "2026-10-06T13:22:00Z", "repair_id": "earlier-repair", "extractor_model": "earlier-model",
            "extractor_tool": "codex", "run_id": "earlier-run", "fact_embedding": [1.0, 0.0, 0.0], "custom": "immutable"}
        return g, request

    async def test_reused_preview_and_readback_keep_first_origin(self):
        g, request = await self.reused_fixture()
        preview = await repair.preview(g, request, "kg_hub", quality_validator=lambda p: [])
        edge = preview["plan"]["edges"][0]
        self.assertEqual(edge["write_mode"], "reused")
        self.assertEqual(edge["valid_at"], "2026-10-06T13:21:29.858Z")
        self.assertEqual(edge["repair_id"], "earlier-repair")
        self.assertEqual(preview["plan"]["source_reference_time"], request["replacement"]["reference_time"])
        result = await repair.apply(g, request, "kg_hub", quality_validator=lambda p: [])
        self.assertTrue(result["verified"])
        proof = result["replacement_edge_verification"]
        self.assertEqual(proof["version"], "edge-origin-v1")
        self.assertEqual(proof["edges"][0]["origin"]["repair_id"], "earlier-repair")
        self.assertEqual(g.driver.edges["first-origin"]["episodes"], ["earlier-source", "episode"])
        self.assertEqual(g.driver.episode["valid_at"], "2026-10-07T00:00:00+00:00")

    async def test_legacy_applied_receipt_is_not_rewritten_or_upgraded(self):
        g = self.graph()
        request = await self.request(g)
        parsed = repair.parse_request(request)
        receipt = {"status": "repaired", "verified": True, "repair_id": request["repair_id"]}
        g.driver.audits[request["repair_id"]] = {"repair_id": request["repair_id"], "state": "applied",
            "request_sha256": repair.digest(parsed), "result_json": repair.encoded(receipt)}
        result = await repair.apply(g, request, "kg_hub", quality_validator=lambda p: [])
        self.assertTrue(result["duplicate"])
        self.assertNotIn("replacement_edge_verification", result)
        self.assertEqual(g.driver.audits[request["repair_id"]]["result_json"], repair.encoded(receipt))

    async def test_legacy_pending_plan_replays_without_new_origin_claim(self):
        g = self.graph()
        request = await self.request(g)
        before = await repair.snapshot(g.driver, "source", "one", "kg_hub")
        parsed = repair.parse_request(request)
        plan = await repair.prepare_plan(g, parsed, before, "kg_hub")
        plan.pop("materialization_version")
        plan.pop("source_reference_time")
        for edge in plan["edges"]:
            edge.pop("write_mode"); edge.pop("stored_origin")
        await repair._persist_prepared(g.driver, parsed, before, plan)
        frozen_plan = g.driver.audits[request["repair_id"]]["plan_json"]
        result = await repair.apply(g, request, "kg_hub", quality_validator=lambda p: [])
        self.assertEqual(result["replacement_edge_verification"], {"status": "legacy_plan", "version": "legacy"})
        self.assertEqual(g.driver.audits[request["repair_id"]]["plan_json"], frozen_plan)

    async def test_reused_origin_or_endorsement_drift_blocks_replay(self):
        for field in ("valid_at", "repair_id", "extractor_model", "custom", "episodes", "source_uuid", "missing"):
            with self.subTest(field=field):
                g, request = await self.reused_fixture()
                g.driver.fail_at = "repair:claim"
                with self.assertRaises(RuntimeError):
                    await repair.apply(g, request, "kg_hub", quality_validator=lambda p: [])
                if field == "missing":
                    del g.driver.edges["first-origin"]
                else:
                    g.driver.edges["first-origin"][field] = [] if field == "episodes" else "tampered"
                with self.assertRaisesRegex(repair.RepairError, "reused edge"):
                    await repair.apply(g, request, "kg_hub", quality_validator=lambda p: [])
                self.assertEqual(g.driver.audits["repair-one"]["state"], "pending")
                self.assertNotIn("result_json", g.driver.audits["repair-one"])

    async def test_preview_is_readonly_and_contains_original_and_edges(self):
        g = self.graph()
        result = await repair.preview(g, {"source_description": "source", "source_obs_id": "one"}, "kg_hub", quality_validator=lambda p: [])
        self.assertEqual(result["snapshot"]["episode"]["custom_metadata"], "preserve me")
        self.assertEqual({e["props"]["uuid"] for e in result["snapshot"]["edges"]}, {"wrong", "keep"})
        self.assertEqual(g.driver.mutations, [])

    async def test_shared_edge_keeps_other_episode_and_source_original(self):
        g = self.graph(shared=True)
        original = deepcopy(g.driver.episode)
        req = await self.request(g)
        result = await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(g.driver.edges["wrong"]["episodes"], ["other"])
        self.assertNotIn("wrong", g.driver.episode["entity_edges"])
        for key in ("content", "extractor_model", "prompt_version", "custom_metadata"):
            self.assertEqual(g.driver.episode[key], original[key])
        self.assertEqual(g.driver.nodes["component"]["summary"], "original summary")
        self.assertNotIn("wrong-project", g.driver.mentions)
        saved = await repair.audit(g.driver, "repair-one")
        self.assertEqual(saved["before"]["edges"][1]["props"]["fact"], "Wrong original project attribution")
        self.assertEqual(saved["state"], "applied")

    async def test_orphan_wrong_edge_removed_only_after_replacements_and_audit(self):
        g = self.graph()
        await repair.apply(g, await self.request(g), "kg_hub", quality_validator=lambda p: [])
        self.assertNotIn("wrong", g.driver.edges)
        self.assertIn("keep", g.driver.edges)
        self.assertLess(g.driver.mutations.index("repair:prepare"), g.driver.mutations.index("repair:edge"))
        self.assertLess(g.driver.mutations.index("repair:edge"), g.driver.mutations.index("repair:detach"))
        self.assertTrue(g.driver.episode["entity_edges"])

    async def test_shared_readback_proves_other_sources_and_rejects_corruption(self):
        for corruption in (None, "missing", "episodes", "fact", "source_uuid", "target_uuid"):
            with self.subTest(corruption=corruption):
                g = self.graph(shared=True)
                req = await self.request(g)
                query = g.driver.execute_query
                async def readback(sql, **params):
                    rows, meta, extra = await query(sql, **params)
                    if "/* repair:shared-readback */" in sql and corruption:
                        if corruption == "missing":
                            rows = []
                        elif corruption in ("source_uuid", "target_uuid"):
                            rows[0][corruption] = "tampered-endpoint"
                        else:
                            rows[0]["props"][corruption] = [] if corruption == "episodes" else "tampered fact"
                    return rows, meta, extra
                g.driver.execute_query = readback
                if corruption:
                    with self.assertRaisesRegex(repair.RepairError, "shared fact"):
                        await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
                    self.assertEqual(g.driver.audits["repair-one"]["state"], "pending")
                else:
                    result = await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
                    proof = result["shared_edge_verification"]
                    self.assertEqual(proof["count"], 1)
                    self.assertEqual(proof["edges"][0]["uuid"], "wrong")
                    self.assertEqual(proof["edges"][0]["other_episodes_count"], 1)
                    self.assertFalse(proof["truncated"])

    async def test_empty_upgrades_same_key_and_preserves_original_empty_provenance(self):
        g = self.graph(empty=True)
        result = await repair.apply(g, await self.request(g), "kg_hub", quality_validator=lambda p: [])
        self.assertTrue(result["upgraded_empty"])
        self.assertEqual(g.driver.key["status"], "ok")
        self.assertEqual(g.driver.key["source_obs_id"], "one")
        self.assertEqual(g.driver.key["empty_by_model"], "original-model")
        self.assertEqual(g.driver.episode["extractor_model"], "original-model")
        self.assertEqual(g.driver.episode["run_id"], "run-one")
        self.assertTrue(g.driver.episode["original_empty"])
        self.assertEqual((g.driver.key["nodes"], g.driver.key["edges"]), (2, 1))

    async def test_source_reference_time_requires_original_instant_with_timezone(self):
        for timestamp in ("2035-01-01T00:00:00Z", "2026-10-07T00:00:00", "invalid"):
            with self.subTest(timestamp=timestamp):
                g = self.graph()
                req = await self.request(g)
                req["replacement"]["reference_time"] = timestamp
                with self.assertRaises(repair.RepairError):
                    await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
                self.assertEqual(g.driver.mutations, [])
        g = self.graph()
        req = await self.request(g)
        req["replacement"]["reference_time"] = "2026-10-07T08:00:00+08:00"
        self.assertTrue((await repair.apply(g, req, "kg_hub", quality_validator=lambda p: []))["verified"])
        self.assertEqual(g.driver.episode["valid_at"], "2026-10-07T00:00:00+00:00")

    async def test_incorrect_key_counts_cannot_be_verified(self):
        g = self.graph(empty=True)
        req = await self.request(g)
        execute = g.driver.execute_query
        async def corrupted(query, **params):
            result = await execute(query, **params)
            if "/* repair:key-finish */" in query:
                g.driver.key.update(nodes=0, edges=0)
            return result
        g.driver.execute_query = corrupted
        with self.assertRaisesRegex(repair.RepairError, "counts differ"):
            await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
        self.assertEqual(g.driver.audits["repair-one"]["state"], "pending")
        self.assertEqual(g.driver.key["repair_pending_id"], "repair-one")

    async def test_new_empty_source_hash_prevents_changing_historical_body(self):
        g = self.graph(empty=True)
        req = await self.request(g)
        g.driver.key["empty_source_sha256"] = repair.source_digest(req["replacement"])
        req = await self.request(g)
        req["replacement"]["episode_body"] += " fabricated source extension"
        with self.assertRaisesRegex(repair.RepairError, "source bytes"):
            await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
        self.assertEqual(g.driver.mutations, [])

    async def test_duplicate_is_idempotent_different_payload_with_same_id_rejected(self):
        g = self.graph()
        req = await self.request(g)
        first = await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
        edges = deepcopy(g.driver.edges)
        second = await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(g.driver.edges, edges)
        req["reason"] = "another action"
        with self.assertRaisesRegex(repair.RepairError, "another request"):
            await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])

    async def test_stale_snapshot_foreign_edge_identity_and_quality_fail_without_writes(self):
        for change in ("stale", "foreign", "identity", "quality"):
            with self.subTest(change=change):
                g = self.graph()
                req = await self.request(g)
                if change == "stale": g.driver.episode["content"] += " changed"
                if change == "foreign": req["remove_edge_uuids"] = ["not-this-episode"]
                if change == "identity": req["replacement"]["source_obs_id"] = "new-key"
                with self.assertRaises(repair.RepairError):
                    await repair.apply(g, req, "kg_hub", quality_validator=lambda p: ["not reviewed"] if change == "quality" else [])
                self.assertEqual(g.driver.mutations, [])

    async def test_partial_write_replays_original_plan_at_every_mutation_boundary(self):
        for boundary in ("repair:claim", "repair:edge", "repair:detach", "repair:episode-finish", "repair:complete", "repair:release"):
            with self.subTest(boundary=boundary):
                g = self.graph()
                req = await self.request(g)
                g.driver.fail_at = boundary
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
                self.assertIn("repair-one", g.driver.audits)
                result = await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
                self.assertEqual(result["status"], "repaired")
                self.assertNotIn("wrong", g.driver.edges)
                self.assertEqual(len(g.driver.edges), 2)
                self.assertNotIn("repair_pending_id", g.driver.key)
                self.assertEqual(g.driver.audits["repair-one"]["state"], "applied")

    async def test_readback_failure_never_emits_success_and_keeps_pending_plan(self):
        g = self.graph()
        req = await self.request(g)
        original = repair.verify_materialization
        with patch.object(repair, "verify_materialization", side_effect=repair.RepairError("repair_not_materialized", "missing", 503)):
            with self.assertRaisesRegex(repair.RepairError, "missing"):
                await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
        self.assertEqual(g.driver.audits["repair-one"]["state"], "pending")
        self.assertEqual(g.driver.audits["repair-one"]["last_error"], "RepairError")
        self.assertEqual(g.driver.key["repair_pending_id"], "repair-one")
        result = await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
        self.assertTrue(result["verified"])
        self.assertNotIn("repair_pending_id", g.driver.key)

    async def test_prepared_but_unclaimed_repair_rejects_external_source_change(self):
        g = self.graph()
        req = await self.request(g)
        g.driver.fail_at = "repair:claim"
        with self.assertRaises(RuntimeError):
            await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
        g.driver.episode["content"] += " external modification"
        with self.assertRaisesRegex(repair.RepairError, "changed"):
            await repair.apply(g, req, "kg_hub", quality_validator=lambda p: [])
        self.assertIn("wrong", g.driver.edges)

    async def test_concurrent_writer_lock_serializes_same_id_and_rejects_stale_other_id(self):
        g = self.graph()
        req = await self.request(g)
        other = deepcopy(req); other["repair_id"] = "other-id"
        lock = asyncio.Lock()
        async def locked(doc):
            async with lock:
                return await repair.apply(g, doc, "kg_hub", quality_validator=lambda p: [])
        first, second = await asyncio.gather(locked(req), locked(req))
        self.assertEqual((first["duplicate"], second["duplicate"]), (False, True))
        with self.assertRaisesRegex(repair.RepairError, "changed"):
            await locked(other)


class RepairCliTests(unittest.TestCase):
    def test_preview_template_is_unapproved_and_apply_does_not_refresh_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); src = root / "source.json"; out = root / "preview.json"; pending = root / "pending.json"
            src.write_text(json.dumps({"source_description": "source", "source_obs_id": "one"}))
            def http(method, url, body, token):
                self.assertEqual(body, {"source_description": "source", "source_obs_id": "one"})
                return 200, {"status": "repair_preview", "snapshot": {"snapshot_sha256": "a"*64,
                    "key": {"status": "empty"}, "episode": None, "edges": []}}
            with patch.object(cli.ax, "api_token", return_value="test-only-secret"):
                self.assertEqual(cli.main(["preview", "--file", str(src), "--out", str(out), "--request-out", str(pending)], http=http), 0)
                template = json.loads(pending.read_text())
                self.assertEqual(template["expected_snapshot_sha256"], "a"*64)
                self.assertEqual(template["reviewer"], "")
                self.assertEqual(template["repair_id"], "")
                with self.assertRaises(repair.RepairError):
                    cli.main(["apply", "--file", str(pending)], http=lambda *a: self.fail("must reject before HTTP"))

    def test_status_uses_original_id_and_non_success_is_failure(self):
        def http(method, url, body, token):
            self.assertEqual(method, "GET"); self.assertIn("repair_id=repair-1", url)
            return 404, {"status": "error", "code": "repair_not_found"}
        with patch.object(cli.ax, "api_token", return_value=None):
            self.assertEqual(cli.main(["status", "--repair-id", "repair-1"], http=http), 1)


if __name__ == "__main__":
    unittest.main()
