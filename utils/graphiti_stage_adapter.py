"""Isolated Graphiti 0.29 node-stage snapshot adapter; not wired to production.

The pinned upstream resolver re-queries live semantic candidates on every run.
This module persists their exact order before the dedupe model can be called.
A caller must also persist and restore the extracted EntityNode objects, including
UUIDs, before using this resolver. The later edge and graph-write stages still
need checkpoints before any manual retry can be exposed.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager


def _encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class StageArtifactStore:
    """Immutable, fsynced stage records keyed by original business operation."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS graphiti_stage_artifacts (
                task_sd TEXT NOT NULL, task_sid TEXT NOT NULL,
                operation_id TEXT NOT NULL, input_digest TEXT NOT NULL,
                stage TEXT NOT NULL, artifact_json TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,
                PRIMARY KEY (task_sd, task_sid, operation_id, stage))""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def save_or_load(self, task_sd: str, task_sid: str, operation_id: str,
                     input_digest: str, stage: str, new_value: object | None = None):
        if not all((task_sd, task_sid, operation_id, input_digest, stage)):
            raise RuntimeError("graphiti stage identity is incomplete")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT input_digest, artifact_json, artifact_digest
                FROM graphiti_stage_artifacts WHERE task_sd=? AND task_sid=?
                AND operation_id=? AND stage=?""",
                (task_sd, task_sid, operation_id, stage)).fetchone()
            if row is not None:
                if row[0] != input_digest:
                    raise RuntimeError("graphiti stage input drift")
                if hashlib.sha256(row[1].encode()).hexdigest() != row[2]:
                    raise RuntimeError("graphiti stage artifact corrupted")
                return json.loads(row[1])
            if new_value is None:
                return None
            payload = _encoded(new_value)
            db.execute("""INSERT INTO graphiti_stage_artifacts VALUES (?,?,?,?,?,?,?)""",
                (task_sd, task_sid, operation_id, input_digest, stage, payload,
                 hashlib.sha256(payload.encode()).hexdigest()))
            return json.loads(payload)

    def begin_graph_commit(self, task_sd: str, task_sid: str,
                           operation_id: str, input_digest: str) -> None:
        """One-way fence: an uncertain graph write must never run twice."""
        marker = _encoded({"state": "graph_commit_started"})
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""INSERT INTO graphiti_stage_artifacts VALUES (?,?,?,?,?,?,?)""",
                    (task_sd, task_sid, operation_id, input_digest,
                     "graph_commit_started", marker,
                     hashlib.sha256(marker.encode()).hexdigest()))
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("graph commit may already have started; freeze") from exc


def load_extracted_nodes(store: StageArtifactStore, *, task_sd: str,
                         task_sid: str, operation_id: str, input_digest: str):
    """Restore exact node UUIDs from a completed extraction stage."""
    from graphiti_core.nodes import EntityNode

    saved = store.save_or_load(task_sd, task_sid, operation_id,
                               input_digest, "extracted_nodes")
    if saved is None:
        return None
    return [EntityNode.model_validate(node) for node in saved]


async def extract_nodes_with_snapshot(
    clients, episode, previous_episodes, entity_types,
    excluded_entity_types, custom_extraction_instructions,
    *, store: StageArtifactStore, task_sd: str, task_sid: str,
    operation_id: str, input_digest: str,
):
    """Run pinned extraction once and restore its random UUIDs on continuation."""
    from graphiti_core.nodes import EntityNode
    from graphiti_core.utils.maintenance import node_operations as ops

    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for node-stage adapter")
    identity = (task_sd, task_sid, operation_id, input_digest)
    saved = store.save_or_load(*identity, "extraction")
    if saved is None:
        nodes, attribution = await ops.extract_nodes(
            clients, episode, previous_episodes, entity_types,
            excluded_entity_types, custom_extraction_instructions)
        saved = store.save_or_load(*identity, "extraction", {
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "attribution": attribution,
        })
    return ([EntityNode.model_validate(node) for node in saved["nodes"]],
            saved["attribution"])


