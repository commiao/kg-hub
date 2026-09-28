"""Optimistic Graphiti resolution with durable read dependencies and short commit locks.

Pinned Falkor reads run through GRAPH.RO_QUERY. Every query (including empty
semantic searches) is validated again under the same cross-process writer lock
used by legacy writers. A conflicting round is retained and never submitted;
new model identities apply only to a completed, proven-stale resolution round.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from types import MethodType

from utils.graphiti_stage_adapter import _stage_value, _stage_digest
from utils.writer_lock import async_writer_lock

log = logging.getLogger("kg_hub.parallel")
MAX_ROUNDS = 3
MAX_READS = 1024


class GraphReadConflict(RuntimeError):
    pass


class ReadDependencies:
    """Record exact ordered read results without retaining graph content twice."""
    def __init__(self, driver, store, identity):
        self.driver, self.store, self.identity = driver, store, identity
        self.graph = driver._get_graph(driver._database)
        if not callable(getattr(self.graph, "ro_query", None)):
            raise RuntimeError("optimistic extraction requires Falkor read-only queries")
        self.records = {}
        self._pending = {}
        self._flush_task = None
        self._flush_error = None
        with store._connect() as db:
            rows = db.execute(
                "SELECT stage,input_digest,artifact_json,artifact_digest "
                "FROM graphiti_stage_artifacts WHERE task_sd=? AND task_sid=? "
                "AND operation_id=? AND stage LIKE 'graph_read:%'", identity[:3]).fetchall()
        for stage, digest, payload, checksum in rows:
            if digest != identity[3] or hashlib.sha256(payload.encode()).hexdigest() != checksum:
                raise RuntimeError("graph read dependency identity/corruption")
            self.records[stage] = json.loads(payload)
        self._known_keys = set(self.records)
        if len(self.records) > MAX_READS:
            raise RuntimeError("graph read dependency limit exceeded")

    async def read(self, query, params):
        # Falkor enforces read-only behavior, including procedures. Never fall
        # back to query(), even for a seemingly harmless unsupported statement.
        result = await self.graph.ro_query(query, params)
        header = [h[1] for h in result.header]
        rows = [{field: row[i] if i < len(row) else None
                 for i, field in enumerate(header)} for row in result.result_set]
        return rows, header, None

    async def execute(self, cypher_query_, **kwargs):
        params = _stage_value(kwargs)
        key = "graph_read:" + _stage_digest([cypher_query_, params])
        if key not in self._known_keys and len(self._known_keys) >= MAX_READS:
            raise RuntimeError("graph read dependency limit exceeded")
        result = await self.read(cypher_query_, params)
        record = {"query": cypher_query_, "params": params,
                  "result_digest": _stage_digest(result)}
        if self._flush_error is not None:
            raise self._flush_error
        saved = self.records.get(key) or self._pending.get(key)
        if saved is not None and saved != record:
            raise GraphReadConflict("graph changed within an unfinished resolution stage")
        if key not in self.records:
            self._known_keys.add(key)
            self._pending[key] = record
            if self._flush_task is None:
                self._flush_task = asyncio.create_task(self._persist_pending())
            # A read never reaches Graphiti or a paid model before its durable
            # dependency. Concurrent reads share one FULL synchronous commit.
            await asyncio.shield(self._flush_task)
        return result

    async def _persist_pending(self):
        try:
            await asyncio.sleep(0.005)
            while self._pending:
                pending, self._pending = self._pending, {}
                saved = await asyncio.to_thread(
                    self.store.save_batch_or_load, *self.identity, pending)
                if saved != pending:
                    raise GraphReadConflict("graph changed within an unfinished resolution stage")
                self.records.update(saved)
        except BaseException as exc:
            self._flush_error = exc
            raise
        finally:
            self._flush_task = None

    async def validate(self):
        for record in self.records.values():
            current = await self.read(record["query"], record["params"])
            if _stage_digest(current) != record["result_digest"]:
                return False
        return True

    def graphiti_view(self, graphiti):
        # The original shared Graphiti/driver/client objects are never patched.
        view = copy.copy(graphiti)
        driver = copy.copy(self.driver)
        async def execute(_driver, cypher_query_, **kwargs):
            return await self.execute(cypher_query_, **kwargs)
        def blocked(*args, **kwargs):
            raise RuntimeError("untracked driver access during optimistic extraction")
        driver.execute_query = MethodType(execute, driver)
        driver.session = blocked
        driver.clone = blocked
        driver._get_graph = blocked
        driver.client = None
        view.driver = driver
        clients = graphiti.clients
        view.clients = (clients.model_copy(update={"driver": driver})
                        if hasattr(clients, "model_copy") else copy.copy(clients))
        view.clients.driver = driver
        return view


async def finish_optimistic_episode(
    graphiti, *, store, identity, episode, previous_episodes, extracted_nodes,
    node_episode_index_map, now, entity_types, edge_type_map, group_id, edge_types,
    custom_extraction_instructions,
):
    from graphiti_core.graphiti import AddEpisodeResults
    from graphiti_core.nodes import EntityNode
    from graphiti_core.edges import EntityEdge
    from model_gateway_client import model_operation, acknowledge_restored_steps
    from utils.graphiti_stage_adapter import (
        resolve_nodes_with_candidate_snapshot, extract_and_resolve_edges_with_snapshot,
        extract_attributes_with_snapshot, commit_episode_with_receipt,
    )
    task_sd, task_sid, operation_id, input_digest = identity
    selected = store.save_or_load(*identity, "parallel_selected_round")
    first = selected["round"] if selected else 0
    # Human recovery accounts for every completed paid step, including a
    # proven-stale round. Restoring a prepared commit skips the stage helpers
    # that normally acknowledge these receipts.
    def acknowledge_round(round_identity):
        complete = store.save_or_load(*round_identity, "prepared_commit")
        if complete is None:
            raise RuntimeError("completed parallel round receipt missing")
        acknowledge_restored_steps(complete.get("model_step_ids", []))

    for earlier in range(first):
        old_identity = (task_sd, task_sid, operation_id + f":graph-round:{earlier}", input_digest)
        if store.save_or_load(*old_identity, "graph_conflict") is None:
            raise RuntimeError("selected parallel round lacks prior conflict proof")
        acknowledge_round(old_identity)
    for round_number in range(first, MAX_ROUNDS):
        round_id = operation_id + f":graph-round:{round_number}"
        round_identity = (task_sd, task_sid, round_id, input_digest)
        if store.save_or_load(*round_identity, "graph_conflict") is not None:
            acknowledge_round(round_identity)
            continue
        dependencies = await asyncio.to_thread(
            ReadDependencies, graphiti.driver, store, round_identity)
        view = dependencies.graphiti_view(graphiti)
        common = dict(store=store, task_sd=task_sd, task_sid=task_sid,
                      operation_id=round_id, input_digest=input_digest)
        started = time.monotonic()
        prepared = store.save_or_load(*round_identity, "prepared_commit")
        if prepared is None:
            if selected:
                raise RuntimeError("selected parallel round has no complete artifact")
            # Resolver helpers mutate nodes: use the original extraction snapshot
            # for every round, never mutated objects from a rejected round.
            fresh = [EntityNode.model_validate(n.model_dump(mode="json"))
                     for n in extracted_nodes]
            with model_operation("ingest.graph-round", round_id):
                nodes, uuid_map, _ = await resolve_nodes_with_candidate_snapshot(
                    view.clients, fresh, episode, previous_episodes, entity_types, **common)
                resolved, invalidated, new = await extract_and_resolve_edges_with_snapshot(
                    view, episode, fresh, previous_episodes, edge_type_map, group_id,
                    edge_types, nodes, uuid_map, custom_extraction_instructions, **common)
                hydrated = await extract_attributes_with_snapshot(
                    view, nodes, episode, previous_episodes, entity_types, new, **common)
            # The pinned bulk writer otherwise generates missing embeddings
            # inside its write transaction. Finish them before taking the lock.
            for node in hydrated:
                if node.name_embedding is None:
                    await node.generate_name_embedding(graphiti.embedder)
            for edge in resolved + invalidated:
                if edge.fact_embedding is None:
                    await edge.generate_embedding(graphiti.embedder)
            completed_steps = set()
            for phase in ("resolved_nodes", "edge_phase", "attribute_phase"):
                artifact = store.save_or_load(*round_identity, phase)
                if artifact is not None:
                    completed_steps.update(artifact.get("model_step_ids", []))
            prepared = store.save_or_load(*round_identity, "prepared_commit", {
                "nodes": [n.model_dump(mode="json") for n in hydrated],
                "edges": [e.model_dump(mode="json") for e in resolved + invalidated],
                "reads": sorted(dependencies.records),
                "model_step_ids": sorted(completed_steps),
            })
        acknowledge_round(round_identity)
        if prepared["reads"] != sorted(dependencies.records):
            raise RuntimeError("parallel graph read footprint drift")
        hydrated = [EntityNode.model_validate(n) for n in prepared["nodes"]]
        edges = [EntityEdge.model_validate(e) for e in prepared["edges"]]
        wait_started = time.monotonic()
        async with async_writer_lock(owner="optimistic-graph-commit", timeout_seconds=180):
            acquired = time.monotonic()
            # A receipt can be replayed after later legitimate graph writes.
            # An uncertain prior write is fenced by the original commit helper.
            receipt = store.save_or_load(*identity, "graph_commit_receipt")
            commit_started = store.save_or_load(*identity, "graph_commit_started")
            if receipt is None and commit_started is None:
                if not await dependencies.validate():
                    if selected:
                        raise GraphReadConflict("selected commit dependencies changed; freeze")
                    store.save_or_load(*round_identity, "graph_conflict", {"validated": False})
                    log.info("[ingest:parallel_conflict] sid=%s round=%d reads=%d",
                             task_sid, round_number, len(dependencies.records))
                    continue
                selected = store.save_or_load(*identity, "parallel_selected_round",
                                             {"round": round_number})
                if selected["round"] != round_number:
                    raise RuntimeError("parallel commit selection drift")
            episodic_edges, saved_episode = await commit_episode_with_receipt(
                graphiti, episode, hydrated, edges, now, group_id,
                None, None, node_episode_index_map,
                store=store, task_sd=task_sd, task_sid=task_sid,
                operation_id=operation_id, input_digest=input_digest)
            finished = time.monotonic()
        log.info("[ingest:parallel_timing] sid=%s round=%d prepare=%.3fs "
                 "lock_wait=%.3fs commit=%.3fs reads=%d", task_sid, round_number,
                 wait_started-started, acquired-wait_started, finished-acquired,
                 len(dependencies.records))
        return AddEpisodeResults(episode=saved_episode, episodic_edges=episodic_edges,
                                 nodes=hydrated, edges=edges, communities=[], community_edges=[])
    raise GraphReadConflict("graph remained contended after bounded completed rounds")
