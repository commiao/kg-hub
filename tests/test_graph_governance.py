"""Node-governance state machine against an in-memory graph emulator.

The emulator answers each tagged query by its graph effect and asserts the
guards that protect other sources (uuid + endpoints + fact on every edge write),
so removing a guard or reordering a destructive step cannot leave these green.
Cypher syntax itself is covered by the opt-in real FalkorDB integration test.
"""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import re
import struct
import tempfile
from types import SimpleNamespace
import unittest

from utils import graph_governance as gov
from tools import graph_governance as cli

GROUP = "kg_hub"


class Embedder:
    async def create_batch(self, values):
        return [[0.1, 0.2, 0.3] for _ in values]


def f32(values):
    return [struct.unpack("!f", struct.pack("!f", float(v)))[0] for v in values]


class Graph:
    def __init__(self):
        self.nodes, self.edges, self.mentions, self.episodes, self.audits = {}, {}, [], {}, {}
        self.foreign = {}
        self.tags, self.fail_at, self.after = [], None, None

    # fixture helpers
    def node(self, uid, kind, name, **props):
        self.nodes[uid] = {"labels": {"Entity", kind}, "props": {"uuid": uid, "name": name, "group_id": GROUP,
                                                                 "name_embedding": [0.5, 0.5, 0.0], **props}}

    def edge(self, uid, src, dst, fact, episodes, created, name="relates_to", **props):
        self.edges[uid] = {"src": src, "dst": dst, "props": {
            "uuid": uid, "name": name, "fact": fact, "group_id": GROUP, "episodes": list(episodes),
            "created_at": created, "fact_embedding": [0.25, 0.0, 0.0], "extractor_model": "origin-" + uid, **props}}

    def episode(self, uid, edges, mentions):
        self.episodes[uid] = {"uuid": uid, "name": "obs-" + uid, "source_description": "claude-mem:" + uid,
                              "group_id": GROUP, "valid_at": "2026-10-01T00:00:00+00:00", "extractor_model": "m",
                              "extractor_tool": "claude", "run_id": "run", "entity_edges": list(edges), "archived": None}
        for node in mentions:
            self.mentions.append({"ep": uid, "node": node, "props": {"uuid": f"m-{uid}-{node}", "group_id": GROUP}})

    def entity(self, uid):
        node = self.nodes.get(uid)
        return node if node and "Entity" in node["labels"] else None

    def live(self, edge):
        return self.entity(edge["src"]) and self.entity(edge["dst"])

    def row(self, edge):
        return {"props": deepcopy(edge["props"]), "source_uuid": edge["src"], "target_uuid": edge["dst"],
                "source_name": self.nodes[edge["src"]]["props"]["name"],
                "target_name": self.nodes[edge["dst"]]["props"]["name"]}

    def at(self, uid, src, dst, name, fact):
        edge = self.edges.get(uid)
        return edge if edge and (edge["src"], edge["dst"]) == (src, dst) and edge["props"]["name"] == name \
            and edge["props"]["fact"] == fact and self.live(edge) else None

    def guard(self, query, text):
        if text not in query:
            raise AssertionError(f"missing guard {text!r}")

    async def execute_query(self, query, **p):
        tag = re.search(r"/\* (gov:[^ ]+) \*/", query)[1]
        self.tags.append(tag)
        result = self.answer(tag, query, p)
        if self.after:
            self.after(tag, p)
        if self.fail_at == tag:
            self.fail_at = None
            raise RuntimeError("interrupted after " + tag)
        return result, None, None

    def answer(self, tag, q, p):
        n, e = self.nodes, self.edges
        if tag in ("gov:resolve", "gov:name-conflicts"):
            return [{"uuid": u, "name": x["props"]["name"], **({"labels": sorted(x["labels"])} if tag == "gov:resolve" else {})}
                    for u, x in sorted(n.items()) if "Entity" in x["labels"]
                    and x["props"]["group_id"] == p["g"] and x["props"]["name"].lower() == p["lname"]]
        if tag == "gov:nodes":
            return [{"props": deepcopy(n[u]["props"]), "labels": sorted(n[u]["labels"])} for u in p["ids"] if self.entity(u)]
        if tag == "gov:tombstones":
            return [{"uuid": u, "merged_into": n[u]["props"].get("merged_into"), "merged_by": n[u]["props"].get("merged_by")}
                    for u in p["ids"] if u in n and gov.TOMBSTONE in n[u]["labels"]]
        if tag == "gov:foreign-rels":
            return self.foreign.get(p["u"], [])
        if tag in ("gov:edges-out", "gov:edges-in", "gov:anchor-out", "gov:anchor-in"):
            side, other = ("src", "dst") if tag.endswith("out") else ("dst", "src")
            return [self.row(x) for x in e.values() if x[side] == p["u"] and self.live(x)
                    and ("others" not in p or x[other] in p["others"])]
        if tag == "gov:mentions":
            return [{"episode_uuid": m["ep"], "props": deepcopy(m["props"])} for m in self.mentions
                    if m["node"] == p["u"] and ("eps" not in p or m["ep"] in p["eps"])]
        if tag == "gov:episodes":
            return [deepcopy(self.episodes[u]) for u in p["eps"] if u in self.episodes]
        if tag == "gov:summary-episodes":
            return [{"props": deepcopy(self.episodes[u])} for u in p["eps"] if u in self.episodes]
        if tag == "gov:pending":
            return [{"governance_id": g} for g, a in sorted(self.audits.items())
                    if a["state"] == "pending" and set(a["node_uuids"]) & set(p["ids"])]
        if tag == "gov:audit-read":
            return [{"props": deepcopy(self.audits[p["gid"]])}] if p["gid"] in self.audits else []
        if tag in ("gov:edge-full", "gov:edge-locate"):
            x = e.get(p["uid"])
            return [{"props": deepcopy(x["props"]), "source_uuid": x["src"], "target_uuid": x["dst"]}] if x and self.live(x) else []
        if tag == "gov:mention-exists":
            return [{"c": sum(m["ep"] == p["ep"] and m["node"] == p["node"] and self.entity(m["node"]) is not None
                              for m in self.mentions)}]
        if tag == "gov:node-read":
            label = gov.TOMBSTONE if gov.TOMBSTONE in q else "Entity"
            x = n.get(p["uid"])
            if not x or label not in x["labels"]:
                return []
            return [{"props": deepcopy(x["props"]), "labels": sorted(x["labels"]), "merged_by": x["props"].get("merged_by")}]
        if tag == "gov:prepare":
            self.audits.setdefault(p["gid"], {"governance_id": p["gid"], "request_sha256": p["request_sha"],
                "request_json": p["request_json"], "before_json": p["before_json"], "plan_json": p["plan_json"],
                "materialization_json": p["mat_json"], "deleted_json": p["deleted_json"],
                "node_uuids": p["nodes"], "state": "pending"})
            return [{"request_sha256": self.audits[p["gid"]]["request_sha256"]}]
        if tag in ("gov:failure", "gov:complete"):
            audit = self.audits[p["gid"]]
            if tag == "gov:failure":
                audit["last_error"] = p["kind"]
            else:
                audit.update(state="applied", result_json=p["result_json"])
            return [{"gid": p["gid"]}]
        if tag == "gov:node-create":
            if p["uid"] not in n:
                kind = re.search(r"ON CREATE SET n:(\w+)", q)[1]
                n[p["uid"]] = {"labels": {"Entity", kind}, "props": {**p["props"], "name_embedding": f32(p["embedding"])}}
            return [{"uuid": p["uid"]}]
        if tag in ("gov:edge-move", "gov:edge-extend", "gov:edge-absorb", "gov:edge-drop"):
            self.guard(q, "e.name=$name AND e.fact=$fact")
            self.guard(q, "{uuid:$uid}]->(b:Entity {uuid:$dst})")
            x = self.at(p["uid"], p["src"], p["dst"], p["name"], p["fact"])
            if tag == "gov:edge-move":
                if not x or not self.entity(p["new_src"]) or not self.entity(p["new_dst"]):
                    return []
                x["src"], x["dst"] = p["new_src"], p["new_dst"]
                x["props"]["episodes"] = gov._ordered_union(x["props"]["episodes"], p["extra"])
                return [{"uuid": p["uid"]}]
            if tag == "gov:edge-extend":
                if not x:
                    return []
                x["props"]["episodes"] = gov._ordered_union(x["props"]["episodes"], p["extra"])
                return [{"uuid": p["uid"]}]
            if tag == "gov:edge-absorb":
                self.guard(q, "WHERE all(x IN $eps WHERE x IN coalesce(s.episodes, []))")
                s = e.get(p["survivor"])
                ok = s and (s["src"], s["dst"]) == (p["s_src"], p["s_dst"]) and set(p["eps"]) <= set(s["props"]["episodes"])
                if not (ok and x):
                    return [{"deleted": 0}]
            if not x:
                return [{"deleted": 0}]
            del e[p["uid"]]
            return [{"deleted": 1}]
        if tag == "gov:episode-refs":
            ep = self.episodes[p["ep"]]
            kept = [x for x in ep["entity_edges"] if x not in p["remove"]]
            ep["entity_edges"] = kept + [x for x in p["add"] if x not in kept]
            return [{"uuid": p["ep"]}]
        if tag == "gov:mention-move":
            moved = 0
            for m in self.mentions:
                if m["ep"] == p["ep"] and m["node"] == p["old"] and self.entity(p["to"]):
                    m["node"], moved = p["to"], moved + 1
            return [{"moved": moved}]
        if tag == "gov:mention-add":
            if not any(m["props"].get("uuid") == p["mid"] and m["node"] == p["to"] for m in self.mentions):
                self.mentions.append({"ep": p["ep"], "node": p["to"], "props": {"uuid": p["mid"], "governance_id": p["gid"]}})
            return [{"added": 1}]
        if tag == "gov:mention-drop":
            self.guard(q, "-[:MENTIONS]->(k:Entity {uuid:$keep})")
            if not any(m["ep"] == p["ep"] and m["node"] == p["keep"] for m in self.mentions):
                return []
            before = len(self.mentions)
            self.mentions = [m for m in self.mentions if not (m["ep"] == p["ep"] and m["node"] == p["old"])]
            return [{"dropped": before - len(self.mentions)}]
        if tag == "gov:node-summary":
            self.guard(q, "WHERE n.name=$name AND coalesce(n.group_id,$g)=$g")
            self.guard(q, "coalesce(n.summary,'') IN $accepted")
            x = self.entity(p["uid"])
            if (not x or x["props"].get("name") != p["name"]
                    or x["props"].get("group_id", p["g"]) != p["g"]
                    or (x["props"].get("summary") or '') not in p["accepted"]):
                return []
            x["props"].update(summary=p["summary"], governance_ids=p["governance_ids"])
            return [{"uuid": p["uid"]}]
        if tag == "gov:node-update":
            x = self.entity(p["uid"])
            if not x:
                return []
            x["props"].update(p["props"])
            removed = re.search(r"REMOVE n((?::\w+)+)", q)
            if removed:
                x["labels"] -= set(removed[1].strip(":").split(":"))
            for label in re.findall(r"SET n:(\w+)", q):
                x["labels"].add(label)
            if "embedding" in p:
                x["props"]["name_embedding"] = f32(p["embedding"])
            return [{"uuid": p["uid"]}]
        if tag == "gov:node-tombstone":
            x = self.entity(p["uid"])
            degree = sum(p["uid"] in (y["src"], y["dst"]) for y in e.values()) + sum(m["node"] == p["uid"] for m in self.mentions)
            degree += sum(r["count"] for r in self.foreign.get(p["uid"], []))
            if not x or degree:
                return []
            x["labels"] = {gov.TOMBSTONE}
            x["props"].update(p["props"])
            return [{"uuid": p["uid"]}]
        raise AssertionError("unexpected tag " + tag)

    @property
    def mutations(self):
        return [t for t in self.tags if t not in gov._READ_TAGS]


