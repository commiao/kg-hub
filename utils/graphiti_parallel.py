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
import os
import time
from types import MethodType

from utils.graphiti_stage_adapter import _stage_value, _stage_digest
from utils import flow_metrics
from utils.writer_lock import async_writer_lock, read_generation

log = logging.getLogger("kg_hub.parallel")
MAX_ROUNDS = 3
MAX_READS = 1024
# Re-reads that decide a commit either run under the writer lock or are proven
# free of any interleaved holder, so their order does not matter. 1 restores
# the original one-by-one validation.
VALIDATE_CONCURRENCY = max(1, int(os.environ.get("KG_HUB_COMMIT_VALIDATE_CONCURRENCY", "8")))
# 0 restores validating only under the lock.
PREVALIDATE = os.environ.get("KG_HUB_COMMIT_PREVALIDATE", "1") != "0"


class _LoopLag:
    """Cumulative event-loop stall: time this loop could not run a ready callback.

    A commit coroutine holds the writer lock while other episodes share its loop;
    a blocking call anywhere on the loop stretches the lock hold.
    """
    INTERVAL = 0.1

    def __init__(self):
        self.total = 0.0
        self._task = None
        self._loop = None

    def reading(self) -> float:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._task is None or self._task.done():
            self._loop, self.total = loop, 0.0
            self._task = loop.create_task(self._run())
        return self.total

    async def _run(self):
        while True:
            started = time.monotonic()
            await asyncio.sleep(self.INTERVAL)
            self.total += max(0.0, time.monotonic() - started - self.INTERVAL)


_loop_lag = _LoopLag()


class GraphReadConflict(RuntimeError):
    pass


