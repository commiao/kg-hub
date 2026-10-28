"""Audited node-level graph governance: identity changes and summary correction.

``utils.extraction_repair`` corrects what ONE source observation wrote. Duplicate
nodes span sources and campaigns, so this module works on node identity instead,
with the same protocol: a read-only preview returns the complete plan bound to a
snapshot digest; a human reviews it; apply re-reads the graph inside the writer
lock and refuses if that digest changed; status reads the durable audit back.

Invariants:
- No model is called. Plans are pure functions of (scope, snapshot).
- Moved edges keep their uuid and every stored property (vectors included): the
  relationship is re-created on the new endpoint from ``properties(e)`` in one
  atomic query, because FalkorDB cannot re-point an existing relationship.
- Duplicate edges after a move (same endpoints, name, fact and validity) collapse
  into the earliest-created edge, which keeps its first-source metadata and gains
  the union of all source endorsements. Absorbed edges and edges that would become
  self-loops leave the graph; their complete properties stay in the audit node.
- Merged nodes are never deleted. They lose the ``Entity`` label (so ingest name
  resolution and retrieval stop seeing them) and become ``MergedEntity`` with
  ``merged_into``; their own properties remain in place.
- Replays reuse the frozen plan. Every write step is guarded by uuid, endpoints
  and fact, and recognises its own completed state instead of re-resolving.
- Summary correction reads every complete backing episode, requires source-bound
  reviews and preserves all other node, edge, mention and source properties.
"""
from __future__ import annotations

import json
import re
import struct
import uuid
from datetime import datetime, timezone

from schema import ENTITY_TYPES
from utils.extraction_repair import encoded, digest, _ID, _SHA

VERSION = "graph-governance-v1"
OPERATIONS = ("merge", "rename", "retype", "split", "summary")
TOMBSTONE = "MergedEntity"
MAX_MERGED = 20
MAX_PARTITIONS = 20
MAX_EDGES = 1500
MAX_EPISODES = 3000
MAX_SUMMARY_EPISODES = 50
_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_VECTORS = ("name_embedding", "fact_embedding")
_READ_TAGS = frozenset({"gov:resolve", "gov:nodes", "gov:tombstones", "gov:foreign-rels", "gov:edges-out",
                        "gov:edges-in", "gov:anchor-out", "gov:anchor-in", "gov:mentions", "gov:episodes",
                        "gov:name-conflicts", "gov:pending", "gov:degree", "gov:audit-read", "gov:edge-locate",
                        "gov:edge-full", "gov:mention-exists", "gov:node-read", "gov:summary-episodes"})


class GovernanceError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409, detail=None):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail


# ---------- request parsing ----------