def merge_graph():
    g = Graph()
    g.node("canon", "File", "credvault:reconciliation_mailbox.py", summary="credvault mailbox")
    g.node("dup", "File", "reconciliation_mailbox.py", summary="bare name")
    g.node("x", "Project", "CredVault")
    g.node("y", "Concept", "ReconciliationMailbox")
    g.edge("old-dup", "dup", "x", "The mailbox belongs to credvault.", ["e1"], "2026-09-01T00:00:00Z", "belongs_to")
    g.edge("new-canon", "canon", "x", "The mailbox belongs to credvault.", ["e2"], "2026-10-20T00:00:00+00:00", "belongs_to")
    g.edge("dup-y", "dup", "y", "The file defines ReconciliationMailbox.", ["e1"], "2026-09-02T00:00:00Z")
    g.edge("loop", "canon", "dup", "The qualified and bare names are the same file.", ["e3"], "2026-10-21T00:00:00Z")
    g.episode("e1", ["old-dup", "dup-y"], ["dup", "x", "y"])
    g.episode("e2", ["new-canon"], ["canon", "x"])
    g.episode("e3", ["loop"], ["canon", "dup"])
    return g


MERGE = {"operation": "merge", "canonical": {"name": "credvault:reconciliation_mailbox.py"},
         "merged": [{"name": "Reconciliation_Mailbox.py"}]}


