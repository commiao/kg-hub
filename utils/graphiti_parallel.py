"""Optimistic Graphiti resolution with durable read dependencies and short commit locks.

Pinned Falkor reads run through GRAPH.RO_QUERY. Every query (including empty
semantic searches) is validated again under the same cross-process writer lock
used by legacy writers. A conflicting round is retained and never submitted;
new model identities apply only to a completed, proven-stale resolution round.
"""
from __future__ import annotations

import copy
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
        with store._connect() as db:
            stages = [row[0] for row in db.execute(
                "SELECT stage FROM graphiti_stage_artifacts WHERE task_sd=? AND task_sid=? "
                "AND operation_id=? AND stage LIKE 'graph_read:%'", identity[:3])]
        for stage in stages:
            self.records[stage] = store.save_or_load(*identity, stage)
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
        if key not in self.records and len(self.records) >= MAX_READS:
            raise RuntimeError("graph read dependency limit exceeded")
        result = await self.read(cypher_query_, params)
        record = {"query": cypher_query_, "params": params,
                  "result_digest": _stage_digest(result)}
        saved = self.store.save_or_load(*self.identity, key, record)
        if saved != record:
            # A graph changed during a stage, possibly after a paid call.
            # Do not silently create a new paid round from an incomplete stage.
            raise GraphReadConflict("graph changed within an unfinished resolution stage")
        self.records[key] = saved
        return result

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
    from model_gateway_client import model_operation
    from utils.graphiti_stage_adapter import (
        resolve_nodes_with_candidate_snapshot, extract_and_resolve_edges_with_snapshot,
        extract_attributes_with_snapshot, commit_episode_with_receipt,
    )
    task_sd, task_sid, operation_id, input_digest = identity
    selected = store.save_or_load(*identity, "parallel_selected_round")
    first = selected["round"] if selected else 0
    for round_number in range(first, MAX_ROUNDS):
        round_id = operation_id + f":graph-round:{round_number}"
        round_identity = (task_sd, task_sid, round_id, input_digest)
        if store.save_or_load(*round_identity, "graph_conflict") is not None:
            continue
        dependencies = ReadDependencies(graphiti.driver, store, round_identity)
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
            prepared = store.save_or_load(*round_identity, "prepared_commit", {
                "nodes": [n.model_dump(mode="json") for n in hydrated],
                "edges": [e.model_dump(mode="json") for e in resolved + invalidated],
                "reads": sorted(dependencies.records),
            })
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
