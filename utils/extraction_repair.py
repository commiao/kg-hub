"""Audited, episode-scoped corrections for pre-extracted graph facts.

No model is called. A durable prepared plan precedes mutations. Replays use the
same deterministic UUIDs and plan; they never re-resolve a half-applied repair.
Shared edges lose only this episode's endorsement. Orphan incorrect edges are
removed only after their complete snapshots and replacement plan are durable.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import uuid
from datetime import datetime, timezone

from schema import ENTITY_TYPES, EDGE_TYPES
from utils.extracted_ingest import (
    ExtractedEntity, ExtractedEdge, normalize_extraction, write_extraction,
    _FIND_EDGE, _norm_key,
)

VERSION = "extraction-repair-v1"
MATERIALIZATION_VERSION = "edge-origin-v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class RepairError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def source_digest(payload: dict) -> str:
    return digest({k: payload.get(k) for k in
                   ("name", "episode_body", "source_description", "source_obs_id", "reference_time")})


def selector(document: dict) -> tuple[str, str]:
    if not isinstance(document, dict):
        raise RepairError("bad_request", "expected JSON object", 400)
    sd, sid = document.get("source_description"), document.get("source_obs_id")
    if not isinstance(sd, str) or not sd.strip() or not isinstance(sid, str) or not sid.strip():
        raise RepairError("bad_request", "source_description and source_obs_id are required", 400)
    if len(sd) > 2000 or len(sid) > 200:
        raise RepairError("bad_request", "source identity too long", 400)
    return sd, sid


def parse_request(document: dict) -> dict:
    sd, sid = selector(document)
    rid = document.get("repair_id")
    expected = document.get("expected_snapshot_sha256")
    if not isinstance(rid, str) or not _ID.fullmatch(rid):
        raise RepairError("bad_request", "invalid repair_id", 400)
    if not isinstance(expected, str) or not _SHA.fullmatch(expected):
        raise RepairError("bad_request", "expected_snapshot_sha256 is required", 400)
    for field in ("reason", "reviewer"):
        if not isinstance(document.get(field), str) or not document[field].strip():
            raise RepairError("bad_request", f"{field} is required", 400)
    if len(document["reason"]) > 2000 or len(document["reviewer"]) > 200:
        raise RepairError("bad_request", "reason/reviewer exceeds limits", 400)
    remove = document.get("remove_edge_uuids", [])
    if (not isinstance(remove, list) or len(remove) > 120
            or any(not isinstance(x, str) or not x or len(x) > 120 for x in remove)
            or len(set(remove)) != len(remove)):
        raise RepairError("bad_request", "invalid remove_edge_uuids", 400)
    replacement = document.get("replacement")
    if replacement is not None and not isinstance(replacement, dict):
        raise RepairError("bad_request", "replacement must be an extracted-ingest payload", 400)
    if not remove and not replacement:
        raise RepairError("bad_request", "repair must remove or replace facts", 400)
    return {"version": VERSION, "repair_id": rid, "source_description": sd, "source_obs_id": sid,
            "expected_snapshot_sha256": expected, "reason": document["reason"].strip()[:2000],
            "reviewer": document["reviewer"].strip()[:200], "remove_edge_uuids": sorted(remove),
            "replacement": replacement}


async def _rows(driver, query, **params):
    rows, _, _ = await driver.execute_query(query, **params)
    return [dict(r) for r in rows]


async def snapshot(driver, sd: str, sid: str, group_id: str) -> dict:
    rows = await _rows(driver,
        "/* repair:snapshot-key */ MATCH (k:IngestedKey {source_description:$sd, source_obs_id:$sid}) "
        "RETURN properties(k) AS props", sd=sd, sid=sid)
    if len(rows) != 1:
        raise RepairError("source_not_found" if not rows else "ambiguous_source", "source key is not unique", 404 if not rows else 409)
    key = dict(rows[0]["props"])
    episode = None
    edges, entities = [], []
    ep = key.get("episode_uuid")
    if ep:
        rows = await _rows(driver,
            "/* repair:snapshot-episode */ MATCH (e:Episodic {uuid:$ep}) "
            "WHERE e.group_id=$group_id RETURN properties(e) AS props", ep=ep, group_id=group_id)
        if len(rows) != 1:
            raise RepairError("episode_not_found", "source key points to a missing or foreign episode")
        episode = dict(rows[0]["props"])
        rows = await _rows(driver,
            "/* repair:snapshot-edges */ MATCH (a:Entity)-[e:RELATES_TO]->(b:Entity) "
            "WHERE e.group_id=$group_id AND $ep IN coalesce(e.episodes,[]) "
            "RETURN properties(e) AS props, a.uuid AS source_uuid, a.name AS source_name, "
            "b.uuid AS target_uuid, b.name AS target_name ORDER BY e.uuid",
            ep=ep, group_id=group_id)
        edges = [dict(r) for r in rows]
        rows = await _rows(driver,
            "/* repair:snapshot-entities */ MATCH (e:Episodic {uuid:$ep})-[:MENTIONS]->(n:Entity) "
            "RETURN properties(n) AS props, labels(n) AS labels ORDER BY n.uuid", ep=ep)
        entities = [dict(r) for r in rows]
    out = {"source_description": sd, "source_obs_id": sid, "key": key,
           "episode": episode, "edges": edges, "entities": entities}
    out["snapshot_sha256"] = digest(out)
    return out


async def shared_edge_readback(driver, before: dict, group_id: str) -> list[dict]:
    """Read only original shared UUIDs, including edges detached from this source."""
    ep = (before.get("episode") or {}).get("uuid")
    ids = sorted(edge["props"]["uuid"] for edge in before["edges"]
                 if set(edge["props"].get("episodes") or []) - {ep})
    rows = []
    for start in range(0, len(ids), 120):
        rows.extend(await _rows(driver,
            "/* repair:shared-readback */ MATCH (a:Entity)-[e:RELATES_TO]->(b:Entity) "
            "WHERE e.group_id=$group_id AND e.uuid IN $ids "
            "RETURN properties(e) AS props,a.uuid AS source_uuid,b.uuid AS target_uuid ORDER BY e.uuid",
            group_id=group_id, ids=ids[start:start + 120]))
    return rows


async def edge_origin(driver, uid: str, group_id: str) -> dict:
    rows = await _rows(driver,
        "/* repair:edge-origin */ MATCH (a:Entity)-[e:RELATES_TO {uuid:$uid}]->(b:Entity) "
        "WHERE e.group_id=$group_id RETURN properties(e) AS props, "
        "a.uuid AS source_uuid,b.uuid AS target_uuid",
        uid=uid, group_id=group_id)
    if len(rows) != 1:
        raise RepairError("repair_edge_origin_changed", "reused edge is missing or its UUID is ambiguous", 503)
    return rows[0]


def _immutable_props(props: dict) -> dict:
    return {key: value for key, value in props.items() if key != "episodes"}


def _assert_origin(found: dict, original: dict) -> None:
    if (any(found.get(key) != original.get(key) for key in ("source_uuid", "target_uuid"))
            or _immutable_props(found["props"]) != _immutable_props(original["props"])
            or not set(original["props"].get("episodes") or []).issubset(set(found["props"].get("episodes") or []))):
        raise RepairError("repair_edge_origin_changed", "reused edge origin or original endorsements changed", 503)


def _write_props(edge: dict) -> dict:
    return {key: value for key, value in edge.items()
            if key not in ("fact_embedding", "write_mode", "stored_origin") and value is not None}


async def audit(driver, repair_id: str) -> dict | None:
    rows = await _rows(driver,
        "/* repair:audit-read */ MATCH (r:ExtractionRepair {repair_id:$rid}) RETURN properties(r) AS props", rid=repair_id)
    if not rows:
        return None
    if len(rows) != 1:
        raise RepairError("ambiguous_repair", "repair id is not unique")
    out = dict(rows[0]["props"])
    for name in ("request", "before", "plan", "result"):
        raw = out.pop(name + "_json", None)
        if raw is not None:
            out[name] = json.loads(raw)
    return out


def _normal(payload: dict):
    try:
        extraction = normalize_extraction([ExtractedEntity.model_validate(e) for e in payload.get("entities", [])],
            [ExtractedEdge.model_validate(e) for e in payload.get("edges", [])], set(ENTITY_TYPES), set(EDGE_TYPES))
    except (ValueError, TypeError) as exc:
        raise RepairError("bad_replacement", str(exc), 400) from exc
    if not extraction.entities or not extraction.edges:
        raise RepairError("bad_replacement", "replacement requires nonempty entities and edges", 400)
    for key in ("unknown_type", "offschema_edge", "dangling_edge", "self_edge", "dup_entity", "dup_edge"):
        if extraction.stats.get(key):
            raise RepairError("bad_replacement", f"replacement has {key}", 400)
    return extraction


def _source_time(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed
    except (AttributeError, TypeError, ValueError) as exc:
        raise RepairError("bad_replacement", "source reference time must be an aware ISO 8601 timestamp", 400) from exc


def validate_against_snapshot(request: dict, before: dict, *, quality_validator) -> None:
    key, episode = before["key"], before["episode"]
    if key.get("status") not in ("ok", "empty"):
        raise RepairError("source_not_settled", "only settled ok/empty sources can be corrected")
    if key.get("repair_pending_id"):
        raise RepairError("repair_pending", "source already has a pending repair")
    if request["expected_snapshot_sha256"] != before["snapshot_sha256"]:
        raise RepairError("stale_snapshot", "source changed after preview")
    if episode and episode.get("archived"):
        raise RepairError("archived_source", "repair does not silently restore archived sources")
    known = {e["props"]["uuid"] for e in before["edges"]}
    if not set(request["remove_edge_uuids"]).issubset(known):
        raise RepairError("foreign_edge", "removed edges must be endorsed by this episode")
    payload = request["replacement"]
    if key.get("status") == "empty" and not payload:
        raise RepairError("bad_replacement", "empty upgrade requires replacement facts", 400)
    if payload:
        if (payload.get("source_description"), payload.get("source_obs_id")) != (before["source_description"], before["source_obs_id"]):
            raise RepairError("identity_changed", "replacement must use the original source identity", 400)
        required = ("name", "episode_body", "reference_time", "extractor_model", "prompt_version")
        if any(not isinstance(payload.get(k), str) or not payload[k] for k in required):
            raise RepairError("bad_replacement", "replacement is missing source/provenance fields", 400)
        reference_time = _source_time(payload["reference_time"])
        if episode:
            if reference_time != _source_time(episode.get("valid_at")):
                raise RepairError("source_changed", "source reference time must preserve original valid_at", 400)
            if payload["name"] != episode.get("name") or payload["episode_body"] != episode.get("content"):
                raise RepairError("source_changed", "source name/content must remain byte-identical", 400)
        elif key.get("name") and payload["name"] != key["name"]:
            raise RepairError("source_changed", "empty source name must be preserved", 400)
        if key.get("status") == "empty" and key.get("empty_source_sha256") and key["empty_source_sha256"] != source_digest(payload):
            raise RepairError("source_changed", "empty source bytes differ from the original receipt", 400)
        if not payload["prompt_version"].startswith("agent-v5"):
            raise RepairError("quality_required", "repairs require the current reviewed protocol", 400)
        errors = quality_validator(payload)
        if errors:
            raise RepairError("quality_review_failed", "; ".join(errors[:20]), 400)
        _normal(payload)


async def prepare_plan(g, request: dict, before: dict, group_id: str) -> dict:
    rid, payload = request["repair_id"], request["replacement"]
    now = datetime.now(timezone.utc).isoformat()
    ep_uuid = before["key"].get("episode_uuid") or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{VERSION}:{rid}:episode"))
    plan = {"episode_uuid": ep_uuid, "created_at": now, "nodes": [], "edges": [],
            "materialization_version": MATERIALIZATION_VERSION,
            "source_reference_time": payload["reference_time"] if payload else (before["episode"] or {}).get("valid_at"),
            "remove": [e for e in before["edges"] if e["props"]["uuid"] in request["remove_edge_uuids"]]}
    if not payload:
        return plan
    normal = _normal(payload)
    preview = await write_extraction(g.driver, g.embedder, normal, name=payload["name"],
        episode_body=payload["episode_body"], source_description=payload["source_description"],
        reference_time=datetime.fromisoformat(payload["reference_time"].replace("Z", "+00:00")),
        extractor_model=payload["extractor_model"], group_id=group_id, dry_run=True)
    vectors = await g.embedder.create_batch([n.name for n in normal.entities] + [e.fact for e in normal.edges])
    resolved = {}
    for index, ent in enumerate(normal.entities):
        match = preview.resolved[ent.key]
        uid = match.get("uuid") or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{VERSION}:{rid}:node:{ent.key}"))
        resolved[ent.key] = uid
        plan["nodes"].append({"uuid": uid, "type": ent.type, "new": not bool(match.get("uuid")),
            "name": ent.name, "matched_name": match.get("matched_name"), "summary": ent.summary,
            "name_embedding": [float(x) for x in vectors[index]], "group_id": group_id,
            "extractor_model": payload["extractor_model"], "extractor_tool": payload.get("extractor_tool"),
            "run_id": payload.get("run_id"), "extraction_mode": "agent_repair", "created_at": now})
    for index, ed in enumerate(normal.edges):
        src, dst = resolved[ed.source_key], resolved[ed.target_key]
        rows = await _rows(g.driver, _FIND_EDGE, src=src, dst=dst, name=ed.name, g=group_id, fact=ed.fact)
        # Never bind the replacement to a selected incorrect edge, even if a caller
        # accidentally sends the exact old triple back as its purported correction.
        rows = [r for r in rows if r["uuid"] not in request["remove_edge_uuids"]]
        if rows:
            stored = await edge_origin(g.driver, rows[0]["uuid"], group_id)
            if (stored["source_uuid"] != src or stored["target_uuid"] != dst
                    or stored["props"].get("name") != ed.name or stored["props"].get("fact") != ed.fact):
                raise RepairError("repair_edge_origin_changed", "deduplicated edge no longer matches the reviewed fact", 503)
            properties = dict(stored["props"])
            properties["episodes"] = list(properties.get("episodes") or [])
            if ep_uuid not in properties["episodes"]:
                properties["episodes"].append(ep_uuid)
            plan["edges"].append({**properties, "source_uuid": src, "target_uuid": dst,
                                  "write_mode": "reused", "stored_origin": stored})
            continue
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{VERSION}:{rid}:edge:{index}"))
        plan["edges"].append({"write_mode": "new", "stored_origin": None, "uuid": uid, "source_uuid": src, "target_uuid": dst,
            "name": ed.name, "fact": ed.fact, "raw_name": ed.raw_name, "group_id": group_id,
            "fact_embedding": [float(x) for x in vectors[len(normal.entities) + index]],
            "episodes": [ep_uuid], "created_at": now, "valid_at": payload["reference_time"],
            "reference_time": payload["reference_time"], "extractor_model": payload["extractor_model"],
            "extractor_tool": payload.get("extractor_tool"), "run_id": payload.get("run_id"),
            "extraction_mode": "agent_repair", "repair_id": rid})
    return plan


async def preview(g, document: dict, group_id: str, *, quality_validator) -> dict:
    sd, sid = selector(document)
    before = await snapshot(g.driver, sd, sid, group_id)
    out = {"status": "repair_preview", "version": VERSION, "snapshot": before}
    if document.get("repair_id"):
        request = parse_request(document)
        validate_against_snapshot(request, before, quality_validator=quality_validator)
        plan = await prepare_plan(g, request, before, group_id)
        out["plan"] = plan
        out["request_sha256"] = digest(request)
    return out


async def _persist_prepared(driver, request, before, plan):
    # The immutable before/plan are durable before marking the source or changing
    # a single graph fact. A crash here is safely retried with the original ID.
    prepared = await _rows(driver,
        "/* repair:prepare */ MERGE (r:ExtractionRepair {repair_id:$rid}) "
        "ON CREATE SET r.request_sha256=$request_sha, r.request_json=$request_json, "
        "r.before_json=$before_json, r.plan_json=$plan_json, r.state='pending', r.created_at=$now "
        "RETURN r.request_sha256 AS request_sha256",
        rid=request["repair_id"], request_sha=digest(request), request_json=encoded(request),
        before_json=encoded(before), plan_json=encoded(plan), now=plan["created_at"])
    if len(prepared) != 1 or prepared[0].get("request_sha256") != digest(request):
        raise RepairError("repair_prepare_unconfirmed", "durable repair plan was not confirmed", 503)


async def _apply_plan(driver, request, before, plan):
    sd, sid, rid, ep = request["source_description"], request["source_obs_id"], request["repair_id"], plan["episode_uuid"]
    claimed = await _rows(driver,
        "/* repair:claim */ MATCH (k:IngestedKey {source_description:$sd, source_obs_id:$sid}) "
        "WHERE k.repair_pending_id IS NULL OR k.repair_pending_id=$rid "
        "SET k.repair_pending_id=$rid RETURN k.source_obs_id AS sid", sd=sd, sid=sid, rid=rid)
    if len(claimed) != 1:
        raise RepairError("repair_pending", "source repair ownership changed")
    for edge in plan["edges"]:
        if edge.get("write_mode") == "reused":
            original = edge["stored_origin"]
            _assert_origin(await edge_origin(driver, edge["uuid"], original["props"]["group_id"]), original)
    for node in plan["nodes"]:
        if not node["new"]:
            continue
        data = {k: v for k, v in node.items() if k not in ("new", "type", "matched_name", "name_embedding") and v is not None}
        # Labels come only from normalize_extraction's allowlisted entity types.
        await _rows(driver,
            "/* repair:node */ MERGE (n:Entity {uuid:$uid}) ON CREATE SET n:" + node["type"] +
            ", n += $props, n.name_embedding=vecf32($embedding) RETURN n.uuid AS uuid",
            uid=node["uuid"], props=data, embedding=node["name_embedding"])
    payload = request["replacement"]
    if before["episode"] is None:
        # Empty keys historically stored no body. The reviewed replacement carries
        # the original source; its exact submitted bytes are retained in the audit.
        original = {"uuid": ep, "name": payload["name"], "content": payload["episode_body"],
            "source": "text", "source_description": sd, "group_id": plan["nodes"][0]["group_id"],
            "entity_edges": [], "created_at": plan["created_at"], "valid_at": payload["reference_time"],
            "extractor_model": before["key"].get("empty_by_model") or payload["extractor_model"],
            "extractor_tool": before["key"].get("empty_by_tool") or payload.get("extractor_tool"),
            "run_id": before["key"].get("empty_run_id") or payload.get("run_id"),
            "extraction_mode": "agent", "original_empty": True}
        await _rows(driver,
            "/* repair:episode */ MERGE (e:Episodic {uuid:$ep}) ON CREATE SET e += $props RETURN e.uuid AS uuid",
            ep=ep, props={k: v for k, v in original.items() if v is not None})
    for node in plan["nodes"]:
        mid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{VERSION}:{ep}:mentions:{node['uuid']}"))
        await _rows(driver,
            "/* repair:mention */ MATCH (e:Episodic {uuid:$ep}), (n:Entity {uuid:$node}) "
            "MERGE (e)-[m:MENTIONS]->(n) ON CREATE SET m.uuid=$mid, m.group_id=$g, m.created_at=$now "
            "RETURN m.uuid AS uuid", ep=ep, node=node["uuid"], mid=mid,
            g=node["group_id"], now=plan["created_at"])
    new_ids = []
    for edge in plan["edges"]:
        if edge.get("write_mode") == "reused":
            rows = await _rows(driver,
                "/* repair:edge-reuse */ MATCH (a:Entity {uuid:$src})-[e:RELATES_TO {uuid:$uid}]->(b:Entity {uuid:$dst}) "
                "WHERE e.group_id=$group_id AND e.name=$name AND e.fact=$fact "
                "SET e.episodes=CASE WHEN $ep IN coalesce(e.episodes,[]) THEN e.episodes "
                "ELSE coalesce(e.episodes,[])+[$ep] END RETURN e.uuid AS uuid",
                src=edge["source_uuid"], dst=edge["target_uuid"], uid=edge["uuid"], group_id=edge["group_id"],
                name=edge["name"], fact=edge["fact"], ep=ep)
            if len(rows) != 1:
                raise RepairError("repair_edge_origin_changed", "reused edge disappeared before endorsement", 503)
            new_ids.append(edge["uuid"])
            continue
        data = _write_props(edge)
        rows = await _rows(driver,
            "/* repair:edge */ MATCH (a:Entity {uuid:$src}), (b:Entity {uuid:$dst}) "
            "MERGE (a)-[e:RELATES_TO {uuid:$uid}]->(b) "
            "ON CREATE SET e += $props, e.fact_embedding=vecf32($embedding) "
            "SET e.episodes=CASE WHEN $ep IN coalesce(e.episodes,[]) THEN e.episodes "
            "ELSE coalesce(e.episodes,[])+[$ep] END RETURN e.uuid AS uuid",
            src=edge["source_uuid"], dst=edge["target_uuid"], uid=edge["uuid"], props=data,
            embedding=edge["fact_embedding"], ep=ep)
        if not rows:
            raise RepairError("repair_write_incomplete", "replacement endpoints are not materialized", 503)
        new_ids.append(edge["uuid"])
    # Only after all replacement facts exist do we remove incorrect endorsements.
    for old in plan["remove"]:
        props = old["props"]
        await _rows(driver,
            "/* repair:detach */ MATCH ()-[e:RELATES_TO {uuid:$uid}]->() "
            "WHERE e.fact=$fact AND e.name=$name "
            "SET e.episodes=[x IN coalesce(e.episodes,[]) WHERE x<>$ep] "
            "RETURN e.uuid AS uuid", uid=props["uuid"], fact=props["fact"], name=props["name"], ep=ep)
        await _rows(driver,
            "/* repair:delete-orphan */ MATCH ()-[e:RELATES_TO {uuid:$uid}]->() "
            "WHERE size(coalesce(e.episodes,[]))=0 DELETE e", uid=props["uuid"])
    old_nodes = sorted({old[k] for old in plan["remove"] for k in ("source_uuid", "target_uuid")})
    if old_nodes:
        await _rows(driver,
            "/* repair:mentions-clean */ MATCH (episode:Episodic {uuid:$ep})-[m:MENTIONS]->(n:Entity) "
            "WHERE n.uuid IN $old_nodes OPTIONAL MATCH (n)-[fact:RELATES_TO]-() "
            "WHERE $ep IN coalesce(fact.episodes,[]) WITH m,count(fact) AS c WHERE c=0 DELETE m",
            ep=ep, old_nodes=old_nodes)
    await _rows(driver,
        "/* repair:episode-finish */ MATCH (e:Episodic {uuid:$ep}) "
        "WITH e,[x IN coalesce(e.entity_edges,[]) WHERE NOT x IN $removed] AS kept "
        "SET e.entity_edges=kept+[x IN $added WHERE NOT x IN kept] "
        "SET e.repair_ids=CASE WHEN $rid IN coalesce(e.repair_ids,[]) THEN e.repair_ids "
        "ELSE coalesce(e.repair_ids,[])+[$rid] END RETURN e.uuid AS uuid",
        ep=ep, removed=request["remove_edge_uuids"], added=new_ids, rid=rid)
    counts = await _rows(driver,
        "/* repair:source-counts */ MATCH (e:Episodic {uuid:$ep}) "
        "OPTIONAL MATCH (e)-[:MENTIONS]->(n:Entity) WITH e,count(DISTINCT n) AS nodes "
        "OPTIONAL MATCH ()-[r:RELATES_TO]->() WHERE $ep IN coalesce(r.episodes,[]) "
        "RETURN nodes,count(DISTINCT r) AS edges", ep=ep)
    if len(counts) != 1:
        raise RepairError("repair_not_materialized", "source counts could not be confirmed", 503)
    await _rows(driver,
        "/* repair:key-finish */ MATCH (k:IngestedKey {source_description:$sd, source_obs_id:$sid}) "
        "WHERE k.repair_pending_id=$rid SET k.status='ok', k.episode_uuid=$ep, "
        "k.nodes=$nodes, k.edges=$edges, k.updated_at=$now, k.last_repair_id=$rid RETURN k.episode_uuid AS uuid",
        sd=sd, sid=sid, rid=rid, ep=ep, nodes=counts[0]["nodes"], edges=counts[0]["edges"],
        now=datetime.now(timezone.utc).isoformat())
    return {"status": "repaired", "repair_id": rid, "episode_uuid": ep,
            "removed_endorsements": len(plan["remove"]), "replacement_edges": len(new_ids),
            "upgraded_empty": before["key"].get("status") == "empty",
            "historical_source_attested": before["key"].get("status") == "empty" and not before["key"].get("empty_source_sha256")}


def verify_materialization(current: dict, before: dict, request: dict, plan: dict, shared_edges: list[dict]) -> dict:
    """Read back actual facts before issuing a success receipt."""
    if current["key"].get("status") != "ok" or current["key"].get("episode_uuid") != plan["episode_uuid"]:
        raise RepairError("repair_not_materialized", "source key does not confirm corrected episode", 503)
    episode = current.get("episode")
    if not episode:
        raise RepairError("repair_not_materialized", "corrected episode is missing", 503)
    original = before.get("episode")
    if original and any(episode.get(k) != original.get(k) for k in
                        ("name", "content", "valid_at", "source_description", "extractor_model", "extractor_tool", "run_id", "prompt_version")):
        raise RepairError("repair_source_changed", "original source/provenance changed", 503)
    actual = {edge["props"]["uuid"]: edge for edge in current["edges"]}
    node_count = len({node["props"]["uuid"] for node in current["entities"]})
    if current["key"].get("nodes") != node_count or current["key"].get("edges") != len(actual):
        raise RepairError("repair_not_materialized", "source key counts differ from actual source facts", 503)
    if not original and _source_time(episode.get("valid_at")) != _source_time(request["replacement"]["reference_time"]):
        raise RepairError("repair_source_changed", "upgraded source reference time changed", 503)
    if set(request["remove_edge_uuids"]) & set(actual):
        raise RepairError("repair_not_materialized", "incorrect endorsement remains", 503)
    for edge in plan["edges"]:
        found = actual.get(edge["uuid"])
        if not found or any(found["props"].get(k) != edge[k] for k in ("name", "fact")) or any(
                found.get(k) != edge[k] for k in ("source_uuid", "target_uuid")):
            raise RepairError("repair_not_materialized", "corrected fact is missing or differs", 503)
        if edge.get("write_mode") == "reused":
            _assert_origin(found, edge["stored_origin"])
        elif edge.get("write_mode") == "new":
            expected_props = _immutable_props(_write_props(edge))
            # Falkor vecf32 materializes IEEE float32, while the embedder may
            # return wider Python floats. Compare the exact stored precision.
            expected_props["fact_embedding"] = [struct.unpack("!f", struct.pack("!f", value))[0]
                                                for value in edge["fact_embedding"]]
            actual_props = _immutable_props(found["props"])
            if actual_props != expected_props:
                raise RepairError("repair_edge_origin_changed", "new fact origin metadata differs from its prepared plan", 503)
    expected = ({edge["props"]["uuid"] for edge in before["edges"]}
                - set(request["remove_edge_uuids"])) | {edge["uuid"] for edge in plan["edges"]}
    if not expected.issubset(set(episode.get("entity_edges") or [])):
        raise RepairError("repair_not_materialized", "episode edge references are incomplete", 503)
    if set(request["remove_edge_uuids"]) & set(episode.get("entity_edges") or []):
        raise RepairError("repair_not_materialized", "episode retains removed edge references", 503)
    ep = plan["episode_uuid"]
    by_uuid = {}
    for edge in shared_edges:
        uid = edge["props"]["uuid"]
        if uid in by_uuid:
            raise RepairError("repair_shared_edge_changed", "shared edge UUID is no longer unique", 503)
        by_uuid[uid] = edge
    proof = []
    for old in before["edges"]:
        other = sorted(set(old["props"].get("episodes") or []) - {ep})
        if not other:
            continue
        uid = old["props"]["uuid"]
        found = by_uuid.get(uid)
        if (not found or any(found["props"].get(k) != old["props"].get(k) for k in ("name", "fact"))
                or any(found.get(k) != old.get(k) for k in ("source_uuid", "target_uuid"))
                or not set(other).issubset(set(found["props"].get("episodes") or []))):
            raise RepairError("repair_shared_edge_changed", "original shared fact or other source endorsement changed", 503)
        proof.append({"uuid": uid, "name": old["props"]["name"],
                      "fact_sha256": digest(old["props"]["fact"]),
                      "source_uuid": old["source_uuid"], "target_uuid": old["target_uuid"],
                      "other_episodes_count": len(other), "other_episodes_sha256": digest(other)})
    proof.sort(key=lambda edge: edge["uuid"])
    return {"status": "verified", "before_snapshot_sha256": before["snapshot_sha256"],
            "count": len(proof), "proof_sha256": digest(proof), "edges": proof[:120],
            "truncated": len(proof) > 120}


def replacement_edge_proof(current: dict, plan: dict) -> dict:
    if plan.get("materialization_version") != MATERIALIZATION_VERSION:
        # Old prepared plans remain replayable without pretending they carried
        # the stronger origin contract. Applied historical audits are untouched.
        return {"status": "legacy_plan", "version": "legacy"}
    actual = {row["props"]["uuid"]: row for row in current["edges"]}
    proof = []
    for edge in plan["edges"]:
        found = actual[edge["uuid"]]
        props = found["props"]
        original = edge.get("stored_origin")
        other = sorted(set(original["props"].get("episodes") or []) - {plan["episode_uuid"]}) if original else []
        proof.append({"uuid": edge["uuid"], "write_mode": edge["write_mode"],
                      "source_uuid": found["source_uuid"], "target_uuid": found["target_uuid"],
                      "fact_sha256": digest(props["fact"]), "immutable_origin_sha256": digest(_immutable_props(props)),
                      "original_episodes_count": len(other), "original_episodes_sha256": digest(other),
                      "origin": {key: props[key] for key in ("name", "group_id", "created_at", "valid_at", "reference_time",
                                 "extractor_model", "extractor_tool", "run_id", "extraction_mode", "repair_id") if key in props}})
    proof.sort(key=lambda edge: edge["uuid"])
    return {"status": "verified", "version": MATERIALIZATION_VERSION,
            "source_reference_time": plan["source_reference_time"], "count": len(proof),
            "proof_sha256": digest(proof), "edges": proof[:120], "truncated": len(proof) > 120}


async def apply(g, document: dict, group_id: str, *, quality_validator) -> dict:
    """Caller MUST hold the shared cross-process writer lock for this whole call."""
    request = parse_request(document)
    old = await audit(g.driver, request["repair_id"])
    if old:
        if old.get("request_sha256") != digest(request):
            raise RepairError("repair_id_conflict", "repair_id is already bound to another request")
        if old.get("state") == "applied":
            await _rows(g.driver,
                "/* repair:release */ MATCH (k:IngestedKey {source_description:$sd, source_obs_id:$sid}) "
                "WHERE k.repair_pending_id=$rid REMOVE k.repair_pending_id RETURN k.source_obs_id AS sid",
                sd=request["source_description"], sid=request["source_obs_id"], rid=request["repair_id"])
            return {**old["result"], "duplicate": True}
        before, plan = old["before"], old["plan"]
        current = await snapshot(g.driver, request["source_description"], request["source_obs_id"], group_id)
        pending = current["key"].get("repair_pending_id")
        if pending not in (None, request["repair_id"]):
            raise RepairError("repair_pending", "another repair owns this source")
        if pending is None and current["snapshot_sha256"] != before["snapshot_sha256"]:
            raise RepairError("stale_snapshot", "source changed before pending repair was claimed")
    else:
        before = await snapshot(g.driver, request["source_description"], request["source_obs_id"], group_id)
        validate_against_snapshot(request, before, quality_validator=quality_validator)
        plan = await prepare_plan(g, request, before, group_id)
        await _persist_prepared(g.driver, request, before, plan)
    try:
        result = await _apply_plan(g.driver, request, before, plan)
        current = await snapshot(g.driver, request["source_description"], request["source_obs_id"], group_id)
        shared_edges = await shared_edge_readback(g.driver, before, group_id)
        result["shared_edge_verification"] = verify_materialization(current, before, request, plan, shared_edges)
        result["replacement_edge_verification"] = replacement_edge_proof(current, plan)
        result["verified"] = True
    except Exception as exc:
        try:
            await _rows(g.driver,
                "/* repair:failure */ MATCH (r:ExtractionRepair {repair_id:$rid}) "
                "SET r.last_error=$kind, r.last_error_at=$now RETURN r.repair_id AS repair_id",
                rid=request["repair_id"], kind=type(exc).__name__, now=datetime.now(timezone.utc).isoformat())
        except Exception:
            pass  # Original pending plan is already durable; never replace the cause.
        raise
    await _rows(g.driver,
        "/* repair:complete */ MATCH (r:ExtractionRepair {repair_id:$rid}) "
        "SET r.state='applied', r.result_json=$result_json, r.applied_at=$now RETURN r.repair_id AS repair_id",
        rid=request["repair_id"], result_json=encoded(result), now=datetime.now(timezone.utc).isoformat())
    # Clearing after the durable completion means a crash cannot make a completed
    # correction appear eligible to a different repair before its receipt exists.
    await _rows(g.driver,
        "/* repair:release */ MATCH (k:IngestedKey {source_description:$sd, source_obs_id:$sid}) "
        "WHERE k.repair_pending_id=$rid REMOVE k.repair_pending_id RETURN k.source_obs_id AS sid",
        sd=request["source_description"], sid=request["source_obs_id"], rid=request["repair_id"])
    return {**result, "duplicate": False}