def run(coro):
    return asyncio.run(coro)


async def reviewed(graph, scope, gid="merge-1"):
    body = await gov.preview(SimpleNamespace(driver=graph, embedder=Embedder()), scope, GROUP)
    return {**body["scope"], "governance_id": gid, "reason": "same file", "reviewer": "unit",
            "expected_snapshot_sha256": body["snapshot"]["snapshot_sha256"]}, body


def g_of(graph):
    return SimpleNamespace(driver=graph, embedder=Embedder())


class ParseTests(unittest.TestCase):
    def test_scope_validation(self):
        bad = [{"operation": "delete"},
               {"operation": "merge", "canonical": {"name": "a"}, "merged": []},
               {"operation": "merge", "canonical": {"name": "a"}, "merged": [{"name": "b", "uuid": "c"}]},
               {"operation": "rename", "node": {"name": "a"}},
               {"operation": "retype", "node": {"name": "a"}, "retype_to": "Server"},
               {"operation": "rename", "node": {"name": "a"}, "rename_to": "two\nlines"},
               {"operation": "split", "node": {"name": "a"},
                "partitions": [{"target": {"name": "b"}, "new_node": {"name": "c"}, "edge_uuids": ["e"]}]},
               {"operation": "split", "node": {"name": "a"}, "partitions": [{"target": {"name": "b"}}]}]
        for document in bad:
            with self.subTest(document=document), self.assertRaises(gov.GovernanceError):
                gov.parse_scope(document, bound=False)
        with self.assertRaises(gov.GovernanceError):  # names are only for the first preview
            gov.parse_scope({"operation": "rename", "node": {"name": "a"}, "rename_to": "b"}, bound=True)
        with self.assertRaises(gov.GovernanceError):
            gov.parse_scope({"operation": "merge", "canonical": {"uuid": "a"}, "merged": [{"uuid": "a"}]}, bound=True)
        with self.assertRaises(gov.GovernanceError):
            gov.parse_scope({"operation": "split", "node": {"uuid": "a"}, "partitions": []}, bound=True)

    def test_request_requires_review_fields(self):
        base = {"operation": "rename", "node": {"uuid": "a"}, "rename_to": "b", "governance_id": "g-1",
                "reason": "r", "reviewer": "me", "expected_snapshot_sha256": "0" * 64}
        self.assertEqual(gov.parse_request(base)["scope"]["rename_to"], "b")
        for field, value in (("governance_id", "bad id"), ("reason", " "), ("reviewer", ""),
                             ("expected_snapshot_sha256", "abc")):
            with self.subTest(field=field), self.assertRaises(gov.GovernanceError):
                gov.parse_request({**base, field: value})