def _vector_read_lock():
    # Falkor's relationship ANN query returned different top-result sets when
    # multiple read-only calls ran concurrently on an unchanged graph. One
    # lock per event loop covers prepare and every validation phase, across
    # all observations; ordinary graph reads keep their existing concurrency.
    loop = asyncio.get_running_loop()
    lock = getattr(loop, "_kg_hub_vector_read_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        loop._kg_hub_vector_read_lock = lock
    return lock


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

    async def read(self, query, params, *, phase="prepare"):
        # Falkor enforces read-only behavior, including procedures. Never fall
        # back to query(), even for a seemingly harmless unsupported statement.
        similarity = None
        if 'e.fact_embedding' in query and 'cosineDistance' in query:
            similarity = 'edge'
        elif 'n.name_embedding' in query and 'cosineDistance' in query:
            similarity = 'node'
        started = time.monotonic() if similarity else None
        try:
            if 'db.idx.vector.queryRelationships' in query:
                async with _vector_read_lock():
                    result = await self.graph.ro_query(query, params)
            else:
                result = await self.graph.ro_query(query, params)
        finally:
            if similarity:
                log.info('[ingest:similarity_query] sid=%s phase=%s kind=%s '
                         'scope=%s seconds=%.3f', self.identity[1], phase,
                         similarity, 'filtered' if 'edge_uuids' in query else 'group',
                         time.monotonic() - started)
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

    async def validate(self, concurrency: int = 1, *, phase="validate"):
        records = list(self.records.values())
        if concurrency <= 1:
            for record in records:
                current = await self.read(record["query"], record["params"], phase=phase)
                if _stage_digest(current) != record["result_digest"]:
                    return False
            return True
        slots = asyncio.Semaphore(concurrency)

        async def unchanged(record):
            async with slots:
                current = await self.read(record["query"], record["params"], phase=phase)
            return _stage_digest(current) == record["result_digest"]

        checks = [asyncio.ensure_future(unchanged(record)) for record in records]
        try:
            for check in asyncio.as_completed(checks):
                if not await check:
                    return False
            return True
        finally:
            for check in checks:
                check.cancel()
            await asyncio.gather(*checks, return_exceptions=True)

    def graphiti_view(self, graphiti):
        # The original shared Graphiti/driver/client objects are never patched.
        view = copy.copy(graphiti)
        driver = copy.copy(self.driver)
        async def execute(_driver, cypher_query_, **kwargs):
            return await self.execute(cypher_query_, **kwargs)
        def blocked(*args, **kwargs):
            raise RuntimeError("untracked driver access during optimistic extraction")
        driver.execute_query = MethodType(execute, driver)
        driver._kg_hub_indexed_ingest = True
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
    # SQLite commits fsync and can wait on the write lock; keep them off the loop.
    def load(*args):
        return asyncio.to_thread(store.save_or_load, *args)

    selected = await load(*identity, "parallel_selected_round")
    first = selected["round"] if selected else 0
    # Human recovery accounts for every completed paid step, including a
    # proven-stale round. Restoring a prepared commit skips the stage helpers
    # that normally acknowledge these receipts.
    async def acknowledge_round(round_identity):
        complete = await load(*round_identity, "prepared_commit")
        if complete is None:
            raise RuntimeError("completed parallel round receipt missing")
        acknowledge_restored_steps(complete.get("model_step_ids", []))

    for earlier in range(first):
        old_identity = (task_sd, task_sid, operation_id + f":graph-round:{earlier}", input_digest)
        if await load(*old_identity, "graph_conflict") is None:
            raise RuntimeError("selected parallel round lacks prior conflict proof")
        await acknowledge_round(old_identity)
    for round_number in range(first, MAX_ROUNDS):
        round_id = operation_id + f":graph-round:{round_number}"
        round_identity = (task_sd, task_sid, round_id, input_digest)
        if await load(*round_identity, "graph_conflict") is not None:
            await acknowledge_round(round_identity)
            continue
        dependencies = await asyncio.to_thread(
            ReadDependencies, graphiti.driver, store, round_identity)
        view = dependencies.graphiti_view(graphiti)
        common = dict(store=store, task_sd=task_sd, task_sid=task_sid,
                      operation_id=round_id, input_digest=input_digest)
        started = time.monotonic()
        prepared = await load(*round_identity, "prepared_commit")
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
                artifact = await load(*round_identity, phase)
                if artifact is not None:
                    completed_steps.update(artifact.get("model_step_ids", []))
            prepared = await load(*round_identity, "prepared_commit", {
                "nodes": [n.model_dump(mode="json") for n in hydrated],
                "edges": [e.model_dump(mode="json") for e in resolved + invalidated],
                "reads": sorted(dependencies.records),
                "model_step_ids": sorted(completed_steps),
            })
        await acknowledge_round(round_identity)
        if prepared["reads"] != sorted(dependencies.records):
            raise RuntimeError("parallel graph read footprint drift")
        hydrated = [EntityNode.model_validate(n) for n in prepared["nodes"]]
        edges = [EntityEdge.model_validate(e) for e in prepared["edges"]]
        # Validate before queueing for the lock. A conflict found here skips
        # the lock entirely; a clean result stays valid under the lock only
        # while no other holder has taken it since (see read_generation).
        prevalidated_at = None
        step = time.monotonic()
        if (PREVALIDATE and not selected
                and await load(*identity, "graph_commit_receipt") is None
                and await load(*identity, "graph_commit_started") is None):
            generation = read_generation()
            if not await dependencies.validate(VALIDATE_CONCURRENCY, phase="prevalidate"):
                await load(*round_identity, "graph_conflict", {"validated": False})
                log.info("[ingest:parallel_conflict] sid=%s round=%d reads=%d "
                         "lock_wait=0.000s hold=0.000s validate=%.3fs loop_lag=0.000s "
                         "validate_concurrency=%d prevalidated=1",
                         task_sid, round_number, len(dependencies.records),
                         time.monotonic() - step, VALIDATE_CONCURRENCY)
                await asyncio.to_thread(flow_metrics.record, conflict=True,
                                        prevalidated_conflict=True)
                continue
            if generation % 2 == 0:
                prevalidated_at = generation
        prevalidate = time.monotonic() - step
        wait_started = time.monotonic()
        conflict_inside_lock = False
        async with async_writer_lock(owner="optimistic-graph-commit", timeout_seconds=180):
            acquired = time.monotonic()
            lag_at_acquire = _loop_lag.reading()
            steps = {"prevalidate": prevalidate}
            skipped = prevalidated_at is not None and read_generation() == prevalidated_at + 1
            # A receipt can be replayed after later legitimate graph writes.
            # An uncertain prior write is fenced by the original commit helper.
            receipt = await load(*identity, "graph_commit_receipt")
            commit_started = await load(*identity, "graph_commit_started")
            steps["fence"] = time.monotonic() - acquired
            if receipt is None and commit_started is None:
                step = time.monotonic()
                unchanged = skipped or await dependencies.validate(VALIDATE_CONCURRENCY)
                steps["validate"] = time.monotonic() - step
                if not unchanged:
                    if selected:
                        raise GraphReadConflict("selected commit dependencies changed; freeze")
                    await load(*round_identity, "graph_conflict", {"validated": False})
                    log.info("[ingest:parallel_conflict] sid=%s round=%d reads=%d "
                             "lock_wait=%.3fs hold=%.3fs validate=%.3fs loop_lag=%.3fs "
                             "validate_concurrency=%d",
                             task_sid, round_number, len(dependencies.records),
                             acquired-wait_started, time.monotonic()-acquired,
                             steps["validate"], _loop_lag.reading()-lag_at_acquire,
                             VALIDATE_CONCURRENCY)
                    conflict_inside_lock = True
                else:
                    step = time.monotonic()
                    selected = await load(*identity, "parallel_selected_round",
                                          {"round": round_number})
                    steps["select"] = time.monotonic() - step
                    if selected["round"] != round_number:
                        raise RuntimeError("parallel commit selection drift")
            if not conflict_inside_lock:
                episodic_edges, saved_episode = await commit_episode_with_receipt(
                    graphiti, episode, hydrated, edges, now, group_id,
                    None, None, node_episode_index_map,
                    store=store, task_sd=task_sd, task_sid=task_sid,
                    operation_id=operation_id, input_digest=input_digest, timings=steps)
                finished = time.monotonic()
                loop_lag = _loop_lag.reading() - lag_at_acquire
        if conflict_inside_lock:
            await asyncio.to_thread(flow_metrics.record, conflict=True,
                                    lock_wait_s=acquired-wait_started)
            continue
        log.info("[ingest:parallel_timing] sid=%s round=%d prepare=%.3fs "
                 "lock_wait=%.3fs commit=%.3fs reads=%d "
                 "fence=%.3fs validate=%.3fs select=%.3fs receipt_lookup=%.3fs "
                 "begin=%.3fs write=%.3fs receipt_save=%.3fs loop_lag=%.3fs "
                 "validate_concurrency=%d prevalidate=%.3fs validate_skipped=%d",
                 task_sid, round_number,
                 wait_started-started, acquired-wait_started, finished-acquired,
                 len(dependencies.records),
                 *(steps.get(k, 0.0) for k in ("fence", "validate", "select", "receipt_lookup",
                                                "begin", "write", "receipt_save")),
                 loop_lag, VALIDATE_CONCURRENCY, steps["prevalidate"], int(skipped))
        await asyncio.to_thread(flow_metrics.record, conflict=False,
                                lock_wait_s=acquired-wait_started,
                                commit_s=finished-acquired, validate_skipped=skipped)
        return AddEpisodeResults(episode=saved_episode, episodic_edges=episodic_edges,
                                 nodes=hydrated, edges=edges, communities=[], community_edges=[])
    raise GraphReadConflict("graph remained contended after bounded completed rounds")