async def resolve_nodes_with_candidate_snapshot(
    clients, extracted_nodes, episode, previous_episodes, entity_types,
    *, store: StageArtifactStore, task_sd: str, task_sid: str,
    operation_id: str, input_digest: str,
):
    """Pinned 0.29 resolver with immutable ordered candidate input.

    This deliberately imports upstream private helpers and must be validated
    against the pinned wheel before any release. The caller supplies restored
    extracted nodes, not freshly regenerated UUIDs.
    """
    from graphiti_core.nodes import EntityNode
    from graphiti_core.utils.maintenance import node_operations as ops

    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for node-stage adapter")

    identity = (task_sd, task_sid, operation_id, input_digest)
    extracted = [node.model_dump(mode="json") for node in extracted_nodes]
    saved = store.save_or_load(*identity, "extracted_nodes", extracted)
    if saved != extracted:
        raise RuntimeError("extracted node UUID/input drift")
    resolved = store.save_or_load(*identity, "resolved_nodes")
    if resolved is not None:
        return (
            [EntityNode.model_validate(node) for node in resolved["nodes"]],
            resolved["uuid_map"],
            [(EntityNode.model_validate(left), EntityNode.model_validate(right))
             for left, right in resolved["duplicates"]],
        )
    candidate_json = store.save_or_load(*identity, "node_candidates")
    if candidate_json is None:
        candidates = await ops._collect_candidate_nodes(clients, extracted_nodes, None)
        candidate_json = store.save_or_load(
            *identity, "node_candidates",
            [[node.model_dump(mode="json") for node in group] for group in candidates])
    candidate_nodes_by_extracted = [
        [EntityNode.model_validate(node) for node in group] for group in candidate_json]
    if len(candidate_nodes_by_extracted) != len(extracted_nodes):
        raise RuntimeError("candidate snapshot length drift")

    state = ops.DedupResolutionState(
        resolved_nodes=[None] * len(extracted_nodes), uuid_map={}, unresolved_indices=[])
    for idx, (node, candidates) in enumerate(
            zip(extracted_nodes, candidate_nodes_by_extracted, strict=True)):
        if not candidates:
            continue
        indexes = ops._build_candidate_indexes(candidates)
        local_state = ops.DedupResolutionState(
            resolved_nodes=[None], uuid_map={}, unresolved_indices=[])
        ops._resolve_with_similarity([node], indexes, local_state)
        if local_state.resolved_nodes[0] is not None:
            ops._commit_resolution(state, local_state.resolved_nodes[0],
                                   local_state.uuid_map, local_state.duplicate_pairs, idx)
            continue
        state.unresolved_indices.append(idx)
    if state.unresolved_indices:
        llm_candidates = ops._merge_candidate_nodes(
            [candidate for idx in state.unresolved_indices
             for candidate in candidate_nodes_by_extracted[idx]], None)
        await ops._resolve_with_llm(
            clients.llm_client, extracted_nodes,
            ops._build_candidate_indexes(llm_candidates), state,
            episode, previous_episodes, entity_types)
    for idx, node in enumerate(extracted_nodes):
        if state.resolved_nodes[idx] is None:
            state.resolved_nodes[idx] = node
            state.uuid_map[node.uuid] = node.uuid
    result = ([node for node in state.resolved_nodes if node is not None],
              state.uuid_map, state.duplicate_pairs)
    resolved_json = {
        "nodes": [node.model_dump(mode="json") for node in result[0]],
        "uuid_map": result[1],
        "duplicates": [[left.model_dump(mode="json"), right.model_dump(mode="json")]
                       for left, right in result[2]],
    }
    saved_resolved = store.save_or_load(*identity, "resolved_nodes", resolved_json)
    if saved_resolved != resolved_json:
        raise RuntimeError("concurrent resolved node artifact drift")
    return result