class PlanTests(unittest.TestCase):
    def test_merge_preview_is_read_only_and_complete(self):
        graph = merge_graph()
        before = deepcopy((graph.nodes, graph.edges, graph.mentions, graph.episodes))
        body = run(gov.preview(g_of(graph), MERGE, GROUP))
        self.assertEqual(graph.mutations, [])
        self.assertEqual(before, (graph.nodes, graph.edges, graph.mentions, graph.episodes))
        plan = body["plan"]
        self.assertEqual({e["uuid"]: e["action"] for e in plan["edges"]},
                         {"old-dup": "move", "new-canon": "absorb", "dup-y": "move", "loop": "drop_self_loop"})
        survivor = next(e for e in plan["edges"] if e["uuid"] == "old-dup")
        self.assertEqual((survivor["to"], survivor["episodes_after"]), (["canon", "x"], ["e1", "e2"]))
        absorbed = next(e for e in plan["edges"] if e["uuid"] == "new-canon")
        self.assertEqual(absorbed["survivor_uuid"], "old-dup")
        self.assertEqual(survivor["to_names"], ["credvault:reconciliation_mailbox.py", "CredVault"])
        self.assertEqual([s["episode_uuid"] for s in plan["sources"]], ["e1", "e2", "e3"])
        self.assertEqual(plan["episode_refs"], [{"episode_uuid": "e2", "remove": ["new-canon"], "add": ["old-dup"]},
                                                {"episode_uuid": "e3", "remove": ["loop"], "add": []}])
        self.assertEqual([(m["episode_uuid"], m["action"]) for m in plan["mentions"]], [("e1", "move"), ("e3", "drop")])
        self.assertEqual(body["resolution"][1]["uuid"], "dup")
        self.assertEqual([n["action"] for n in plan["nodes"]], ["update", "tombstone"])
        self.assertEqual(plan["nodes"][0]["set"], {"merged_from": ["dup"], "merged_names": ["reconciliation_mailbox.py"]})
        self.assertNotIn("name_embedding", json.dumps(body["snapshot"]["nodes"][0]["props"]["name_embedding"]))
        self.assertEqual(plan["plan_sha256"], gov.digest({k: v for k, v in plan.items() if k != "plan_sha256"}))

    def test_older_canonical_edge_survives_in_place(self):
        graph = merge_graph()
        graph.edges["new-canon"]["props"]["created_at"] = "2026-08-01T00:00:00Z"
        plan = run(gov.preview(g_of(graph), MERGE, GROUP))["plan"]
        actions = {e["uuid"]: e for e in plan["edges"]}
        self.assertEqual((actions["new-canon"]["action"], actions["new-canon"]["episodes_after"]), ("extend", ["e2", "e1"]))
        self.assertEqual((actions["old-dup"]["action"], actions["old-dup"]["survivor_uuid"]), ("absorb", "new-canon"))

    def test_differently_invalidated_facts_are_not_collapsed(self):
        graph = merge_graph()
        graph.edges["new-canon"]["props"]["invalid_at"] = "2026-10-22T00:00:00Z"
        plan = run(gov.preview(g_of(graph), MERGE, GROUP))["plan"]
        self.assertNotIn("absorb", {e["action"] for e in plan["edges"]})

    def test_type_mismatch_is_warned_and_retype_clears_it(self):
        graph = merge_graph()
        graph.nodes["dup"]["labels"] = {"Entity", "Config"}
        plan = run(gov.preview(g_of(graph), MERGE, GROUP))["plan"]
        self.assertIn("type_mismatch", [w["code"] for w in plan["warnings"]])
        plan = run(gov.preview(g_of(graph), {**MERGE, "retype_to": "File"}, GROUP))["plan"]
        self.assertNotIn("type_mismatch", [w["code"] for w in plan["warnings"]])

    def test_refusals_are_reviewable(self):
        cases = []
        graph = merge_graph()
        graph.node("dup2", "File", "reconciliation_mailbox.py")
        cases.append((graph, MERGE, "ambiguous_node"))
        graph = merge_graph()
        cases.append((graph, {**MERGE, "merged": [{"name": "missing"}]}, "node_not_found"))
        graph = merge_graph()
        graph.foreign["dup"] = [{"type": "HAS_MEMBER", "count": 1}]
        cases.append((graph, MERGE, "unsupported_relationship"))
        graph = merge_graph()
        graph.audits["older"] = {"governance_id": "older", "state": "pending", "node_uuids": ["canon"]}
        cases.append((graph, MERGE, "governance_pending"))
        graph = merge_graph()
        cases.append((graph, {"operation": "rename", "node": {"uuid": "dup"}, "rename_to": "CREDVAULT"}, "name_conflict"))
        graph = merge_graph()
        cases.append((graph, {"operation": "rename", "node": {"uuid": "dup"}, "rename_to": "reconciliation_mailbox.py"},
                      "nothing_to_change"))
        graph = merge_graph()
        graph.nodes["dup"]["labels"] = {gov.TOMBSTONE}
        cases.append((graph, {**MERGE, "merged": [{"uuid": "dup"}]}, "already_merged"))
        for graph, scope, code in cases:
            with self.subTest(code=code), self.assertRaises(gov.GovernanceError) as caught:
                run(gov.preview(g_of(graph), scope, GROUP))
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(graph.mutations, [])

    def test_merge_rename_into_a_merged_name_is_allowed(self):
        graph = merge_graph()
        plan = run(gov.preview(g_of(graph), {**MERGE, "rename_to": "reconciliation_mailbox.py"}, GROUP))["plan"]
        canonical = plan["nodes"][0]
        self.assertEqual((canonical["name"], canonical["embed_name"]), ("reconciliation_mailbox.py", True))
        self.assertEqual(canonical["set"]["previous_names"], ["credvault:reconciliation_mailbox.py"])