def _text(value, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\n" in value:
        raise GovernanceError("bad_request", f"{field} must be a single-line string of at most {limit} chars", 400)
    return value.strip()


def _uuid(value, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 120:
        raise GovernanceError("bad_request", f"invalid {field}", 400)
    return value


def _ref(value, field: str, *, bound: bool) -> dict:
    if isinstance(value, dict) and set(value) == {"uuid"}:
        return {"uuid": _uuid(value["uuid"], field)}
    if not bound and isinstance(value, dict) and set(value) == {"name"}:
        return {"name": _text(value["name"], field, 300)}
    raise GovernanceError("bad_request", f"{field} must be {{\"uuid\": ...}}"
                          + ("" if bound else " or {\"name\": ...}"), 400)


def _type(value, field: str) -> str:
    if value not in ENTITY_TYPES:
        raise GovernanceError("bad_request", f"{field} must be one of {sorted(ENTITY_TYPES)}", 400)
    return value


def _uuid_list(value, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_EDGES or len(set(map(str, value))) != len(value):
        raise GovernanceError("bad_request", f"{field} must be a list of distinct ids", 400)
    return sorted(_uuid(x, field) for x in value)


def parse_scope(document: dict, *, bound: bool) -> dict:
    """Normalise WHAT to govern. ``bound`` scopes name every node by uuid."""
    if not isinstance(document, dict):
        raise GovernanceError("bad_request", "expected JSON object", 400)
    op = document.get("operation")
    if op not in OPERATIONS:
        raise GovernanceError("bad_request", f"operation must be one of {OPERATIONS}", 400)
    scope = {"operation": op}
    if op == "merge":
        scope["canonical"] = _ref(document.get("canonical"), "canonical", bound=bound)
        merged = document.get("merged")
        if not isinstance(merged, list) or not 1 <= len(merged) <= MAX_MERGED:
            raise GovernanceError("bad_request", f"merged must list 1..{MAX_MERGED} nodes", 400)
        scope["merged"] = [_ref(x, "merged", bound=bound) for x in merged]
    else:
        scope["node"] = _ref(document.get("node"), "node", bound=bound)
    if op == "summary":
        allowed = {"operation", "node", "summary_to", "reviewed_episodes", "governance_id",
                   "expected_snapshot_sha256", "reason", "reviewer", "version"}
        if set(document) - allowed:
            raise GovernanceError("bad_request", "summary correction cannot also change identity or type", 400)
        scope["summary_to"] = _text(document.get("summary_to"), "summary_to", 2000)
        reviews = document.get("reviewed_episodes", [])
        if (not isinstance(reviews, list) or len(reviews) > MAX_SUMMARY_EPISODES or bound and not reviews):
            raise GovernanceError("bad_request", "summary apply requires explicit backing episode reviews", 400)
        scope["reviewed_episodes"] = []
        for item in reviews:
            if not isinstance(item, dict) or set(item) != {"uuid", "sha256", "note"}:
                raise GovernanceError("bad_request", "each episode review requires uuid, sha256 and note", 400)
            sha = item["sha256"]
            if not isinstance(sha, str) or not _SHA.fullmatch(sha):
                raise GovernanceError("bad_request", "episode review requires the full source snapshot SHA256", 400)
            note = _text(item["note"], "episode review note", 2000)
            if len(note) < 10:
                raise GovernanceError("bad_request", "episode review needs a concrete source-specific explanation", 400)
            scope["reviewed_episodes"].append({"uuid": _uuid(item["uuid"], "episode uuid"),
                                               "sha256": sha, "note": note})
        ids = [item["uuid"] for item in scope["reviewed_episodes"]]
        if len(set(ids)) != len(ids):
            raise GovernanceError("bad_request", "episode reviews cannot repeat a source", 400)
        scope["reviewed_episodes"].sort(key=lambda item: item["uuid"])
    elif "summary_to" in document or "reviewed_episodes" in document:
        raise GovernanceError("bad_request", "summary changes require the dedicated summary operation", 400)
    if op == "split":
        parts = document.get("partitions") or []
        if not isinstance(parts, list) or len(parts) > MAX_PARTITIONS or (bound and not parts):
            raise GovernanceError("bad_request", f"split requires 1..{MAX_PARTITIONS} partitions", 400)
        scope["partitions"] = []
        for index, part in enumerate(parts):
            if not isinstance(part, dict) or (part.get("target") is None) == (part.get("new_node") is None):
                raise GovernanceError("bad_request", f"partition {index} needs exactly one of target/new_node", 400)
            out = {"target": None, "new_node": None,
                   "edge_uuids": _uuid_list(part.get("edge_uuids"), "edge_uuids"),
                   "episode_uuids": _uuid_list(part.get("episode_uuids"), "episode_uuids")}
            if part.get("target") is not None:
                out["target"] = _ref(part["target"], "target", bound=bound)
            else:
                spec = part["new_node"]
                if not isinstance(spec, dict):
                    raise GovernanceError("bad_request", "new_node must be an object", 400)
                summary = spec.get("summary") or ""
                if not isinstance(summary, str) or len(summary) > 2000:
                    raise GovernanceError("bad_request", "new_node.summary must be a string", 400)
                out["new_node"] = {"name": _text(spec.get("name"), "new_node.name", 300),
                                   "type": _type(spec["type"], "new_node.type") if spec.get("type") else None,
                                   "summary": summary.strip()}
            if not out["edge_uuids"] and not out["episode_uuids"]:
                raise GovernanceError("bad_request", f"partition {index} selects no edges or episodes", 400)
            scope["partitions"].append(out)
        scope["keep_edge_uuids"] = _uuid_list(document.get("keep_edge_uuids"), "keep_edge_uuids")
        scope["retire_original"] = bool(document.get("retire_original", False))
    if op in ("rename", "merge", "split") and document.get("rename_to") is not None:
        scope["rename_to"] = _text(document["rename_to"], "rename_to", 300)
    if op in ("retype", "merge", "split") and document.get("retype_to") is not None:
        scope["retype_to"] = _type(document["retype_to"], "retype_to")
    if op == "rename" and "rename_to" not in scope:
        raise GovernanceError("bad_request", "rename requires rename_to", 400)
    if op == "retype" and "retype_to" not in scope:
        raise GovernanceError("bad_request", "retype requires retype_to", 400)
    if bound:
        ids = node_ids(scope)
        if len(set(ids)) != len(ids):
            raise GovernanceError("bad_request", "a node appears more than once in the scope", 400)
    return scope


def parse_request(document: dict) -> dict:
    scope = parse_scope(document, bound=True)
    gid, expected = document.get("governance_id"), document.get("expected_snapshot_sha256")
    if not isinstance(gid, str) or not _ID.fullmatch(gid):
        raise GovernanceError("bad_request", "invalid governance_id", 400)
    if not isinstance(expected, str) or not _SHA.fullmatch(expected):
        raise GovernanceError("bad_request", "expected_snapshot_sha256 is required", 400)
    for field in ("reason", "reviewer"):
        if not isinstance(document.get(field), str) or not document[field].strip():
            raise GovernanceError("bad_request", f"{field} is required", 400)
    if len(document["reason"]) > 2000 or len(document["reviewer"]) > 200:
        raise GovernanceError("bad_request", "reason/reviewer exceeds limits", 400)
    return {"version": VERSION, "governance_id": gid, "expected_snapshot_sha256": expected,
            "reason": document["reason"].strip(), "reviewer": document["reviewer"].strip(), "scope": scope}


def node_ids(scope: dict) -> list[str]:
    primary, anchors = _roles(scope)
    return primary + anchors


def _roles(scope: dict) -> tuple[list[str], list[str]]:
    """primary nodes lose edges (all their edges are read); anchors receive them."""
    op = scope["operation"]
    if op == "merge":
        return [m["uuid"] for m in scope["merged"]], [scope["canonical"]["uuid"]]
    if op == "split":
        return [scope["node"]["uuid"]], [p["target"]["uuid"] for p in scope["partitions"] if p["target"]]
    if op == "summary":
        return [scope["node"]["uuid"]], []
    return [], [scope["node"]["uuid"]]


# ---------- graph reads (preview is strictly read-only) ----------

async def _rows(driver, query, **params):
    rows, _, _ = await driver.execute_query(query, **params)
    return [dict(r) for r in rows]


def _vector_digest(value) -> dict:
    values = [float(x) for x in value]
    return {"sha256": digest(values), "dims": len(values)}


def _strip(props: dict) -> dict:
    out = dict(props)
    for key in _VECTORS:
        if out.get(key) is not None:
            out[key] = _vector_digest(out[key])
    return out


async def resolve_scope(driver, scope: dict, group_id: str) -> tuple[dict, list[dict]]:
    """Bind name references to uuids by the ingest rule (case-insensitive exact)."""
    resolution = []

    async def bind(ref, role):
        if "uuid" in ref:
            return ref
        rows = await _rows(driver,
            "/* gov:resolve */ MATCH (n:Entity) WHERE n.group_id=$g AND toLower(n.name)=$lname "
            "RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels ORDER BY n.uuid",
            g=group_id, lname=" ".join(ref["name"].split()).lower())
        if len(rows) != 1:
            raise GovernanceError("node_not_found" if not rows else "ambiguous_node",
                                  f"{role} {ref['name']!r} matched {len(rows)} nodes",
                                  404 if not rows else 409, detail={"candidates": rows})
        resolution.append({"role": role, "name": ref["name"], "uuid": rows[0]["uuid"],
                           "matched_name": rows[0]["name"], "labels": sorted(rows[0]["labels"])})
        return {"uuid": rows[0]["uuid"]}

    out = dict(scope)
    if scope["operation"] == "merge":
        out["canonical"] = await bind(scope["canonical"], "canonical")
        out["merged"] = [await bind(m, "merged") for m in scope["merged"]]
    else:
        out["node"] = await bind(scope["node"], "node")
    if scope["operation"] == "split":
        out["partitions"] = [{**p, "target": await bind(p["target"], "target") if p["target"] else None}
                             for p in scope["partitions"]]
    ids = node_ids(out)
    if len(set(ids)) != len(ids):
        raise GovernanceError("bad_request", "a node appears more than once in the scope", 400)
    return out, resolution


def _edge_row(row: dict) -> dict:
    return {"props": _strip(row["props"]), "source_uuid": row["source_uuid"], "target_uuid": row["target_uuid"],
            "source_name": row.get("source_name"), "target_name": row.get("target_name")}


def _new_names(scope: dict) -> list[str]:
    names = [scope["rename_to"]] if scope.get("rename_to") else []
    names += [p["new_node"]["name"] for p in scope.get("partitions", []) if p["new_node"]]
    return names


async def snapshot(driver, scope: dict, group_id: str) -> dict:
    primary, anchors = _roles(scope)
    ids = primary + anchors
    nodes = await _rows(driver,
        "/* gov:nodes */ MATCH (n:Entity) WHERE n.uuid IN $ids RETURN properties(n) AS props, labels(n) AS labels",
        ids=ids)
    tombstones = await _rows(driver,
        "/* gov:tombstones */ MATCH (n:" + TOMBSTONE + ") WHERE n.uuid IN $ids "
        "RETURN n.uuid AS uuid, n.merged_into AS merged_into, n.merged_by AS merged_by", ids=ids)
    foreign, edges, anchor_edges, mentions = [], {}, {}, []
    for uid in primary:
        foreign += [{"node_uuid": uid, **r} for r in await _rows(driver,
            "/* gov:foreign-rels */ MATCH (n:Entity {uuid:$u})-[r]-(o) "
            "WHERE NOT (type(r)='RELATES_TO' AND o:Entity) AND NOT (type(r)='MENTIONS' AND o:Episodic) "
            "RETURN type(r) AS type, count(r) AS count", u=uid) if r.get("count")]
        for tag, pattern in (("gov:edges-out", "(a:Entity {uuid:$u})-[e:RELATES_TO]->(b:Entity)"),
                             ("gov:edges-in", "(a:Entity)-[e:RELATES_TO]->(b:Entity {uuid:$u})")):
            for row in await _rows(driver,
                    "/* " + tag + " */ MATCH " + pattern + " RETURN properties(e) AS props, "
                    "a.uuid AS source_uuid, b.uuid AS target_uuid, a.name AS source_name, b.name AS target_name",
                    u=uid):
                edge = _edge_row(row)
                previous = edges.setdefault(edge["props"]["uuid"], edge)
                if (previous["source_uuid"], previous["target_uuid"]) != (edge["source_uuid"], edge["target_uuid"]):
                    raise GovernanceError("ambiguous_edge_uuid", f"edge uuid {edge['props']['uuid']} is not unique")
        if len(edges) > MAX_EDGES:
            raise GovernanceError("governance_too_large", f"more than {MAX_EDGES} edges in scope; split the request", 413)
    others = sorted({e[k] for e in edges.values() for k in ("source_uuid", "target_uuid")} | set(ids))
    if primary:
        for uid in anchors:
            for tag, pattern in (("gov:anchor-out", "(a:Entity {uuid:$u})-[e:RELATES_TO]->(b:Entity) WHERE b.uuid IN $others"),
                                 ("gov:anchor-in", "(a:Entity)-[e:RELATES_TO]->(b:Entity {uuid:$u}) WHERE a.uuid IN $others")):
                for row in await _rows(driver,
                        "/* " + tag + " */ MATCH " + pattern + " RETURN properties(e) AS props, "
                        "a.uuid AS source_uuid, b.uuid AS target_uuid, a.name AS source_name, b.name AS target_name",
                        u=uid, others=others):
                    edge = _edge_row(row)
                    if edge["props"]["uuid"] not in edges:
                        anchor_edges[edge["props"]["uuid"]] = edge
    episode_ids = {ep for e in list(edges.values()) + list(anchor_edges.values())
                   for ep in (e["props"].get("episodes") or [])}
    for uid in primary:
        mentions += [{"episode_uuid": r["episode_uuid"], "node_uuid": uid, "props": dict(r["props"])}
                     for r in await _rows(driver,
                         "/* gov:mentions */ MATCH (ep:Episodic)-[m:MENTIONS]->(n:Entity {uuid:$u}) "
                         "RETURN ep.uuid AS episode_uuid, properties(m) AS props", u=uid)]
    episode_ids |= {m["episode_uuid"] for m in mentions}
    if primary:
        scoped = sorted(episode_ids)
        for uid in anchors:
            mentions += [{"episode_uuid": r["episode_uuid"], "node_uuid": uid, "props": dict(r["props"])}
                         for start in range(0, len(scoped), 200)
                         for r in await _rows(driver,
                             "/* gov:mentions */ MATCH (ep:Episodic)-[m:MENTIONS]->(n:Entity {uuid:$u}) "
                             "WHERE ep.uuid IN $eps RETURN ep.uuid AS episode_uuid, properties(m) AS props",
                             u=uid, eps=scoped[start:start + 200])]
    if len(episode_ids) > MAX_EPISODES:
        raise GovernanceError("governance_too_large", f"more than {MAX_EPISODES} source episodes in scope", 413)
    if scope["operation"] == "summary" and len(episode_ids) > MAX_SUMMARY_EPISODES:
        raise GovernanceError("governance_too_large", "summary correction requires reviewing at most "
                              f"{MAX_SUMMARY_EPISODES} complete backing episodes", 413)
    episodes, wanted = [], sorted(episode_ids)
    for start in range(0, len(wanted), 200):
        if scope["operation"] == "summary":
            rows = await _rows(driver,
                "/* gov:summary-episodes */ MATCH (ep:Episodic) WHERE ep.uuid IN $eps "
                "RETURN properties(ep) AS props", eps=wanted[start:start + 200])
            episodes += [_strip(row["props"]) for row in rows]
            continue
        episodes += await _rows(driver,
            "/* gov:episodes */ MATCH (ep:Episodic) WHERE ep.uuid IN $eps RETURN ep.uuid AS uuid, ep.name AS name, "
            "ep.source_description AS source_description, ep.valid_at AS valid_at, ep.group_id AS group_id, "
            "ep.extractor_model AS extractor_model, ep.extractor_tool AS extractor_tool, ep.run_id AS run_id, "
            "ep.entity_edges AS entity_edges, ep.archived AS archived", eps=wanted[start:start + 200])
    conflicts = {}
    for name in _new_names(scope):
        conflicts[name] = await _rows(driver,
            "/* gov:name-conflicts */ MATCH (n:Entity) WHERE n.group_id=$g AND toLower(n.name)=$lname "
            "RETURN n.uuid AS uuid, n.name AS name ORDER BY n.uuid", g=group_id, lname=" ".join(name.split()).lower())
    pending = await _rows(driver,
        "/* gov:pending */ MATCH (r:GraphGovernance) WHERE r.state='pending' "
        "AND any(x IN coalesce(r.node_uuids,[]) WHERE x IN $ids) RETURN r.governance_id AS governance_id "
        "ORDER BY r.governance_id", ids=ids)
    found = {e["uuid"] for e in episodes}
    if scope["operation"] == "summary" and len(found) != len(episodes):
        raise GovernanceError("ambiguous_episode_uuid", "summary backing episode UUIDs must be unique")
    out = {"version": VERSION, "group_id": group_id, "scope": scope,
           "nodes": sorted(({"uuid": n["props"]["uuid"], "labels": sorted(n["labels"]), "props": _strip(n["props"])}
                            for n in nodes), key=lambda n: n["uuid"]),
           "tombstones": sorted(tombstones, key=lambda n: n["uuid"]),
           "foreign_relationships": sorted(foreign, key=lambda r: (r["node_uuid"], r["type"])),
           "edges": sorted(edges.values(), key=lambda e: e["props"]["uuid"]),
           "anchor_edges": sorted(anchor_edges.values(), key=lambda e: e["props"]["uuid"]),
           "mentions": sorted(mentions, key=lambda m: (m["episode_uuid"], m["node_uuid"], str(m["props"].get("uuid")))),
           "episodes": sorted((dict(e) if scope["operation"] == "summary" else
                               {**e, "entity_edges": list(e.get("entity_edges") or [])} for e in episodes),
                              key=lambda e: e["uuid"]),
           "missing_episodes": sorted(episode_ids - found),
           "name_conflicts": conflicts,
           "pending_governance": [r["governance_id"] for r in pending]}
    out["snapshot_sha256"] = digest(out)
    return out


# ---------- planning (pure) ----------

def _time_key(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (0, parsed.timestamp(), value)
        except ValueError:
            pass
    return (1, 0.0, "" if value is None else str(value))


def _ordered_union(*lists) -> list:
    out = []
    for values in lists:
        for value in values or []:
            if value not in out:
                out.append(value)
    return out


def _type_labels(labels) -> list[str]:
    return sorted(label for label in labels if label in ENTITY_TYPES)


def _edge_key(src: str, dst: str, props: dict) -> tuple:
    return (src, props.get("name"), dst, props.get("fact"), props.get("invalid_at"), props.get("expired_at"))


def _node_set(node: dict, updates: dict) -> dict:
    """Absolute property values (idempotent on replay) for list-valued history fields."""
    props = node["props"]
    out = {}
    for key, values in updates.items():
        out[key] = _ordered_union(props.get(key), values)
    return out


def build_plan(scope: dict, snap: dict) -> dict:
    """Return the complete, deterministic plan or raise a reviewable GovernanceError."""
    op = scope["operation"]
    primary, anchors = _roles(scope)
    nodes = {n["uuid"]: n for n in snap["nodes"]}
    tomb = {t["uuid"]: t for t in snap["tombstones"]}
    for uid in primary + anchors:
        if uid in tomb:
            raise GovernanceError("already_merged", f"node {uid} was already merged into {tomb[uid].get('merged_into')}")
        if uid not in nodes:
            raise GovernanceError("node_not_found", f"node {uid} is not a live Entity", 404)
        if nodes[uid]["props"].get("group_id") not in (None, snap["group_id"]):
            raise GovernanceError("foreign_node", f"node {uid} belongs to another group")
        for label in nodes[uid]["labels"]:
            if not _LABEL.fullmatch(label):
                raise GovernanceError("unsupported_label", f"node {uid} has an unsupported label {label!r}")
    if snap["pending_governance"]:
        raise GovernanceError("governance_pending", "an unfinished governance owns these nodes: "
                              + ", ".join(snap["pending_governance"]))
    if snap["foreign_relationships"]:
        raise GovernanceError("unsupported_relationship", "scope nodes carry relationships other than "
                              "Entity RELATES_TO / Episodic MENTIONS", detail=snap["foreign_relationships"])
    allowed = set(primary + anchors)
    for name, rows in snap["name_conflicts"].items():
        clash = [r for r in rows if r["uuid"] not in allowed]
        if clash:
            raise GovernanceError("name_conflict", f"{name!r} already names another node; merge instead of renaming",
                                  detail={"name": name, "nodes": clash})
    new_names = [p["new_node"]["name"].lower() for p in scope.get("partitions", []) if p["new_node"]]
    if len(set(new_names)) != len(new_names):
        raise GovernanceError("bad_request", "two partitions create the same node name", 400)
    for edge in snap["edges"] + snap["anchor_edges"]:
        if edge["props"].get("group_id") not in (None, snap["group_id"]):
            raise GovernanceError("foreign_edge", f"edge {edge['props']['uuid']} belongs to another group")

    names = {uid: n["props"].get("name") for uid, n in nodes.items()}
    node_plans, warnings = [], []
    partition_nodes = []  # target uuid per partition
    if op == "split":
        original = nodes[primary[0]]
        for index, part in enumerate(scope["partitions"]):
            if part["target"]:
                partition_nodes.append(part["target"]["uuid"])
                continue
            spec = part["new_node"]
            uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{VERSION}:{primary[0]}:{spec['name'].lower()}"))
            kind = spec["type"] or (_type_labels(original["labels"]) or [None])[0]
            if kind is None:
                raise GovernanceError("bad_request", f"partition {index} needs new_node.type", 400)
            names[uid] = spec["name"]
            partition_nodes.append(uid)
            node_plans.append({"uuid": uid, "action": "create", "name": spec["name"], "labels_add": ["Entity", kind],
                               "labels_remove": [], "embed_name": True,
                               "set": {"uuid": uid, "name": spec["name"], "summary": spec["summary"],
                                       "group_id": snap["group_id"], "split_from": [primary[0]]}})

    # edge endpoint mapping
    assignment = {}
    if op == "merge":
        target_of = {m: anchors[0] for m in primary}
        for edge in snap["edges"]:
            assignment[edge["props"]["uuid"]] = None
    else:
        target_of = {}
    if op == "split":
        explicit = {}
        for index, part in enumerate(scope["partitions"]):
            for euid in part["edge_uuids"]:
                if euid in explicit:
                    raise GovernanceError("ambiguous_edge", f"edge {euid} is assigned to two partitions", 400)
                explicit[euid] = index
        keep = set(scope.get("keep_edge_uuids") or [])
        if keep & set(explicit):
            raise GovernanceError("ambiguous_edge", "an edge is both kept and assigned to a partition", 400,
                                  detail={"edge_uuids": sorted(keep & set(explicit))})
        known = {e["props"]["uuid"] for e in snap["edges"]}
        unknown = sorted((set(explicit) | keep) - known)
        if unknown:
            raise GovernanceError("foreign_edge", "partition edge_uuids are not edges of the split node",
                                  detail={"edge_uuids": unknown})
        ambiguous = []
        for edge in snap["edges"]:
            euid = edge["props"]["uuid"]
            if euid in explicit:
                assignment[euid] = explicit[euid]
                continue
            if euid in keep:
                continue
            eps = set(edge["props"].get("episodes") or [])
            hits = [i for i, part in enumerate(scope["partitions"]) if eps and eps <= set(part["episode_uuids"])]
            touched = [i for i, part in enumerate(scope["partitions"]) if eps & set(part["episode_uuids"])]
            if len(hits) == 1 and len(touched) == 1:
                assignment[euid] = hits[0]
            elif touched:
                ambiguous.append({"uuid": euid, "partitions": touched, "episodes": sorted(eps)})
        if ambiguous:
            raise GovernanceError("ambiguous_edge", "edges endorsed across partitions must be assigned by a "
                                  "partition's edge_uuids or kept by keep_edge_uuids",
                                  400, detail={"edges": ambiguous})

    def remap(edge):
        src, dst = edge["source_uuid"], edge["target_uuid"]
        if op == "merge":
            return target_of.get(src, src), target_of.get(dst, dst)
        index = assignment.get(edge["props"]["uuid"])
        if index is None:
            return src, dst
        target = partition_nodes[index]
        return (target if src == primary[0] else src), (target if dst == primary[0] else dst)

    edge_plans, residual, groups = [], [], {}
    for edge in snap["edges"]:
        props = edge["props"]
        new_src, new_dst = remap(edge)
        if (new_src, new_dst) == (edge["source_uuid"], edge["target_uuid"]):
            residual.append(props["uuid"])
            continue
        if new_src == new_dst:
            edge_plans.append({"uuid": props["uuid"], "action": "drop_self_loop", "name": props.get("name"),
                               "fact": props.get("fact"), "from": [edge["source_uuid"], edge["target_uuid"]],
                               "to": [new_src, new_dst], "episodes": list(props.get("episodes") or [])})
            continue
        groups.setdefault(_edge_key(new_src, new_dst, props), []).append((edge, new_src, new_dst, True))
    for edge in snap["anchor_edges"]:
        key = _edge_key(edge["source_uuid"], edge["target_uuid"], edge["props"])
        if key in groups:
            groups[key].append((edge, edge["source_uuid"], edge["target_uuid"], False))
    for key in sorted(groups, key=lambda k: tuple("" if v is None else str(v) for v in k)):
        members = sorted(groups[key], key=lambda m: (_time_key(m[0]["props"].get("created_at")),
                                                    0 if not m[3] else 1, m[0]["props"]["uuid"]))
        survivor, s_src, s_dst, s_moving = members[0]
        union = _ordered_union(*(m[0]["props"].get("episodes") for m in members))
        sp = survivor["props"]
        base = {"name": sp.get("name"), "fact": sp.get("fact"), "to": [s_src, s_dst]}
        if s_moving:
            edge_plans.append({**base, "uuid": sp["uuid"], "action": "move",
                               "from": [survivor["source_uuid"], survivor["target_uuid"]],
                               "episodes": list(sp.get("episodes") or []), "episodes_after": union})
        elif len(union) > len(sp.get("episodes") or []):
            edge_plans.append({**base, "uuid": sp["uuid"], "action": "extend",
                               "from": [s_src, s_dst], "episodes": list(sp.get("episodes") or []),
                               "episodes_after": union})
        for edge, _, _, moving in members[1:]:
            props = edge["props"]
            edge_plans.append({"uuid": props["uuid"], "action": "absorb", "name": props.get("name"),
                               "fact": props.get("fact"), "from": [edge["source_uuid"], edge["target_uuid"]],
                               "to": [s_src, s_dst], "survivor_uuid": sp["uuid"],
                               "episodes": list(props.get("episodes") or [])})
    order = {"move": 0, "extend": 1, "absorb": 2, "drop_self_loop": 3}
    edge_plans.sort(key=lambda e: (order[e["action"]], e["uuid"]))
    for plan_edge in edge_plans:
        plan_edge["from_names"] = [names.get(x) or _counterpart_name(snap, plan_edge["uuid"], x) for x in plan_edge["from"]]
        plan_edge["to_names"] = [names.get(x) or _counterpart_name(snap, plan_edge["uuid"], x) for x in plan_edge["to"]]

    # mentions
    existing = {(m["episode_uuid"], m["node_uuid"]) for m in snap["mentions"]}
    mention_plans, present = [], set(existing)
    if op == "merge":
        for m in snap["mentions"]:
            if m["node_uuid"] not in primary:
                continue
            ep, target = m["episode_uuid"], anchors[0]
            if (ep, target) in present:
                mention_plans.append({"episode_uuid": ep, "action": "drop", "from": m["node_uuid"], "keep": target})
            else:
                mention_plans.append({"episode_uuid": ep, "action": "move", "from": m["node_uuid"], "to": target})
                present.add((ep, target))
    elif op == "split":
        if scope.get("retire_original") and residual:
            raise GovernanceError("retire_requires_all_edges", "retire_original needs every edge assigned",
                                  400, detail={"residual_edges": residual})
        source = primary[0]
        related, keeps = {}, set()
        for plan_edge in edge_plans:
            targets = {x for x in plan_edge["to"] if x in partition_nodes}
            for ep in plan_edge["episodes"]:
                related.setdefault(ep, set()).update(targets)
        residual_set = set(residual)
        for edge in snap["edges"]:
            if edge["props"]["uuid"] in residual_set:
                keeps.update(edge["props"].get("episodes") or [])
        for m in snap["mentions"]:
            if m["node_uuid"] != source:
                continue
            ep = m["episode_uuid"]
            targets = sorted(related.get(ep, set()))
            if not targets and scope.get("retire_original"):
                targets = sorted({partition_nodes[i] for i, p in enumerate(scope["partitions"]) if ep in p["episode_uuids"]})
                if not targets:
                    raise GovernanceError("unassigned_mention", f"episode {ep} mentions the retired node but no "
                                          "partition takes it; list it in a partition's episode_uuids", 400)
            keep_here = ep in keeps or not targets
            if len(targets) == 1 and not keep_here and (ep, targets[0]) not in present:
                mention_plans.append({"episode_uuid": ep, "action": "move", "from": source, "to": targets[0]})
                present.add((ep, targets[0]))
                continue
            for target in targets:
                if (ep, target) not in present:
                    mention_plans.append({"episode_uuid": ep, "action": "add", "to": target,
                                          "mention_uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{VERSION}:{ep}:{target}"))})
                    present.add((ep, target))
            if not keep_here:
                mention_plans.append({"episode_uuid": ep, "action": "drop", "from": source, "keep": targets[0]})
    mention_plans.sort(key=lambda m: (m["episode_uuid"], m["action"], m.get("from") or "", m.get("to") or ""))

    # episode edge references
    refs = {}
    removed = {e["uuid"]: e for e in edge_plans if e["action"] in ("absorb", "drop_self_loop")}
    for episode in snap["episodes"]:
        stored = episode.get("entity_edges") or []
        remove = sorted(x for x in stored if x in removed)
        add = _ordered_union([removed[x]["survivor_uuid"] for x in remove if removed[x]["action"] == "absorb"])
        add = [x for x in add if x not in stored]
        if remove or add:
            refs[episode["uuid"]] = {"episode_uuid": episode["uuid"], "remove": remove, "add": add}

    # node changes
    def retype(node, kind):
        old = _type_labels(node["labels"])
        return [x for x in old if x != kind], ([kind] if kind not in old else [])

    def update(uid, *, rename=None, kind=None, history=None):
        node = nodes[uid]
        sets, remove, add = {}, [], []
        history = dict(history or {})
        if rename and rename != node["props"].get("name"):
            sets["name"] = rename
            history["previous_names"] = [node["props"].get("name")]
        if kind:
            remove, add = retype(node, kind)
            if remove or add:
                history["previous_types"] = _type_labels(node["labels"])
        sets.update(_node_set(node, history))
        node_plans.append({"uuid": uid, "action": "update", "name": sets.get("name", node["props"].get("name")),
                           "labels_remove": remove, "labels_add": add, "embed_name": "name" in sets, "set": sets})

    if op == "merge":
        canonical = nodes[anchors[0]]
        update(anchors[0], rename=scope.get("rename_to"), kind=scope.get("retype_to"),
               history={"merged_from": primary, "merged_names": [names[m] for m in primary]})
        kinds = {tuple(_type_labels(nodes[u]["labels"])) for u in primary + anchors}
        if len(kinds) > 1 and not scope.get("retype_to"):
            warnings.append({"code": "type_mismatch", "message": "merged nodes have different types; the canonical "
                             "type is kept unless retype_to is set",
                             "types": {u: _type_labels(nodes[u]["labels"]) for u in primary + anchors}})
        for m in primary:
            node_plans.append({"uuid": m, "action": "tombstone", "name": names[m], "labels_remove": nodes[m]["labels"],
                               "labels_add": [TOMBSTONE], "embed_name": False,
                               "set": {"merged_into": anchors[0], "merged_labels": nodes[m]["labels"]},
                               "summary": nodes[m]["props"].get("summary")})
        if any(nodes[m]["props"].get("summary") for m in primary):
            warnings.append({"code": "summaries_not_combined", "message": "the canonical summary is kept; merged "
                             "summaries stay on the MergedEntity nodes", "canonical_summary": canonical["props"].get("summary")})
    elif op in ("rename", "retype"):
        update(anchors[0], rename=scope.get("rename_to"), kind=scope.get("retype_to"))
        if not node_plans[-1]["set"].get("name") and not node_plans[-1]["labels_add"] and not node_plans[-1]["labels_remove"]:
            raise GovernanceError("nothing_to_change", "the node already has this name/type", 400)
    elif op == "summary":
        source = primary[0]
        if len(snap["nodes"]) != 1:
            raise GovernanceError("ambiguous_node_uuid", "summary correction requires one unique live node")
        old_summary = nodes[source]["props"].get("summary")
        if not isinstance(old_summary, str) or not old_summary.strip():
            raise GovernanceError("summary_missing", "summary correction requires an existing stored summary", 400)
        if old_summary == scope["summary_to"]:
            raise GovernanceError("nothing_to_change", "the node already has this summary", 400)
        if not snap["episodes"] or snap["missing_episodes"]:
            raise GovernanceError("summary_sources_incomplete", "every summary backing source must exist")
        for ep in snap["episodes"]:
            if not isinstance(ep.get("content"), str) or not ep["content"].strip():
                raise GovernanceError("summary_sources_incomplete", f"episode {ep['uuid']} lacks source content")
        reviews = {item["uuid"]: item for item in scope["reviewed_episodes"]}
        if reviews:
            if set(reviews) != {ep["uuid"] for ep in snap["episodes"]}:
                raise GovernanceError("summary_review_incomplete", "review every backing episode, not one selected source")
            for ep in snap["episodes"]:
                if reviews[ep["uuid"]]["sha256"] != digest(ep):
                    raise GovernanceError("stale_summary_evidence", f"episode {ep['uuid']} changed after source review")
        else:
            warnings.append({"code": "summary_review_required", "message": "listing only: review each complete "
                             "episode below and bind its snapshot SHA256 and a concrete note before apply"})
        node_plans.append({"uuid": source, "action": "update", "name": names[source],
                           "labels_remove": [], "labels_add": [], "embed_name": False,
                           "set": {"summary": scope["summary_to"]}, "previous_summary": old_summary})
    else:
        source = primary[0]
        for target in sorted(set(partition_nodes) & set(nodes)):
            update(target, history={"split_from": [source]})
        if scope.get("retire_original"):
            if scope.get("rename_to") or scope.get("retype_to"):
                raise GovernanceError("bad_request", "a retired original cannot also be renamed/retyped", 400)
            node_plans.append({"uuid": source, "action": "tombstone", "name": names[source],
                               "labels_remove": nodes[source]["labels"], "labels_add": [TOMBSTONE], "embed_name": False,
                               "set": {"merged_into": None, "split_into": partition_nodes,
                                       "merged_labels": nodes[source]["labels"]},
                               "summary": nodes[source]["props"].get("summary")})
        else:
            update(source, rename=scope.get("rename_to"), kind=scope.get("retype_to"),
                   history={"split_into": partition_nodes})
    order = {"create": 0, "update": 1, "tombstone": 2}
    node_plans.sort(key=lambda n: (order[n["action"]], n["uuid"]))

    # affected sources
    touched_eps = {}
    if op == "summary":
        touched_eps = {ep["uuid"]: {"edges": sum(ep["uuid"] in (edge["props"].get("episodes") or [])
                                                    for edge in snap["edges"])} for ep in snap["episodes"]}
    for plan_edge in edge_plans:
        for ep in plan_edge["episodes"]:
            touched_eps.setdefault(ep, {"edges": 0})["edges"] += 1
    for m in mention_plans:
        touched_eps.setdefault(m["episode_uuid"], {"edges": 0})
    by_uuid = {e["uuid"]: e for e in snap["episodes"]}
    sources = []
    for ep in sorted(touched_eps):
        info = by_uuid.get(ep)
        sources.append({"episode_uuid": ep, "edges": touched_eps[ep]["edges"], "missing": info is None,
                        **({k: info.get(k) for k in ("name", "source_description", "valid_at", "extractor_model",
                                                      "extractor_tool", "run_id", "archived")} if info else {})})
    counts = {}
    for plan_edge in edge_plans:
        counts[plan_edge["action"]] = counts.get(plan_edge["action"], 0) + 1
    summary = {"edges_move": counts.get("move", 0), "edges_extend": counts.get("extend", 0),
               "edges_absorb": counts.get("absorb", 0), "edges_drop_self_loop": counts.get("drop_self_loop", 0),
               "edges_residual": len(residual), "mentions": len(mention_plans), "episode_refs": len(refs),
               "sources": len(sources), "nodes": {a: sum(n["action"] == a for n in node_plans)
                                                  for a in ("create", "update", "tombstone")}}
    if snap["missing_episodes"]:
        warnings.append({"code": "dangling_episode_refs", "message": "edges reference episodes that no longer exist",
                         "episodes": snap["missing_episodes"]})
    plan = {"version": VERSION, "operation": op, "snapshot_sha256": snap["snapshot_sha256"],
            "nodes": node_plans, "edges": edge_plans, "residual_edges": sorted(residual),
            "mentions": mention_plans, "episode_refs": [refs[k] for k in sorted(refs)],
            "sources": sources, "summary": summary, "warnings": warnings,
            "applicable": not (op == "split" and not scope.get("partitions"))
                          and not (op == "summary" and not scope.get("reviewed_episodes"))}
    if op == "summary":
        plan["summary_sources"] = [{"uuid": ep["uuid"], "sha256": digest(ep), "content": ep["content"],
                                     "review_note": next((r["note"] for r in scope["reviewed_episodes"]
                                                         if r["uuid"] == ep["uuid"]), None)} for ep in snap["episodes"]]
    plan["plan_sha256"] = digest(plan)
    return plan


def _counterpart_name(snap: dict, edge_uuid: str, node_uuid: str):
    for edge in snap["edges"] + snap["anchor_edges"]:
        if edge["props"]["uuid"] == edge_uuid:
            if edge["source_uuid"] == node_uuid:
                return edge.get("source_name")
            if edge["target_uuid"] == node_uuid:
                return edge.get("target_name")
    return None


# ---------- preview ----------

async def preview(g, document: dict, group_id: str) -> dict:
    scope = parse_scope(document, bound=False)
    scope, resolution = await resolve_scope(g.driver, scope, group_id)
    snap = await snapshot(g.driver, scope, group_id)
    out = {"status": "governance_preview", "version": VERSION, "scope": scope, "resolution": resolution,
           "snapshot": snap}
    try:
        out["plan"] = build_plan(scope, snap)
    except GovernanceError as exc:
        exc.detail = {"error_detail": exc.detail, "scope": scope, "resolution": resolution, "snapshot": snap}
        raise
    if document.get("governance_id"):
        request = parse_request(document)
        if request["scope"] != scope:
            raise GovernanceError("bad_request", "reviewed requests must bind every node by uuid", 400)
        if request["expected_snapshot_sha256"] != snap["snapshot_sha256"]:
            raise GovernanceError("stale_snapshot", "graph changed after the reviewed preview",
                                  detail={"snapshot_sha256": snap["snapshot_sha256"]})
        out["request_sha256"] = digest(request)
    return out


# ---------- apply ----------

async def audit(driver, governance_id: str) -> dict | None:
    rows = await _rows(driver,
        "/* gov:audit-read */ MATCH (r:GraphGovernance {governance_id:$gid}) RETURN properties(r) AS props",
        gid=governance_id)
    if not rows:
        return None
    if len(rows) != 1:
        raise GovernanceError("ambiguous_governance", "governance id is not unique")
    out = dict(rows[0]["props"])
    for name in ("request", "before", "plan", "materialization", "deleted", "result"):
        raw = out.pop(name + "_json", None)
        if raw is not None:
            out[name] = json.loads(raw)
    return out


async def _materialize(g, plan: dict) -> dict:
    """Timestamps and name vectors are fixed once, in the durable prepared audit."""
    now = datetime.now(timezone.utc).isoformat()
    embed = [n for n in plan["nodes"] if n["embed_name"]]
    vectors = await g.embedder.create_batch([n["name"] for n in embed]) if embed else []
    return {"prepared_at": now,
            "name_embeddings": {n["uuid"]: [float(x) for x in v] for n, v in zip(embed, vectors)}}


async def _deleted_edges(driver, plan: dict) -> list[dict]:
    out = []
    for edge in plan["edges"]:
        if edge["action"] not in ("absorb", "drop_self_loop"):
            continue
        rows = await _rows(driver,
            "/* gov:edge-full */ MATCH (a:Entity)-[e:RELATES_TO {uuid:$uid}]->(b:Entity) "
            "RETURN properties(e) AS props, a.uuid AS source_uuid, b.uuid AS target_uuid", uid=edge["uuid"])
        if len(rows) != 1:
            raise GovernanceError("governance_conflict", f"edge {edge['uuid']} changed before prepare", 503)
        out.append({"props": {k: ([float(x) for x in v] if k in _VECTORS and v is not None else v)
                              for k, v in rows[0]["props"].items()},
                    "source_uuid": rows[0]["source_uuid"], "target_uuid": rows[0]["target_uuid"]})
    return out


async def _locate(driver, uid: str) -> list[dict]:
    return await _rows(driver,
        "/* gov:edge-locate */ MATCH (a:Entity)-[e:RELATES_TO {uuid:$uid}]->(b:Entity) "
        "RETURN a.uuid AS source_uuid, b.uuid AS target_uuid, properties(e) AS props", uid=uid)


def _at(rows, endpoints, episodes=()) -> bool:
    return (len(rows) == 1 and [rows[0]["source_uuid"], rows[0]["target_uuid"]] == list(endpoints)
            and set(episodes).issubset(set(rows[0]["props"].get("episodes") or [])))


def _labels(labels) -> str:
    for label in labels:
        if not _LABEL.fullmatch(label):
            raise GovernanceError("unsupported_label", f"refusing label {label!r}", 400)
    return ":" + ":".join(labels)


async def _apply_plan(driver, request: dict, before: dict, plan: dict, mat: dict, group_id: str) -> dict:
    gid, now = request["governance_id"], mat["prepared_at"]
    history = {"governance_ids": [gid]}
    stored = {n["uuid"]: n["props"] for n in before["nodes"]}
    for node in plan["nodes"]:
        if node["action"] != "create":
            continue
        await _rows(driver,
            "/* gov:node-create */ MERGE (n:Entity {uuid:$uid}) ON CREATE SET n" + _labels(node["labels_add"][1:]) +
            ", n += $props, n.name_embedding=vecf32($embedding) RETURN n.uuid AS uuid",
            uid=node["uuid"], props={**node["set"], "created_at": now, **history},
            embedding=mat["name_embeddings"][node["uuid"]])
    for edge in plan["edges"]:
        if edge["action"] != "move":
            continue
        extra = edge.get("episodes_after") or []
        rows = await _rows(driver,
            "/* gov:edge-move */ MATCH (a:Entity {uuid:$src})-[e:RELATES_TO {uuid:$uid}]->(b:Entity {uuid:$dst}) "
            "WHERE e.name=$name AND e.fact=$fact "
            "MATCH (s:Entity {uuid:$new_src}) MATCH (t:Entity {uuid:$new_dst}) "
            "WITH e, s, t, properties(e) AS p DELETE e "
            "CREATE (s)-[n:RELATES_TO]->(t) SET n = p "
            "SET n.episodes = coalesce(p.episodes, []) + [x IN $extra WHERE NOT x IN coalesce(p.episodes, [])] "
            "RETURN n.uuid AS uuid",
            src=edge["from"][0], dst=edge["from"][1], uid=edge["uuid"], name=edge["name"], fact=edge["fact"],
            new_src=edge["to"][0], new_dst=edge["to"][1], extra=extra)
        if len(rows) != 1 and not _at(await _locate(driver, edge["uuid"]), edge["to"], extra):
            raise GovernanceError("governance_conflict", f"edge {edge['uuid']} is neither at its origin nor moved", 503)
    for edge in plan["edges"]:
        if edge["action"] != "extend":
            continue
        rows = await _rows(driver,
            "/* gov:edge-extend */ MATCH (a:Entity {uuid:$src})-[e:RELATES_TO {uuid:$uid}]->(b:Entity {uuid:$dst}) "
            "WHERE e.name=$name AND e.fact=$fact "
            "SET e.episodes = coalesce(e.episodes, []) + [x IN $extra WHERE NOT x IN coalesce(e.episodes, [])] "
            "RETURN e.uuid AS uuid", src=edge["from"][0], dst=edge["from"][1], uid=edge["uuid"],
            name=edge["name"], fact=edge["fact"], extra=edge["episodes_after"])
        if len(rows) != 1:
            raise GovernanceError("governance_conflict", f"surviving edge {edge['uuid']} changed", 503)
    for edge in plan["edges"]:
        if edge["action"] != "absorb":
            continue
        # The survivor must already carry every endorsement before the duplicate goes.
        rows = await _rows(driver,
            "/* gov:edge-absorb */ MATCH (sa:Entity {uuid:$s_src})-[s:RELATES_TO {uuid:$survivor}]->(sb:Entity {uuid:$s_dst}) "
            "WHERE all(x IN $eps WHERE x IN coalesce(s.episodes, [])) "
            "MATCH (a:Entity {uuid:$src})-[e:RELATES_TO {uuid:$uid}]->(b:Entity {uuid:$dst}) "
            "WHERE e.name=$name AND e.fact=$fact DELETE e RETURN count(e) AS deleted",
            s_src=edge["to"][0], s_dst=edge["to"][1], survivor=edge["survivor_uuid"], eps=edge["episodes"],
            src=edge["from"][0], dst=edge["from"][1], uid=edge["uuid"], name=edge["name"], fact=edge["fact"])
        if not (rows and rows[0].get("deleted")) and await _locate(driver, edge["uuid"]):
            raise GovernanceError("governance_conflict", f"duplicate edge {edge['uuid']} could not be absorbed", 503)
        if not _at(await _locate(driver, edge["survivor_uuid"]), edge["to"], edge["episodes"]):
            raise GovernanceError("governance_conflict", f"survivor {edge['survivor_uuid']} lacks endorsements", 503)
    for edge in plan["edges"]:
        if edge["action"] != "drop_self_loop":
            continue
        await _rows(driver,
            "/* gov:edge-drop */ MATCH (a:Entity {uuid:$src})-[e:RELATES_TO {uuid:$uid}]->(b:Entity {uuid:$dst}) "
            "WHERE e.name=$name AND e.fact=$fact DELETE e RETURN count(e) AS deleted",
            src=edge["from"][0], dst=edge["from"][1], uid=edge["uuid"], name=edge["name"], fact=edge["fact"])
        if await _locate(driver, edge["uuid"]):
            raise GovernanceError("governance_conflict", f"self-loop edge {edge['uuid']} changed", 503)
    for ref in plan["episode_refs"]:
        await _rows(driver,
            "/* gov:episode-refs */ MATCH (e:Episodic {uuid:$ep}) "
            "WITH e, [x IN coalesce(e.entity_edges, []) WHERE NOT x IN $remove] AS kept "
            "SET e.entity_edges = kept + [x IN $add WHERE NOT x IN kept] RETURN e.uuid AS uuid",
            ep=ref["episode_uuid"], remove=ref["remove"], add=ref["add"])
    for m in plan["mentions"]:
        if m["action"] == "move":
            rows = await _rows(driver,
                "/* gov:mention-move */ MATCH (ep:Episodic {uuid:$ep})-[m:MENTIONS]->(o:Entity {uuid:$old}) "
                "MATCH (t:Entity {uuid:$to}) WITH ep, m, t, properties(m) AS p DELETE m "
                "CREATE (ep)-[x:MENTIONS]->(t) SET x = p RETURN count(x) AS moved",
                ep=m["episode_uuid"], to=m["to"], old=m["from"])
            if not (rows and rows[0].get("moved")) and not await _mention(driver, m["episode_uuid"], m["to"]):
                raise GovernanceError("governance_conflict", f"mention of episode {m['episode_uuid']} changed", 503)
        elif m["action"] == "add":
            await _rows(driver,
                "/* gov:mention-add */ MATCH (ep:Episodic {uuid:$ep}) MATCH (t:Entity {uuid:$to}) "
                "MERGE (ep)-[m:MENTIONS {uuid:$mid}]->(t) "
                "ON CREATE SET m.group_id=$g, m.created_at=$now, m.governance_id=$gid RETURN count(m) AS added",
                ep=m["episode_uuid"], to=m["to"], mid=m["mention_uuid"], g=group_id, now=now, gid=gid)
    for m in plan["mentions"]:
        if m["action"] == "drop":
            # Only after the kept mention exists: an episode never loses its link to the concept.
            await _rows(driver,
                "/* gov:mention-drop */ MATCH (ep:Episodic {uuid:$ep})-[:MENTIONS]->(k:Entity {uuid:$keep}) "
                "MATCH (ep)-[m:MENTIONS]->(o:Entity {uuid:$old}) DELETE m RETURN count(m) AS dropped",
                ep=m["episode_uuid"], keep=m["keep"], old=m["from"])
    for node in plan["nodes"]:
        if node["action"] != "update":
            continue
        props = {**node["set"],
                 "governance_ids": _ordered_union(stored[node["uuid"]].get("governance_ids"), [gid])}
        if plan["operation"] == "summary":
            rows = await _rows(driver,
                "/* gov:node-summary */ MATCH (n:Entity {uuid:$uid}) "
                "WHERE n.name=$name AND coalesce(n.group_id,$g)=$g "
                "AND coalesce(n.summary,'') IN $accepted "
                "SET n.summary=$summary, n.governance_ids=$governance_ids RETURN n.uuid AS uuid",
                uid=node["uuid"], name=node["name"], g=group_id,
                accepted=[node["previous_summary"], node["set"]["summary"]],
                summary=node["set"]["summary"], governance_ids=props["governance_ids"])
            if len(rows) != 1:
                raise GovernanceError("governance_conflict", "summary or node identity changed", 503)
            continue
        query = "/* gov:node-update */ MATCH (n:Entity {uuid:$uid}) SET n += $props"
        if node["labels_remove"]:
            query += " REMOVE n" + _labels(node["labels_remove"])
        if node["labels_add"]:
            query += " SET n" + _labels(node["labels_add"])
        params = {"uid": node["uuid"], "props": props}
        if node["embed_name"]:
            query += " SET n.name_embedding=vecf32($embedding)"
            params["embedding"] = mat["name_embeddings"][node["uuid"]]
        rows = await _rows(driver, query + " RETURN n.uuid AS uuid", **params)
        if len(rows) != 1:
            raise GovernanceError("governance_conflict", f"node {node['uuid']} disappeared", 503)
    for node in plan["nodes"]:
        if node["action"] != "tombstone":
            continue
        rows = await _rows(driver,
            "/* gov:node-tombstone */ MATCH (n:Entity {uuid:$uid}) OPTIONAL MATCH (n)-[r]-() "
            "WITH n, count(r) AS degree WHERE degree = 0 "
            "REMOVE n" + _labels(node["labels_remove"]) + " SET n:" + TOMBSTONE + " SET n += $props "
            "RETURN n.uuid AS uuid",
            uid=node["uuid"], props={**{k: v for k, v in node["set"].items() if v is not None},
                                     "merged_by": gid, "merged_at": now})
        if len(rows) != 1:
            done = await _rows(driver,
                "/* gov:node-read */ MATCH (n:" + TOMBSTONE + " {uuid:$uid}) RETURN n.merged_by AS merged_by",
                uid=node["uuid"])
            if not done or done[0].get("merged_by") != gid:
                raise GovernanceError("governance_incomplete", f"node {node['uuid']} still has relationships; "
                                      "it was not retired", 503)
    return {"status": "governed", "governance_id": gid, "operation": plan["operation"],
            "plan_sha256": plan["plan_sha256"], "summary": plan["summary"]}


async def _mention(driver, ep: str, node: str) -> bool:
    rows = await _rows(driver,
        "/* gov:mention-exists */ MATCH (ep:Episodic {uuid:$ep})-[m:MENTIONS]->(n:Entity {uuid:$node}) "
        "RETURN count(m) AS c", ep=ep, node=node)
    return bool(rows and rows[0].get("c"))


def _float32(values) -> list[float]:
    return [struct.unpack("!f", struct.pack("!f", float(v)))[0] for v in values]


async def _verify_summary_state(driver, request: dict, before: dict, plan: dict, *, require_after: bool) -> dict:
    """A summary edit preserves complete source, node, edge and mention state.

    On a pending replay, accept only the exact old state or this plan's exact
    completed update. Any other write requires a new human decision.
    """
    current = await snapshot(driver, request["scope"], before["group_id"])
    gid = request["governance_id"]
    for key in ("version", "group_id", "scope", "edges", "anchor_edges", "mentions", "episodes",
                "foreign_relationships", "missing_episodes", "name_conflicts", "tombstones"):
        if current[key] != before[key]:
            raise GovernanceError("governance_origin_changed", f"summary correction changed or lost {key}", 503)
    if [x for x in current["pending_governance"] if x != gid] != before["pending_governance"]:
        raise GovernanceError("governance_conflict", "another governance owns the summary node", 503)
    expected = {**before["nodes"][0], "props": {**before["nodes"][0]["props"],
        "summary": request["scope"]["summary_to"],
        "governance_ids": _ordered_union(before["nodes"][0]["props"].get("governance_ids"), [gid])}}
    choices = [[expected]] if require_after else [before["nodes"], [expected]]
    if current["nodes"] not in choices:
        raise GovernanceError("governance_origin_changed", "summary or other original node properties differ", 503)
    item = {"uuid": expected["uuid"], "action": "summary", "name": expected["props"]["name"],
            "old_summary_sha256": digest(before["nodes"][0]["props"]["summary"]),
            "new_summary_sha256": digest(request["scope"]["summary_to"]),
            "preserved_edges": len(before["edges"]), "preserved_mentions": len(before["mentions"]),
            "preserved_sources": len(before["episodes"])}
    return {"status": "verified", "before_snapshot_sha256": before["snapshot_sha256"], "count": 1,
            "proof_sha256": digest([item]), "items": [item], "truncated": False}


async def verify(driver, request: dict, before: dict, plan: dict, mat: dict) -> dict:
    """Read every planned effect back before issuing a success receipt."""
    if plan["operation"] == "summary":
        return await _verify_summary_state(driver, request, before, plan, require_after=True)
    original = {e["props"]["uuid"]: e for e in before["edges"] + before["anchor_edges"]}
    proof = []
    for edge in plan["edges"]:
        rows = await _locate(driver, edge["uuid"])
        if edge["action"] in ("absorb", "drop_self_loop"):
            if rows:
                raise GovernanceError("governance_not_materialized", f"edge {edge['uuid']} is still present", 503)
            proof.append({"uuid": edge["uuid"], "action": edge["action"], "present": False})
            continue
        expected_eps = edge.get("episodes_after") or edge["episodes"]
        if not _at(rows, edge["to"], expected_eps):
            raise GovernanceError("governance_not_materialized", f"edge {edge['uuid']} is not at its planned endpoints", 503)
        found = {k: v for k, v in _strip(rows[0]["props"]).items() if k != "episodes"}
        stored = {k: v for k, v in original[edge["uuid"]]["props"].items() if k != "episodes"}
        if found != stored:
            raise GovernanceError("governance_origin_changed", f"edge {edge['uuid']} lost original properties", 503)
        proof.append({"uuid": edge["uuid"], "action": edge["action"], "endpoints": edge["to"],
                      "immutable_sha256": digest(found), "episodes_count": len(rows[0]["props"].get("episodes") or [])})
    for m in plan["mentions"]:
        if m["action"] in ("move", "add") and not await _mention(driver, m["episode_uuid"], m["to"]):
            raise GovernanceError("governance_not_materialized", f"episode {m['episode_uuid']} lost its mention", 503)
        if m["action"] in ("move", "drop") and await _mention(driver, m["episode_uuid"], m["from"]):
            raise GovernanceError("governance_not_materialized", f"episode {m['episode_uuid']} still mentions the old node", 503)
    for ref in plan["episode_refs"]:
        rows = await _rows(driver, "/* gov:episodes */ MATCH (ep:Episodic) WHERE ep.uuid IN $eps "
                           "RETURN ep.uuid AS uuid, ep.entity_edges AS entity_edges", eps=[ref["episode_uuid"]])
        stored = set((rows[0].get("entity_edges") or []) if rows else [])
        if set(ref["remove"]) & stored or not set(ref["add"]) <= stored:
            raise GovernanceError("governance_not_materialized", f"episode {ref['episode_uuid']} edge refs differ", 503)
    for node in plan["nodes"]:
        label = TOMBSTONE if node["action"] == "tombstone" else "Entity"
        rows = await _rows(driver, "/* gov:node-read */ MATCH (n:" + label + " {uuid:$uid}) "
                           "RETURN properties(n) AS props, labels(n) AS labels", uid=node["uuid"])
        if len(rows) != 1:
            raise GovernanceError("governance_not_materialized", f"node {node['uuid']} is missing as {label}", 503)
        props, labels = rows[0]["props"], set(rows[0]["labels"])
        if node["action"] == "tombstone":
            if "Entity" in labels or props.get("merged_by") != request["governance_id"]:
                raise GovernanceError("governance_not_materialized", f"node {node['uuid']} was not retired", 503)
        else:
            if not set(node["labels_add"]) <= labels or set(node["labels_remove"]) & labels:
                raise GovernanceError("governance_not_materialized", f"node {node['uuid']} labels differ", 503)
            if props.get("name") != node["name"]:
                raise GovernanceError("governance_not_materialized", f"node {node['uuid']} name differs", 503)
            if node["embed_name"] and [float(x) for x in props.get("name_embedding") or []] != \
                    _float32(mat["name_embeddings"][node["uuid"]]):
                raise GovernanceError("governance_not_materialized", f"node {node['uuid']} name vector differs", 503)
        proof.append({"uuid": node["uuid"], "action": node["action"], "name": node["name"],
                      "labels": sorted(labels)})
    proof.sort(key=lambda p: (p.get("action"), p["uuid"]))
    return {"status": "verified", "before_snapshot_sha256": before["snapshot_sha256"], "count": len(proof),
            "proof_sha256": digest(proof), "items": proof[:120], "truncated": len(proof) > 120}


async def apply(g, document: dict, group_id: str) -> dict:
    """Caller MUST hold the shared cross-process writer lock for this whole call."""
    request = parse_request(document)
    gid = request["governance_id"]
    old = await audit(g.driver, gid)
    if old:
        if old.get("request_sha256") != digest(request):
            raise GovernanceError("governance_id_conflict", "governance_id is already bound to another request")
        if old.get("state") == "applied":
            return {**old["result"], "duplicate": True}
        before, plan, mat = old["before"], old["plan"], old["materialization"]
    else:
        before = await snapshot(g.driver, request["scope"], group_id)
        if before["snapshot_sha256"] != request["expected_snapshot_sha256"]:
            raise GovernanceError("stale_snapshot", "graph changed after the reviewed preview; preview again",
                                  detail={"snapshot_sha256": before["snapshot_sha256"]})
        plan = build_plan(request["scope"], before)
        if not plan["applicable"]:
            raise GovernanceError("bad_request", "plan is a listing only", 400)
        mat = await _materialize(g, plan)
        deleted = await _deleted_edges(g.driver, plan)
        prepared = await _rows(g.driver,
            "/* gov:prepare */ MERGE (r:GraphGovernance {governance_id:$gid}) "
            "ON CREATE SET r.request_sha256=$request_sha, r.request_json=$request_json, r.before_json=$before_json, "
            "r.plan_json=$plan_json, r.materialization_json=$mat_json, r.deleted_json=$deleted_json, "
            "r.plan_sha256=$plan_sha, r.node_uuids=$nodes, r.operation=$op, r.reviewer=$reviewer, "
            "r.state='pending', r.created_at=$now RETURN r.request_sha256 AS request_sha256",
            gid=gid, request_sha=digest(request), request_json=encoded(request), before_json=encoded(before),
            plan_json=encoded(plan), mat_json=encoded(mat), deleted_json=encoded(deleted),
            plan_sha=plan["plan_sha256"], nodes=sorted({n["uuid"] for n in plan["nodes"]} | set(node_ids(request["scope"]))),
            op=plan["operation"], reviewer=request["reviewer"], now=mat["prepared_at"])
        if len(prepared) != 1 or prepared[0].get("request_sha256") != digest(request):
            raise GovernanceError("governance_prepare_unconfirmed", "durable plan was not confirmed", 503)
    try:
        if plan["operation"] == "summary":
            await _verify_summary_state(g.driver, request, before, plan, require_after=False)
        result = await _apply_plan(g.driver, request, before, plan, mat, group_id)
        result["verification"] = await verify(g.driver, request, before, plan, mat)
        result["verified"] = True
    except Exception as exc:
        try:
            await _rows(g.driver,
                "/* gov:failure */ MATCH (r:GraphGovernance {governance_id:$gid}) "
                "SET r.last_error=$kind, r.last_error_at=$now RETURN r.governance_id AS gid",
                gid=gid, kind=getattr(exc, "code", type(exc).__name__), now=datetime.now(timezone.utc).isoformat())
        except Exception:
            pass  # The pending plan is already durable; never replace the cause.
        raise
    await _rows(g.driver,
        "/* gov:complete */ MATCH (r:GraphGovernance {governance_id:$gid}) "
        "SET r.state='applied', r.result_json=$result_json, r.applied_at=$now RETURN r.governance_id AS gid",
        gid=gid, result_json=encoded(result), now=datetime.now(timezone.utc).isoformat())
    return {**result, "duplicate": False}
