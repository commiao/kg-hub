"""Pinned Graphiti 0.29 stage snapshot adapter for the original ingest worker.

The pinned upstream resolver re-queries live semantic candidates on every run.
This module persists their exact order before the dedupe model can be called.
Completed edge and attribute stages are replayed from immutable artifacts.
An interrupted model stage requires an exact human grant. Request drift stops
before HTTP; an uncertain graph commit requires readback before success.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from enum import Enum


def _encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _stage_value(value):
    if isinstance(value, type):
        name = f"{value.__module__}.{value.__qualname__}"
        if hasattr(value, "model_json_schema"):
            return {"type": name, "schema": value.model_json_schema()}
        return name
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _stage_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stage_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise RuntimeError(f"unsupported Graphiti stage input type: {type(value)!r}")


def _stage_digest(value) -> str:
    return hashlib.sha256(_encoded(_stage_value(value)).encode()).hexdigest()


def _active_manual_stage() -> str | None:
    from model_gateway_client import manual_resume_stage
    return manual_resume_stage()


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

    def locate(self, task_sd: str, task_sid: str, operation_id: str, stage: str):
        """Resolve a saved input digest without guessing a historical operation."""
        with self._connect() as db:
            row = db.execute("""SELECT input_digest FROM graphiti_stage_artifacts
                WHERE task_sd=? AND task_sid=? AND operation_id=? AND stage=?""",
                (task_sd, task_sid, operation_id, stage)).fetchone()
        if row is None:
            return None
        return row[0], self.save_or_load(task_sd, task_sid, operation_id, row[0], stage)

    def begin_graph_commit(self, task_sd: str, task_sid: str,
                           operation_id: str, input_digest: str,
                           expected: dict | None = None) -> None:
        """One-way fence: an uncertain graph write must never run twice."""
        marker = _encoded({"state": "graph_commit_started",
                           "expected": expected})
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("""INSERT INTO graphiti_stage_artifacts VALUES (?,?,?,?,?,?,?)""",
                    (task_sd, task_sid, operation_id, input_digest,
                     "graph_commit_started", marker,
                     hashlib.sha256(marker.encode()).hexdigest()))
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("graph commit may already have started; freeze") from exc

    def begin_stage(self, task_sd: str, task_sid: str, operation_id: str,
                    input_digest: str, stage: str, stage_input_digest: str) -> None:
        marker = _encoded({"stage_input_digest": stage_input_digest})
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("""SELECT input_digest, artifact_json, artifact_digest
                FROM graphiti_stage_artifacts WHERE task_sd=? AND task_sid=?
                AND operation_id=? AND stage=?""",
                (task_sd, task_sid, operation_id, stage + "_started")).fetchone()
            if prior is not None:
                if (prior[0] != input_digest or prior[1] != marker
                        or hashlib.sha256(prior[1].encode()).hexdigest() != prior[2]):
                    raise RuntimeError(f"{stage} start identity drift; freeze")
                # `_stage_record` has already required a matching manual grant
                # before an interrupted stage can reach this point. Treat the
                # exact marker as the original stage boundary, not a second one.
                return
            try:
                db.execute("""INSERT INTO graphiti_stage_artifacts VALUES (?,?,?,?,?,?,?)""",
                    (task_sd, task_sid, operation_id, input_digest,
                     stage + "_started", marker,
                     hashlib.sha256(marker.encode()).hexdigest()))
            except sqlite3.IntegrityError as exc:
                raise RuntimeError(f"{stage} may already be in flight; freeze") from exc


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
    from model_gateway_client import collect_model_steps, acknowledge_restored_steps
    identity = (task_sd, task_sid, operation_id, input_digest)
    inputs = (episode, previous_episodes, entity_types,
              excluded_entity_types, custom_extraction_instructions)
    saved, digest = _stage_record(
        store, identity, "extraction", inputs,
        allow_retry=_active_manual_stage() == "node_extraction")
    if saved is None:
        store.begin_stage(*identity, "extraction", digest)
        from model_gateway_client import model_stage
        with model_stage("node_extraction"), collect_model_steps() as used_steps:
            nodes, attribution = await ops.extract_nodes(
                clients, episode, previous_episodes, entity_types,
                excluded_entity_types, custom_extraction_instructions)
        saved = store.save_or_load(*identity, "extraction", {
            "stage_input_digest": digest,
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "attribution": attribution,
            "model_step_ids": sorted(used_steps),
        })
    acknowledge_restored_steps(saved.get("model_step_ids", []))
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
    from model_gateway_client import collect_model_steps, acknowledge_restored_steps
    extracted = [node.model_dump(mode="json") for node in extracted_nodes]
    saved = store.save_or_load(*identity, "extracted_nodes", extracted)
    if saved != extracted:
        raise RuntimeError("extracted node UUID/input drift")
    resolved = store.save_or_load(*identity, "resolved_nodes")
    if resolved is not None:
        acknowledge_restored_steps(resolved.get("model_step_ids", []))
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

    resolution_inputs = (episode, previous_episodes, entity_types,
                         extracted_nodes, candidate_nodes_by_extracted)
    resolution, resolution_digest = _stage_record(
        store, identity, "node_resolution",
        resolution_inputs,
        allow_retry=_active_manual_stage() == "node_resolution")
    if resolution is not None:
        resolved = store.save_or_load(*identity, "resolved_nodes")
        if resolved is None:
            raise RuntimeError("node resolution receipt is missing")
        return (
            [EntityNode.model_validate(node) for node in resolved["nodes"]],
            resolved["uuid_map"],
            [(EntityNode.model_validate(left), EntityNode.model_validate(right))
             for left, right in resolved["duplicates"]],
        )

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
    used_steps = set()
    if state.unresolved_indices:
        llm_candidates = ops._merge_candidate_nodes(
            [candidate for idx in state.unresolved_indices
             for candidate in candidate_nodes_by_extracted[idx]], None)
        store.begin_stage(*identity, "node_resolution", resolution_digest)
        from model_gateway_client import model_stage
        with model_stage("node_resolution"), collect_model_steps() as used_steps:
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
        "model_step_ids": sorted(used_steps),
        "nodes": [node.model_dump(mode="json") for node in result[0]],
        "uuid_map": result[1],
        "duplicates": [[left.model_dump(mode="json"), right.model_dump(mode="json")]
                       for left, right in result[2]],
    }
    saved_resolved = store.save_or_load(*identity, "resolved_nodes", resolved_json)
    if saved_resolved != resolved_json:
        raise RuntimeError("concurrent resolved node artifact drift")
    if state.unresolved_indices:
        store.save_or_load(*identity, "node_resolution", {
            "stage_input_digest": resolution_digest,
            "resolved_nodes_digest": _stage_digest(resolved_json),
        })
    return result


def _stage_record(store, identity, stage, inputs, *, allow_retry: bool = False):
    digest = _stage_digest(inputs)
    saved = store.save_or_load(*identity, stage)
    if saved is not None:
        if saved["stage_input_digest"] != digest:
            raise RuntimeError(f"{stage} input drift")
        return saved, digest
    started = store.save_or_load(*identity, stage + "_started")
    if started is not None:
        if not allow_retry or started.get("stage_input_digest") != digest:
            raise RuntimeError(f"{stage} interrupted without complete artifact; freeze")
    return None, digest


async def extract_and_resolve_edges_with_snapshot(
    graphiti, episode, extracted_nodes, previous_episodes, edge_type_map,
    group_id, edge_types, nodes, uuid_map, custom_extraction_instructions,
    *, store: StageArtifactStore, task_sd: str, task_sid: str,
    operation_id: str, input_digest: str,
):
    """Replay complete edge output; freeze an interrupted upstream edge phase."""
    from graphiti_core.edges import EntityEdge

    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for edge-stage adapter")
    identity = (task_sd, task_sid, operation_id, input_digest)
    from model_gateway_client import collect_model_steps, acknowledge_restored_steps
    inputs = (episode, extracted_nodes, previous_episodes, edge_type_map,
              group_id, edge_types, nodes, uuid_map,
              custom_extraction_instructions)
    saved, digest = _stage_record(
        store, identity, "edge_phase", inputs,
        allow_retry=_active_manual_stage() == "edge_phase")
    if saved is None:
        store.begin_stage(*identity, "edge_phase", digest)
        from model_gateway_client import model_stage
        with model_stage("edge_phase"), collect_model_steps() as used_steps:
            groups = await graphiti._extract_and_resolve_edges(*inputs)
        saved = store.save_or_load(*identity, "edge_phase", {
            "stage_input_digest": digest,
            "model_step_ids": sorted(used_steps),
            "groups": [[edge.model_dump(mode="json") for edge in group]
                       for group in groups],
        })
    acknowledge_restored_steps(saved.get("model_step_ids", []))
    return tuple([[EntityEdge.model_validate(edge) for edge in group]
                  for group in saved["groups"]])


async def extract_attributes_with_snapshot(
    graphiti, nodes, episode, previous_episodes, entity_types, new_edges,
    *, store: StageArtifactStore, task_sd: str, task_sid: str,
    operation_id: str, input_digest: str,
):
    """Replay complete attributes; freeze an interrupted upstream model phase."""
    from graphiti_core.nodes import EntityNode
    from graphiti_core.utils.maintenance import node_operations as ops

    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for attribute-stage adapter")
    identity = (task_sd, task_sid, operation_id, input_digest)
    from model_gateway_client import collect_model_steps, acknowledge_restored_steps
    inputs = (nodes, episode, previous_episodes, entity_types, new_edges)
    saved, digest = _stage_record(
        store, identity, "attribute_phase", inputs,
        allow_retry=_active_manual_stage() == "attribute_phase")
    if saved is None:
        store.begin_stage(*identity, "attribute_phase", digest)
        from model_gateway_client import model_stage
        with model_stage("attribute_phase"), collect_model_steps() as used_steps:
            hydrated = await ops.extract_attributes_from_nodes(
                graphiti.clients, nodes, episode, previous_episodes,
                entity_types, edges=new_edges)
        saved = store.save_or_load(*identity, "attribute_phase", {
            "stage_input_digest": digest,
            "model_step_ids": sorted(used_steps),
            "nodes": [node.model_dump(mode="json") for node in hydrated],
        })
    acknowledge_restored_steps(saved.get("model_step_ids", []))
    return [EntityNode.model_validate(node) for node in saved["nodes"]]


async def commit_episode_with_receipt(
    graphiti, episode, hydrated_nodes, entity_edges, now, group_id,
    saga, saga_previous_episode_uuid, node_episode_index_map,
    *, store: StageArtifactStore, task_sd: str, task_sid: str,
    operation_id: str, input_digest: str,
):
    """Return a durable receipt or freeze any uncertain graph write.

    This does not infer business success from a missing receipt. It deliberately
    requires external graph verification after a crash between write and receipt.
    """
    from graphiti_core.edges import EpisodicEdge
    from graphiti_core.nodes import EpisodicNode

    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for commit adapter")
    identity = (task_sd, task_sid, operation_id, input_digest)
    inputs = (episode, hydrated_nodes, entity_edges, now, group_id,
              saga, saga_previous_episode_uuid, node_episode_index_map)
    saved, digest = _stage_record(store, identity, "graph_commit_receipt", inputs)
    if saved is None:
        store.begin_graph_commit(*identity, expected={
            "episode": episode.model_dump(mode="json"),
            "nodes": [node.model_dump(mode="json") for node in hydrated_nodes],
            "entity_edges": [edge.model_dump(mode="json") for edge in entity_edges],
            "saga_expected": saga is not None,
        })
        episodic_edges, saved_episode = await graphiti._process_episode_data(*inputs)
        saved = store.save_or_load(*identity, "graph_commit_receipt", {
            "stage_input_digest": digest,
            "episode": saved_episode.model_dump(mode="json"),
            "episodic_edges": [edge.model_dump(mode="json")
                               for edge in episodic_edges],
        })
    return ([EpisodicEdge.model_validate(edge) for edge in saved["episodic_edges"]],
            EpisodicNode.model_validate(saved["episode"]))


def operation_input_digest(kwargs: dict) -> str:
    """Digest every business argument accepted by the supported live path."""
    return _stage_digest(kwargs)


async def add_episode_with_stage_checkpoint(
    graphiti, *, store: StageArtifactStore, task_sd: str, task_sid: str,
    operation_id: str, relevant_schema_limit: int, on_episode_identity=None,
    **kwargs,
):
    """Pinned 0.29.0 single-episode continuation for the default kg-hub path.

    This deliberately supports only the non-saga, non-community path used by
    kg-hub's ordinary ingest. Every Graphiti phase output and its exact inputs
    are persisted before the next phase. Interrupted edge/attribute/commit
    phases freeze; only extraction or node-resolution may be manually resumed.
    """
    from importlib.metadata import version
    from graphiti_core.graphiti import AddEpisodeResults
    from graphiti_core.helpers import (
        validate_excluded_entity_types, validate_group_id,
    )
    from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types
    from graphiti_core.nodes import EpisodeType, EpisodicNode
    from graphiti_core.search.search_utils import RELEVANT_SCHEMA_LIMIT
    from graphiti_core.utils.datetime_utils import utc_now

    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for stage continuation")
    if relevant_schema_limit != RELEVANT_SCHEMA_LIMIT:
        raise RuntimeError("Graphiti schema context limit changed")
    if kwargs.get("uuid") is not None or kwargs.get("saga") is not None:
        raise RuntimeError("stage continuation only supports new non-saga episodes")
    if kwargs.get("update_communities", False):
        raise RuntimeError("stage continuation does not support community updates")

    name = kwargs["name"]
    episode_body = kwargs["episode_body"]
    source_description = kwargs["source_description"]
    reference_time = kwargs["reference_time"]
    source = kwargs.get("source", EpisodeType.message)
    group_id = kwargs.get("group_id")
    entity_types = kwargs.get("entity_types")
    excluded_entity_types = kwargs.get("excluded_entity_types")
    edge_types = kwargs.get("edge_types")
    edge_type_map = kwargs.get("edge_type_map")
    custom_extraction_instructions = kwargs.get("custom_extraction_instructions")
    if group_id is None:
        raise RuntimeError("stage continuation requires an explicit graph group")
    validate_entity_types(entity_types)
    validate_excluded_entity_types(excluded_entity_types, entity_types)
    validate_group_id(group_id)
    if group_id != graphiti.driver._database:
        graphiti.driver = graphiti.driver.clone(database=group_id)
        graphiti.clients.driver = graphiti.driver
    edge_type_map_default = (
        {("Entity", "Entity"): list(edge_types.keys())}
        if edge_types is not None else {("Entity", "Entity"): []}
    )
    edge_type_map = edge_type_map or edge_type_map_default

    # Include optional arguments after defaults are resolved, so any behavioral
    # drift changes the operation identity and stops continuation.
    operation_args = {
        "name": name, "episode_body": episode_body,
        "source_description": source_description,
        "reference_time": reference_time, "source": source,
        "group_id": group_id, "entity_types": entity_types,
        "excluded_entity_types": excluded_entity_types,
        "edge_types": edge_types, "edge_type_map": edge_type_map,
        "custom_extraction_instructions": custom_extraction_instructions,
        "update_communities": False, "saga": None,
    }
    input_digest = operation_input_digest(operation_args)
    identity = (task_sd, task_sid, operation_id, input_digest)
    envelope = store.save_or_load(*identity, "operation_envelope")
    if envelope is None:
        now = utc_now()
        previous_episodes = await graphiti.retrieve_episodes(
            reference_time, last_n=relevant_schema_limit,
            group_ids=[group_id], source=source)
        episode = EpisodicNode(
            name=name, group_id=group_id, labels=[], source=source,
            content=episode_body, source_description=source_description,
            created_at=now, valid_at=reference_time)
        envelope_value = {
            "schema_version": "graphiti-core-0.29.0",
            "input_digest": input_digest,
            "now": now.isoformat(),
            "episode": episode.model_dump(mode="json"),
            "previous_episodes": [item.model_dump(mode="json")
                                  for item in previous_episodes],
        }
        envelope = store.save_or_load(*identity, "operation_envelope", envelope_value)
    if (envelope.get("input_digest") != input_digest
            or envelope.get("schema_version") != "graphiti-core-0.29.0"):
        raise RuntimeError("stage operation envelope identity drift")
    episode = EpisodicNode.model_validate(envelope["episode"])
    previous_episodes = [EpisodicNode.model_validate(item)
                         for item in envelope["previous_episodes"]]
    now = datetime.fromisoformat(envelope["now"].replace("Z", "+00:00"))
    if on_episode_identity is not None:
        await on_episode_identity(episode.uuid)

    extracted_nodes, node_episode_index_map = await extract_nodes_with_snapshot(
        graphiti.clients, episode, previous_episodes, entity_types,
        excluded_entity_types, custom_extraction_instructions,
        store=store, task_sd=task_sd, task_sid=task_sid,
        operation_id=operation_id, input_digest=input_digest)
    nodes, uuid_map, _ = await resolve_nodes_with_candidate_snapshot(
        graphiti.clients, extracted_nodes, episode, previous_episodes,
        entity_types, store=store, task_sd=task_sd, task_sid=task_sid,
        operation_id=operation_id, input_digest=input_digest)
    resolved_edges, invalidated_edges, new_edges = await extract_and_resolve_edges_with_snapshot(
        graphiti, episode, extracted_nodes, previous_episodes, edge_type_map,
        group_id, edge_types, nodes, uuid_map, custom_extraction_instructions,
        store=store, task_sd=task_sd, task_sid=task_sid,
        operation_id=operation_id, input_digest=input_digest)
    entity_edges = resolved_edges + invalidated_edges
    hydrated_nodes = await extract_attributes_with_snapshot(
        graphiti, nodes, episode, previous_episodes, entity_types, new_edges,
        store=store, task_sd=task_sd, task_sid=task_sid,
        operation_id=operation_id, input_digest=input_digest)
    episodic_edges, episode = await commit_episode_with_receipt(
        graphiti, episode, hydrated_nodes, entity_edges, now, group_id,
        None, None, node_episode_index_map,
        store=store, task_sd=task_sd, task_sid=task_sid,
        operation_id=operation_id, input_digest=input_digest)
    return AddEpisodeResults(
        episode=episode, episodic_edges=episodic_edges, nodes=hydrated_nodes,
        edges=entity_edges, communities=[], community_edges=[])


async def inspect_graph_commit_materialization(driver, *, episode, nodes,
                                               entity_edges) -> dict:
    """Read only the exact pinned Graphiti/Falkor core write identities.

    A complete core write is not proof of a business success: Graphiti's saga
    links are written after its bulk transaction, and an interrupted commit may
    have left them incomplete. The caller must not promote a task from this
    result alone.
    """
    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for commit readback")
    if not episode.uuid or not episode.group_id:
        raise RuntimeError("episode identity is incomplete")
    expected_nodes = {node.uuid: node for node in nodes}
    expected_edges = {edge.uuid: edge for edge in entity_edges}
    if len(expected_nodes) != len(nodes) or len(expected_edges) != len(entity_edges):
        raise RuntimeError("duplicate expected graph identities")

    episode_rows, _, _ = await driver.execute_query(
        "MATCH (e:Episodic {uuid: $uuid}) "
        "RETURN e.uuid AS uuid, e.group_id AS group_id, "
        "e.name AS name, e.source_description AS source_description",
        uuid=episode.uuid)
    if not episode_rows:
        return {"phase": "core_absent", "episode_uuid": episode.uuid,
                "reason": "exact_episode_missing"}
    if (len(episode_rows) != 1 or episode_rows[0].get("group_id") != episode.group_id
            or episode_rows[0].get("name") != episode.name
            or episode_rows[0].get("source_description") != episode.source_description):
        return {"phase": "unknown", "episode_uuid": episode.uuid,
                "reason": "episode_identity_mismatch"}

    node_rows, _, _ = await driver.execute_query(
        "MATCH (n:Entity) WHERE n.uuid IN $uuids "
        "RETURN n.uuid AS uuid, n.group_id AS group_id",
        uuids=list(expected_nodes))
    actual_nodes = {row.get("uuid") for row in node_rows
                    if row.get("group_id") == episode.group_id}
    edge_rows, _, _ = await driver.execute_query(
        "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) "
        "WHERE r.uuid IN $uuids "
        "RETURN r.uuid AS uuid, r.group_id AS group_id, "
        "a.uuid AS source_uuid, b.uuid AS target_uuid",
        uuids=list(expected_edges))
    actual_edges = {row.get("uuid") for row in edge_rows
                    if (row.get("uuid") in expected_edges
                        and row.get("group_id") == episode.group_id
                        and row.get("source_uuid") == expected_edges[row["uuid"]].source_node_uuid
                        and row.get("target_uuid") == expected_edges[row["uuid"]].target_node_uuid)}
    mention_rows, _, _ = await driver.execute_query(
        "MATCH (e:Episodic {uuid: $uuid})-[r:MENTIONS]->(n:Entity) "
        "RETURN n.uuid AS node_uuid, r.group_id AS group_id",
        uuid=episode.uuid)
    actual_mentions = {row.get("node_uuid") for row in mention_rows
                       if row.get("group_id") == episode.group_id}
    missing = {
        "nodes": sorted(set(expected_nodes) - actual_nodes),
        "entity_edges": sorted(set(expected_edges) - actual_edges),
        "mentions": sorted(set(expected_nodes) - actual_mentions),
    }
    return {"phase": "core_materialized" if not any(missing.values()) else "partial",
            "episode_uuid": episode.uuid, "missing": missing,
            "reason": "saga_and_business_receipt_unverified"}


async def inspect_started_graph_commit(driver, store: StageArtifactStore, *,
                                       task_sd: str, task_sid: str,
                                       operation_id: str, input_digest: str) -> dict:
    """Read back a fenced commit from its exact pre-write snapshot; no mutation."""
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode, EpisodicNode

    identity = (task_sd, task_sid, operation_id, input_digest)
    marker = store.save_or_load(*identity, "graph_commit_started")
    if marker is None:
        return {"phase": "not_started"}
    expected = marker.get("expected")
    if not isinstance(expected, dict):
        return {"phase": "unknown", "reason": "prewrite_snapshot_missing"}
    proof = await inspect_graph_commit_materialization(
        driver,
        episode=EpisodicNode.model_validate(expected["episode"]),
        nodes=[EntityNode.model_validate(node) for node in expected["nodes"]],
        entity_edges=[EntityEdge.model_validate(edge)
                      for edge in expected["entity_edges"]],
    )
    proof["saga_expected"] = bool(expected.get("saga_expected"))
    proof["receipt_saved"] = store.save_or_load(
        *identity, "graph_commit_receipt") is not None
    return proof