def split_graph():
    g = Graph()
    g.node("pom", "File", "pom.xml")
    g.node("sd", "Project", "sd-server")
    g.node("libtv", "Project", "libtv-m")
    g.node("dep", "Concept", "spring-boot 3.2")
    g.node("sdpom", "File", "sd-server/pom.xml")
    g.edge("a", "pom", "sd", "pom.xml belongs to sd-server.", ["ea"], "2026-09-01T00:00:00Z", "belongs_to")
    g.edge("b", "pom", "libtv", "pom.xml belongs to libtv-m.", ["eb"], "2026-09-02T00:00:00Z", "belongs_to")
    g.edge("c", "pom", "dep", "pom.xml pins spring-boot 3.2.", ["ea", "eb"], "2026-09-03T00:00:00Z")
    g.edge("d", "sdpom", "sd", "pom.xml belongs to sd-server.", ["ec"], "2026-08-01T00:00:00Z", "belongs_to")
    g.episode("ea", ["a", "c"], ["pom", "sd", "dep"])
    g.episode("eb", ["b", "c"], ["pom", "libtv", "dep"])
    g.episode("ec", ["d"], ["sdpom", "sd"])
    return g


class SplitTests(unittest.TestCase):
    def test_split_into_existing_and_new_nodes_by_source(self):
        graph = split_graph()
        scope = {"operation": "split", "node": {"name": "pom.xml"}, "retire_original": True,
                 "partitions": [{"target": {"name": "sd-server/pom.xml"}, "episode_uuids": ["ea"]},
                                {"new_node": {"name": "libtv-m:pom.xml"}, "episode_uuids": ["eb"],
                                 "edge_uuids": ["c"]}]}
        request, body = run(reviewed(graph, scope, gid="split-pom"))
        plan = body["plan"]
        new = next(n["uuid"] for n in plan["nodes"] if n["action"] == "create")
        actions = {e["uuid"]: (e["action"], e["to"]) for e in plan["edges"]}
        # a duplicates the older sd-server/pom.xml fact, so it is absorbed there.
        self.assertEqual(actions, {"a": ("absorb", ["sdpom", "sd"]), "b": ("move", [new, "libtv"]),
                                   "c": ("move", [new, "dep"]), "d": ("extend", ["sdpom", "sd"])})
        self.assertEqual(plan["residual_edges"], [])
        result = run(gov.apply(g_of(graph), request, GROUP))
        self.assertTrue(result["verified"])
        self.assertEqual(graph.nodes["pom"]["labels"], {gov.TOMBSTONE})
        self.assertEqual(graph.nodes["pom"]["props"]["split_into"], ["sdpom", new])
        self.assertEqual(graph.edges["d"]["props"]["episodes"], ["ec", "ea"])
        self.assertNotIn("a", graph.edges)
        self.assertEqual(graph.episodes["ea"]["entity_edges"], ["c", "d"])
        self.assertEqual(sorted((m["ep"], m["node"]) for m in graph.mentions if m["node"] in ("pom", "sdpom", new)),
                         sorted([("ea", "sdpom"), ("ea", new), ("eb", new), ("ec", "sdpom")]))
        self.assertEqual(graph.nodes[new]["labels"], {"Entity", "File"})
        self.assertEqual(graph.nodes[new]["props"]["name_embedding"], f32([0.1, 0.2, 0.3]))

    def test_split_refusals(self):
        base = {"operation": "split", "node": {"name": "pom.xml"},
                "partitions": [{"new_node": {"name": "libtv-m:pom.xml"}, "episode_uuids": ["eb"]}]}
        for scope, code in ((base, "ambiguous_edge"),
                            ({**base, "keep_edge_uuids": ["c"], "retire_original": True}, "retire_requires_all_edges"),
                            ({**base, "keep_edge_uuids": ["zz"]}, "foreign_edge"),
                            ({**base, "partitions": [{**base["partitions"][0], "edge_uuids": ["c"]}],
                              "keep_edge_uuids": ["c"]}, "ambiguous_edge"),
                            ({**base, "keep_edge_uuids": ["c"], "partitions": [
                                {"new_node": {"name": "Dep"}, "episode_uuids": ["eb"]},
                                {"new_node": {"name": "dep"}, "edge_uuids": ["a"]}]}, "bad_request")):
            with self.subTest(code=code), self.assertRaises(gov.GovernanceError) as caught:
                run(gov.preview(g_of(split_graph()), scope, GROUP))
            self.assertEqual(caught.exception.code, code)

    def test_mention_only_episode_must_be_assigned_before_retiring(self):
        graph = split_graph()
        graph.episode("ed", [], ["pom"])
        scope = {"operation": "split", "node": {"name": "pom.xml"}, "retire_original": True,
                 "partitions": [{"target": {"name": "sd-server/pom.xml"}, "edge_uuids": ["a", "c"]},
                                {"new_node": {"name": "libtv-m:pom.xml"}, "edge_uuids": ["b"]}]}
        with self.assertRaises(gov.GovernanceError) as caught:
            run(gov.preview(g_of(graph), scope, GROUP))
        self.assertEqual(caught.exception.code, "unassigned_mention")
        scope["partitions"][0]["episode_uuids"] = ["ed"]
        plan = run(gov.preview(g_of(graph), scope, GROUP))["plan"]
        self.assertIn({"episode_uuid": "ed", "action": "move", "from": "pom", "to": "sdpom"}, plan["mentions"])


