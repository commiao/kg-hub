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


def _interval_wall_seconds(intervals):
    """Elapsed wall time covered by overlapping graph calls, counted once."""
    covered = 0.0
    end = None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            covered += stop - start
            end = stop
        elif stop > end:
            covered += stop - end
            end = stop
    return covered
# Re-reads that decide a commit either run under the writer lock or are proven
# free of any interleaved holder, so their order does not matter. 1 restores
# the original one-by-one validation.
VALIDATE_CONCURRENCY = max(1, int(os.environ.get("KG_HUB_COMMIT_VALIDATE_CONCURRENCY", "8")))
# 0 restores validating only under the lock.
PREVALIDATE = os.environ.get("KG_HUB_COMMIT_PREVALIDATE", "1") != "0"
# Reuse an earlier round's edge stage after a conflict when its inputs that the
# edge stage actually depends on are unchanged and its reads still hold.
EDGE_REUSE = os.environ.get("KG_HUB_PARALLEL_EDGE_REUSE", "1") != "0"


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


def edge_reuse_key(episode, extracted_nodes, previous_episodes, edge_type_map,
                   group_id, edge_types, nodes, uuid_map, custom_extraction_instructions):
    """What the edge stage depends on (graphiti-core 0.29.0).

    extract_edges uses the original extracted nodes; resolve_extracted_edges
    uses resolved entities only by uuid and labels. Entity summaries, which
    other observations rewrite constantly, are deliberately excluded.
    """
    projected = sorted((n.uuid, sorted(n.labels or [])) for n in nodes)
    return _stage_digest([episode, extracted_nodes, previous_episodes, edge_type_map,
                          group_id, edge_types, projected, uuid_map,
                          custom_extraction_instructions])


def _read_kind(query):
    if 'e.fact_embedding' in query and 'cosineDistance' in query:
        return "edge_similarity"
    if 'n.name_embedding' in query and 'cosineDistance' in query:
        return "node_similarity"
    if 'db.idx.vector.' in query:
        return "vector_index"
    return "other"