class ApplyTests(unittest.TestCase):
    def test_merge_apply_verifies_and_replays(self):
        graph = merge_graph()
        request, body = run(reviewed(graph, MERGE))
        result = run(gov.apply(g_of(graph), request, GROUP))
        self.assertTrue(result["verified"])
        self.assertEqual(result["plan_sha256"], body["plan"]["plan_sha256"])
        moved = graph.edges["old-dup"]
        self.assertEqual((moved["src"], moved["dst"], moved["props"]["episodes"]), ("canon", "x", ["e1", "e2"]))
        self.assertEqual(moved["props"]["extractor_model"], "origin-old-dup")
        self.assertNotIn("new-canon", graph.edges)
        self.assertNotIn("loop", graph.edges)
        self.assertEqual(graph.nodes["dup"]["labels"], {gov.TOMBSTONE})
        self.assertEqual(graph.nodes["dup"]["props"]["merged_into"], "canon")
        self.assertEqual(graph.nodes["dup"]["props"]["summary"], "bare name")
        self.assertEqual(graph.nodes["canon"]["props"]["governance_ids"], ["merge-1"])
        audit = run(gov.audit(graph, "merge-1"))
        self.assertEqual(audit["state"], "applied")
        self.assertEqual(sorted(x["props"]["uuid"] for x in audit["deleted"]), ["loop", "new-canon"])
        self.assertEqual(audit["deleted"][0]["props"]["fact_embedding"], [0.25, 0.0, 0.0])
        # the order of destructive steps is part of the contract
        tags = graph.mutations
        self.assertEqual(tags[0], "gov:prepare")
        self.assertLess(max(i for i, t in enumerate(tags) if t == "gov:edge-move"), tags.index("gov:edge-absorb"))
        self.assertLess(tags.index("gov:mention-move"), tags.index("gov:mention-drop"))
        self.assertEqual(tags[-2:], ["gov:node-tombstone", "gov:complete"])
        graph.tags.clear()
        again = run(gov.apply(g_of(graph), request, GROUP))
        self.assertTrue(again["duplicate"])
        self.assertEqual(graph.mutations, [])
        with self.assertRaises(gov.GovernanceError) as caught:
            run(gov.apply(g_of(graph), {**request, "reason": "different"}, GROUP))
        self.assertEqual(caught.exception.code, "governance_id_conflict")

    def test_stale_snapshot_refused_before_any_write(self):
        graph = merge_graph()
        request, _ = run(reviewed(graph, MERGE))
        graph.edge("late", "dup", "y", "A later ingest wrote this.", ["e9"], "2026-10-24T00:00:00Z")
        graph.tags.clear()
        with self.assertRaises(gov.GovernanceError) as caught:
            run(gov.apply(g_of(graph), request, GROUP))
        self.assertEqual(caught.exception.code, "stale_snapshot")
        self.assertEqual(graph.mutations, [])
        self.assertEqual(graph.audits, {})

    def test_reviewed_preview_checks_digest_and_binding(self):
        graph = merge_graph()
        request, _ = run(reviewed(graph, MERGE))
        body = run(gov.preview(g_of(graph), request, GROUP))
        self.assertEqual(body["request_sha256"], gov.digest(gov.parse_request(request)))
        with self.assertRaises(gov.GovernanceError) as caught:
            run(gov.preview(g_of(graph), {**request, "expected_snapshot_sha256": "1" * 64}, GROUP))
        self.assertEqual(caught.exception.code, "stale_snapshot")

    def test_every_interruption_replays_to_the_same_verified_state(self):
        reference = merge_graph()
        request, _ = run(reviewed(reference, MERGE))
        run(gov.apply(g_of(reference), request, GROUP))
        boundaries = sorted(set(reference.mutations) - {"gov:complete"})
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                graph = merge_graph()
                graph.fail_at = boundary
                with self.assertRaises(RuntimeError):
                    run(gov.apply(g_of(graph), request, GROUP))
                self.assertEqual(graph.audits["merge-1"]["state"], "pending")
                result = run(gov.apply(g_of(graph), request, GROUP))
                self.assertTrue(result["verified"])
                self.assertEqual((graph.edges, graph.mentions, graph.episodes),
                                 (reference.edges, reference.mentions, reference.episodes))
                self.assertEqual(graph.nodes["dup"]["labels"], {gov.TOMBSTONE})

    def test_concurrent_change_after_prepare_is_not_reported_as_success(self):
        graph = merge_graph()
        request, _ = run(reviewed(graph, MERGE))

        def tamper(tag, params):
            if tag == "gov:edge-move" and params["uid"] == "dup-y":
                graph.edges["dup-y"]["props"]["fact"] = "rewritten by someone else"
        graph.after = tamper
        with self.assertRaises(gov.GovernanceError) as caught:
            run(gov.apply(g_of(graph), request, GROUP))
        self.assertEqual(caught.exception.code, "governance_origin_changed")
        self.assertEqual(graph.audits["merge-1"]["state"], "pending")
        self.assertEqual(graph.audits["merge-1"]["last_error"], "governance_origin_changed")

    def test_tombstone_refuses_node_that_gained_edges(self):
        graph = merge_graph()
        request, _ = run(reviewed(graph, MERGE))

        def late(tag, params):
            if tag == "gov:mention-drop":
                graph.edge("late", "dup", "y", "Written between steps.", ["e9"], "2026-10-24T00:00:00Z")
        graph.after = late
        with self.assertRaises(gov.GovernanceError) as caught:
            run(gov.apply(g_of(graph), request, GROUP))
        self.assertEqual(caught.exception.code, "governance_incomplete")
        self.assertIn("Entity", graph.nodes["dup"]["labels"])

    def test_rename_and_retype(self):
        graph = Graph()
        graph.node("znas", "Person", "ZNAS3")
        request, body = run(reviewed(graph, {"operation": "retype", "node": {"name": "znas3"}, "retype_to": "Tool"}, "t1"))
        self.assertEqual((body["plan"]["nodes"][0]["labels_remove"], body["plan"]["nodes"][0]["labels_add"]), (["Person"], ["Tool"]))
        run(gov.apply(g_of(graph), request, GROUP))
        self.assertEqual(graph.nodes["znas"]["labels"], {"Entity", "Tool"})
        self.assertEqual(graph.nodes["znas"]["props"]["previous_types"], ["Person"])
        request, _ = run(reviewed(graph, {"operation": "rename", "node": {"uuid": "znas"}, "rename_to": "ZNAS3 NAS"}, "r1"))
        result = run(gov.apply(g_of(graph), request, GROUP))
        self.assertTrue(result["verified"])
        props = graph.nodes["znas"]["props"]
        self.assertEqual((props["name"], props["previous_names"], props["governance_ids"]), ("ZNAS3 NAS", ["ZNAS3"], ["t1", "r1"]))
        self.assertEqual(props["name_embedding"], f32([0.1, 0.2, 0.3]))


class CliTests(unittest.TestCase):
    def setUp(self):
        # Keep the CLI summary out of run_all's last line, which reports the test count.
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_preview_writes_bound_template_and_review_sheet(self):
        graph = merge_graph()
        body = run(gov.preview(g_of(graph), MERGE, GROUP))
        calls = []

        def http(method, url, payload, token, **kw):
            calls.append((method, url, payload))
            return 200, json.loads(json.dumps(body))
        with tempfile.TemporaryDirectory() as tmp:
            scope = Path(tmp, "scope.json")
            scope.write_text(json.dumps(MERGE))
            code = cli.main(["preview", "--file", str(scope), "--out", f"{tmp}/plan.json", "--request-out",
                             f"{tmp}/pending.json", "--report", f"{tmp}/plan.md", "--url", "http://kg"], http=http)
            self.assertEqual(code, 0)
            self.assertEqual(calls[0][:2], ("POST", "http://kg/api/graph/governance/preview"))
            pending = json.loads(Path(tmp, "pending.json").read_text())
            self.assertEqual((pending["canonical"], pending["merged"]), ({"uuid": "canon"}, [{"uuid": "dup"}]))
            self.assertEqual(pending["expected_snapshot_sha256"], body["snapshot"]["snapshot_sha256"])
            self.assertEqual((pending["governance_id"], pending["reviewer"]), ("", ""))
            report = Path(tmp, "plan.md").read_text()
            for text in ("drop_self_loop", "absorb", "old-dup", "claude-mem:e3", "1 → 2"):
                self.assertIn(text, report)
            with self.assertRaises(gov.GovernanceError):  # an unreviewed template cannot be applied
                cli.main(["apply", "--file", f"{tmp}/pending.json"], http=http)
            pending.update(governance_id="merge-1", reason="same file", reviewer="unit")
            Path(tmp, "pending.json").write_text(json.dumps(pending))
            calls.clear()
            cli.main(["apply", "--file", f"{tmp}/pending.json", "--url", "http://kg"], http=lambda *a, **k: (calls.append(a), (200, {"status": "governed"}))[1])
            self.assertEqual(calls[0][0:3], ("POST", "http://kg/api/graph/governance", pending))

    def test_status_requires_id(self):
        with self.assertRaises(SystemExit):
            cli.main(["status"], http=lambda *a, **k: (200, {}))
        seen = []
        cli.main(["status", "--governance-id", "merge-1", "--url", "http://kg"],
                 http=lambda *a, **k: (seen.append(a), (200, {"status": "ok", "governance": {"state": "applied"}}))[1])
        self.assertEqual(seen[0][:2], ("GET", "http://kg/api/graph/governance?governance_id=merge-1"))

    def test_public_entry_routes_nodes(self):
        from tools import extract_cli
        from unittest.mock import patch
        with patch.object(cli, "main", return_value=0) as main:
            self.assertEqual(extract_cli.main(["nodes", "status", "--governance-id", "x"]), 0)
        main.assert_called_once_with(["status", "--governance-id", "x"])


if __name__ == "__main__":
    unittest.main()