class ReadDependencies:
    """Record exact ordered read results without retaining graph content twice."""
    def __init__(self, driver, store, identity):
        self.driver, self.store, self.identity = driver, store, identity
        self.graph = driver._get_graph(driver._database)
        if not callable(getattr(self.graph, "ro_query", None)):
            raise RuntimeError("optimistic extraction requires Falkor read-only queries")
        self.records = {}
        # Which stage issued each read and which read went stale. In memory
        # only: persisted records must stay byte-identical for replay.
        self.current_phase = "restored"
        self._read_phase = {}
        self._touched = {}
        self.last_stale = None
        self._read_times = []
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
        started = time.monotonic()
        try:
            if 'db.idx.vector.queryRelationships' in query:
                async with _vector_read_lock():
                    result = await self.graph.ro_query(query, params)
            else:
                result = await self.graph.ro_query(query, params)
        finally:
            finished = time.monotonic()
            self._read_times.append((phase, similarity or "other", started, finished))
            if similarity:
                log.info('[ingest:similarity_query] sid=%s phase=%s kind=%s '
                         'scope=%s seconds=%.3f', self.identity[1], phase,
                         similarity, 'filtered' if 'edge_uuids' in query else 'group',
                         finished - started)
        header = [h[1] for h in result.header]
        rows = [{field: row[i] if i < len(row) else None
                 for i, field in enumerate(header)} for row in result.result_set]
        return rows, header, None

    def read_summary(self, phase):
        rows = [(kind, start, stop) for read_phase, kind, start, stop
                in self._read_times if read_phase == phase]
        return (len(rows),
                _interval_wall_seconds((start, stop) for _, start, stop in rows),
                _interval_wall_seconds((start, stop) for kind, start, stop in rows
                                       if kind == "other"))

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
        self._touched.setdefault(self.current_phase, set()).add(key)
        if key not in self.records:
            self._read_phase.setdefault(key, self.current_phase)
            self._known_keys.add(key)
            self._pending[key] = record
            if self._flush_task is None:
                self._flush_task = asyncio.create_task(self._persist_pending())
            # A read never reaches Graphiti or a paid model before its durable
            # dependency. Concurrent reads share one journal commit.
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

    def _note_stale(self, key, record):
        self.last_stale = {"kind": _read_kind(record["query"]),
                           "phase": self._read_phase.get(key, "restored")}

    def phase_keys(self, phase):
        """Every read a stage issued in this process, including repeated ones."""
        return sorted(self._touched.get(phase, ()))

    async def records_unchanged(self, records, concurrency=1):
        """Re-read another round's dependencies against the current graph."""
        slots = asyncio.Semaphore(max(1, concurrency))

        async def unchanged(key, record):
            async with slots:
                current = await self.read(record["query"], record["params"], phase="reuse_check")
            if _stage_digest(current) != record["result_digest"]:
                self._note_stale(key, record)
                return False
            return True

        self.last_stale = None
        results = await asyncio.gather(*(unchanged(k, r) for k, r in records.items()))
        return all(results)

    async def adopt(self, records, phase):
        """Make verified reads of an earlier round dependencies of this round.

        Persisted before the reused stage output, so a restart validates them
        at commit exactly like reads this round issued itself.
        """
        if self._flush_task is not None:
            await asyncio.shield(self._flush_task)
        if self._flush_error is not None:
            raise self._flush_error
        new = {k: v for k, v in records.items() if k not in self.records}
        if any(self.records[k] != v for k, v in records.items() if k in self.records):
            return False
        if len(self._known_keys | set(new)) > MAX_READS:
            return False
        if new:
            saved = await asyncio.to_thread(self.store.save_batch_or_load, *self.identity, new)
            if saved != new:
                return False
            self.records.update(saved)
            self._known_keys.update(saved)
        for key in records:
            self._read_phase.setdefault(key, phase)
            self._touched.setdefault(phase, set()).add(key)
        return True

    def stale_detail(self):
        stale = self.last_stale or {}
        return "stale_kind=%s stale_phase=%s" % (stale.get("kind", "-"), stale.get("phase", "-"))

    async def validate(self, concurrency: int = 1, *, phase="validate"):
        records = list(self.records.items())
        self.last_stale = None
        if concurrency <= 1:
            for key, record in records:
                current = await self.read(record["query"], record["params"], phase=phase)
                if _stage_digest(current) != record["result_digest"]:
                    self._note_stale(key, record)
                    return False
            return True
        slots = asyncio.Semaphore(concurrency)

        async def unchanged(key, record):
            async with slots:
                current = await self.read(record["query"], record["params"], phase=phase)
            if _stage_digest(current) != record["result_digest"]:
                self._note_stale(key, record)
                return False
            return True

        checks = [asyncio.ensure_future(unchanged(key, record)) for key, record in records]
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
        edge_stage_inputs,
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
            conflict = await load(*round_identity, "graph_conflict")
            if not conflict or not conflict.get("early"):
                raise RuntimeError("completed parallel round receipt missing")
            complete = conflict
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
        phase_seconds = {}
        prepared = await load(*round_identity, "prepared_commit")
        if prepared is None:
            if selected:
                raise RuntimeError("selected parallel round has no complete artifact")
            # Resolver helpers mutate nodes: use the original extraction snapshot
            # for every round, never mutated objects from a rejected round.
            fresh = [EntityNode.model_validate(n.model_dump(mode="json"))
                     for n in extracted_nodes]
            async def abandon_stale_round(before_phase, artifacts):
                """Record a proven-stale round without paying for later stages."""
                completed_steps = set()
                for phase in artifacts:
                    artifact = await load(*round_identity, phase)
                    if artifact is not None:
                        completed_steps.update(artifact.get("model_step_ids", []))
                await load(*round_identity, "graph_conflict", {
                    "validated": False, "early": True,
                    "model_step_ids": sorted(completed_steps),
                })
                acknowledge_restored_steps(completed_steps)
                log.info("[ingest:parallel_conflict] sid=%s round=%d reads=%d "
                         "early=1 phase=%s %s", task_sid, round_number,
                         len(dependencies.records), before_phase,
                         dependencies.stale_detail())
                flow_metrics.record(conflict=True, prevalidated_conflict=True)

            async def reuse_earlier_edges(reuse_key, inputs):
                """Seed this round's edge_phase from the latest earlier round.

                Only when its edge-relevant inputs match and every read its edge
                stage issued still returns the same result. The attribute stage
                always reruns: it rewrites entity summaries.
                """
                for earlier in range(round_number - 1, -1, -1):
                    earlier_identity = (task_sd, task_sid,
                                        operation_id + f":graph-round:{earlier}", input_digest)
                    marker = await load(*earlier_identity, "edge_reuse")
                    edge = await load(*earlier_identity, "edge_phase")
                    if marker is None or edge is None:
                        continue
                    reason = None
                    records = {}
                    if not isinstance(edge.get("groups"), list):
                        reason = "edge_artifact_incomplete"
                    elif marker["key"] != reuse_key:
                        reason = "inputs_changed"
                    else:
                        for key in marker["read_keys"]:
                            record = await load(*earlier_identity, key)
                            if record is None:
                                reason = "reads_missing"
                                break
                            records[key] = record
                    if reason is None and records and not await dependencies.records_unchanged(
                            records, VALIDATE_CONCURRENCY):
                        reason = "reads_changed"
                    if reason is None and records and not await dependencies.adopt(records, "edges"):
                        reason = "reads_conflict"
                    if reason is not None:
                        log.info("[ingest:edge_reuse] sid=%s round=%d from_round=%d result=miss "
                                 "reason=%s reads=%d %s", task_sid, round_number, earlier,
                                 reason, len(marker["read_keys"]),
                                 dependencies.stale_detail() if reason == "reads_changed" else "")
                        return False
                    await load(*round_identity, "edge_reuse", marker)
                    await load(*round_identity, "edge_phase", {
                        "stage_input_digest": _stage_digest(inputs),
                        "model_step_ids": edge["model_step_ids"],
                        "groups": edge["groups"],
                        "reused_from_round": earlier,
                    })
                    log.info("[ingest:edge_reuse] sid=%s round=%d from_round=%d result=hit "
                             "reads=%d saved_steps=%d", task_sid, round_number, earlier,
                             len(records), len(edge["model_step_ids"]))
                    return True
                log.info("[ingest:edge_reuse] sid=%s round=%d result=miss reason=no_earlier_edges",
                         task_sid, round_number)
                return False

            async def stale_before(stage):
                early_started = time.monotonic()
                stale = not await dependencies.validate(
                    VALIDATE_CONCURRENCY, phase="prevalidate")
                log.info("[ingest:parallel_early_validation] sid=%s round=%d "
                         "reads=%d seconds=%.3f stale=%d at=%s %s", task_sid,
                         round_number, len(dependencies.records),
                         time.monotonic() - early_started, int(stale), stage,
                         dependencies.stale_detail())
                return stale

            with model_operation("ingest.graph-round", round_id):
                phase_started = time.monotonic()
                dependencies.current_phase = "nodes"
                nodes, uuid_map, _ = await resolve_nodes_with_candidate_snapshot(
                    view.clients, fresh, episode, previous_episodes, entity_types, **common)
                phase_seconds["nodes"] = time.monotonic() - phase_started
                # The edge stage is the most expensive (about 2.4 calls per
                # observation). A node read already stale here condemns the
                # round, so stop before paying for edges (2026-10-08: ~25% of
                # tasks needed another round).
                if PREVALIDATE and await stale_before("before_edges"):
                    await abandon_stale_round("before_edges", ("resolved_nodes",))
                    continue
                phase_started = time.monotonic()
                reuse_key = edge_reuse_key(
                    episode, fresh, previous_episodes, edge_type_map, group_id,
                    edge_types, nodes, uuid_map, custom_extraction_instructions)
                edge_existed = await load(*round_identity, "edge_phase") is not None
                if EDGE_REUSE and round_number > 0 and not edge_existed:
                    edge_existed = await reuse_earlier_edges(
                        reuse_key, edge_stage_inputs(
                            episode, fresh, previous_episodes, edge_type_map, group_id,
                            edge_types, nodes, uuid_map, custom_extraction_instructions))
                dependencies.current_phase = "edges"
                resolved, invalidated, new = await extract_and_resolve_edges_with_snapshot(
                    view, episode, fresh, previous_episodes, edge_type_map, group_id,
                    edge_types, nodes, uuid_map, custom_extraction_instructions, **common)
                if not edge_existed:
                    # Only an edge stage that really ran here knows its reads.
                    await load(*round_identity, "edge_reuse", {
                        "key": reuse_key, "read_keys": dependencies.phase_keys("edges")})
                phase_seconds["edges"] = time.monotonic() - phase_started
                # Most stale rounds can be identified before the attribute model
                # calls. A false result only aborts this round; the final commit
                # fence still validates every read under the writer lock.
                if PREVALIDATE and await stale_before("before_attributes"):
                    await abandon_stale_round("before_attributes",
                                              ("resolved_nodes", "edge_phase"))
                    continue
                phase_started = time.monotonic()
                dependencies.current_phase = "attributes"
                hydrated = await extract_attributes_with_snapshot(
                    view, nodes, episode, previous_episodes, entity_types, new, **common)
                phase_seconds["attributes"] = time.monotonic() - phase_started
            # The pinned bulk writer otherwise generates missing embeddings
            # inside its write transaction. Finish them before taking the lock.
            phase_started = time.monotonic()
            for node in hydrated:
                if node.name_embedding is None:
                    await node.generate_name_embedding(graphiti.embedder)
            for edge in resolved + invalidated:
                if edge.fact_embedding is None:
                    await edge.generate_embedding(graphiti.embedder)
            phase_seconds["embeddings"] = time.monotonic() - phase_started
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
        prepare_elapsed = time.monotonic() - started
        read_count, read_wall, read_other_wall = dependencies.read_summary("prepare")
        log.info("[ingest:parallel_prepare] sid=%s round=%d fresh=%d "
                 "total=%.3fs nodes=%.3fs edges=%.3fs attributes=%.3fs "
                 "embeddings=%.3fs other=%.3fs graph_reads=%d "
                 "graph_read_wall=%.3fs graph_other_wall=%.3fs",
                 task_sid, round_number, int(bool(phase_seconds)), prepare_elapsed,
                 *(phase_seconds.get(k, 0.0) for k in
                   ("nodes", "edges", "attributes", "embeddings")),
                 max(0.0, prepare_elapsed - sum(phase_seconds.values())),
                 read_count, read_wall, read_other_wall)
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
                         "validate_concurrency=%d prevalidated=1 %s",
                         task_sid, round_number, len(dependencies.records),
                         time.monotonic() - step, VALIDATE_CONCURRENCY,
                         dependencies.stale_detail())
                flow_metrics.record(conflict=True, prevalidated_conflict=True)
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
                             "validate_concurrency=%d %s",
                             task_sid, round_number, len(dependencies.records),
                             acquired-wait_started, time.monotonic()-acquired,
                             steps["validate"], _loop_lag.reading()-lag_at_acquire,
                             VALIDATE_CONCURRENCY, dependencies.stale_detail())
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
            flow_metrics.record(conflict=True, lock_wait_s=acquired-wait_started)
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
        # No new cancellation point after the graph commit: a cancelled caller
        # must never observe a failed request for a successfully written graph.
        flow_metrics.record(conflict=False, lock_wait_s=acquired-wait_started,
                            commit_s=finished-acquired, validate_skipped=skipped)
        return AddEpisodeResults(episode=saved_episode, episodic_edges=episodic_edges,
                                 nodes=hydrated, edges=edges, communities=[], community_edges=[])
    raise GraphReadConflict("graph remained contended after bounded completed rounds")
