"""Batch Graphiti 0.29.0's per-edge temporal extraction within one episode.

The pinned Graphiti release already ships BatchEdgeTimestamps and its prompt,
but resolve_extracted_edge still calls the single-fact prompt for every edge.
Keep the upstream resolver (including duplicate/invalidation behavior) intact.
Only coalesce timestamp requests which it actually makes, in bounded groups.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
from datetime import datetime
from importlib.metadata import version

from graphiti_core.llm_client.client import ModelSize
from graphiti_core.prompts import prompt_library
from graphiti_core.utils.datetime_utils import ensure_utc
from pydantic import BaseModel, Field

from utils.batch_answer import BatchAnswerMismatch, is_bad_batch_answer, note_fallback


class _TimestampRow(BaseModel):
    index: int = Field(description="Zero-based index of the input fact")
    valid_at: str | None = None
    invalid_at: str | None = None


class _TimestampResponse(BaseModel):
    timestamps: list[_TimestampRow]


_batch: contextvars.ContextVar[_TimestampBatch | None] = contextvars.ContextVar(
    "edge_timestamp_batch", default=None)
log = logging.getLogger("kg_hub.edge_timestamp_batch")


def _parse_timestamp(value: str | None):
    if not value:
        return None
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


class _TimestampBatch:
    def __init__(self, original, *, max_items: int = 12, max_calls: int = 2):
        self.original = original
        self.max_items = max(1, max_items)
        self.call_limit = asyncio.Semaphore(max(1, max_calls))
        self.pending = []
        self.flush_task = None
        self.batch_requests = 0
        self.batched_edges = 0
        self.expected_edges = None
        self.accounted_edges = set()
        self.limited_clients = {}
        self.failure = None

    def set_expected_edges(self, count: int):
        if self.expected_edges is not None:
            raise RuntimeError("timestamp batch resolver boundary was set twice")
        self.expected_edges = count
        self._maybe_flush()

    def edge_finished(self, edge):
        if self.failure is not None:
            return
        self.accounted_edges.add(id(edge))
        self._maybe_flush()

    def abort(self, exc):
        if self.failure is not None:
            return
        self.failure = exc
        pending, self.pending = self.pending, []
        for _, _, _, done in pending:
            if not done.done():
                done.set_exception(exc)

    def client_for(self, client):
        if getattr(client, "_kg_model_call_limit_managed", False):
            return client
        key = id(client)
        if key not in self.limited_clients:
            self.limited_clients[key] = _TimestampLimitedClient(client, self)
        return self.limited_clients[key]

    def _maybe_flush(self):
        if (self.expected_edges is not None
                and len(self.accounted_edges) == self.expected_edges
                and self.flush_task is None and self.failure is None):
            self.flush_task = asyncio.create_task(self._flush())

    async def extract(self, llm_client, edge, episode):
        if self.failure is not None:
            raise self.failure
        if self.expected_edges is None:
            raise RuntimeError("timestamp batch has no deterministic episode boundary")
        edge_id = id(edge)
        if edge_id in self.accounted_edges:
            raise RuntimeError("timestamp batch received an edge more than once")
        self.accounted_edges.add(edge_id)
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        self.pending.append((llm_client, edge, episode, done))
        self._maybe_flush()
        return await done

    async def _flush(self):
        if self.failure is not None:
            return
        pending, self.pending = self.pending, []
        # Wait for the complete edge resolver set, then sort and chunk it. This
        # keeps batch membership and gateway idempotency keys stable on replay.
        pending.sort(key=lambda item: (
            item[1].fact, item[1].source_node_uuid, item[1].target_node_uuid))
        if not pending:
            return
        for start in range(0, len(pending), self.max_items):
            if self.failure is not None:
                for _, _, _, done in pending[start:]:
                    if not done.done():
                        done.set_exception(self.failure)
                return
            chunk = pending[start:start + self.max_items]
            try:
                if len(chunk) == 1:
                    client, edge, episode, _ = chunk[0]
                    await self.original(client, edge, episode)
                else:
                    try:
                        await self._batched(chunk)
                    except Exception as exc:
                        if not is_bad_batch_answer(exc):
                            raise
                        note_fallback("edge_timestamps", len(chunk), exc)
                        await asyncio.gather(*(
                            self.original(client, edge, episode)
                            for client, edge, episode, _ in chunk))
            except BaseException as exc:
                self.failure = exc
                for _, _, _, done in chunk:
                    if not done.done():
                        done.set_exception(exc)
                for _, _, _, done in pending[start + len(chunk):]:
                    if not done.done():
                        done.set_exception(exc)
                if not isinstance(exc, Exception):
                    raise
                return
            else:
                for _, _, _, done in chunk:
                    if not done.done():
                        done.set_result(None)

    async def _batched(self, chunk):
        clients = {id(item[0]) for item in chunk}
        episodes = {item[2].uuid for item in chunk}
        if len(clients) != 1 or len(episodes) != 1:
            raise RuntimeError("timestamp batch mixed clients or episodes")
        facts = [
            {"index": index, "fact": edge.fact,
             "reference_time": episode.valid_at.isoformat()}
            for index, (_, edge, episode, _) in enumerate(chunk)
        ]
        response = await chunk[0][0].generate_response(
            prompt_library.extract_edges.extract_timestamps_batch({"facts": facts}),
            response_model=_TimestampResponse,
            model_size=ModelSize.small,
            prompt_name="extract_edges.extract_timestamps_batch",
        )
        values = _TimestampResponse(**response).timestamps
        if [value.index for value in values] != list(range(len(chunk))):
            raise BatchAnswerMismatch("timestamp batch response identity mismatch")
        # Validate the entire response before mutating any graph object.
        parsed = [(_parse_timestamp(value.valid_at),
                   _parse_timestamp(value.invalid_at)) for value in values]
        for (_, edge, _, _), (valid_at, invalid_at) in zip(chunk, parsed):
            if valid_at is not None:
                edge.valid_at = valid_at
            if invalid_at is not None:
                edge.invalid_at = invalid_at
        self.batch_requests += 1
        self.batched_edges += len(chunk)


class _TimestampLimitedClient:
    """Bound per-edge LLM calls when timestamp batching widens the edge gather."""

    def __init__(self, client, batch: _TimestampBatch):
        self.client = client
        self.batch = batch

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def generate_response(self, *args, **kwargs):
        async with self.batch.call_limit:
            return await self.client.generate_response(*args, **kwargs)


def install(sample_percent: int):
    """Patch only the pinned resolver's temporal call boundary, once."""
    if not 0 <= sample_percent <= 100:
        raise ValueError("timestamp batch sample percent must be 0..100")
    if sample_percent == 0:
        return
    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for timestamp batching")
    import graphiti_core.graphiti as pipeline
    import graphiti_core.helpers as helpers
    import graphiti_core.utils.maintenance.edge_operations as ops

    if getattr(ops.resolve_extracted_edges, "_kg_timestamp_batch", False):
        return
    original_resolve = ops.resolve_extracted_edges
    original_resolve_edge = ops.resolve_extracted_edge
    original_extract = ops._extract_edge_timestamps
    original_gather = ops.semaphore_gather

    async def batched_resolve_edge(llm_client, *args, **kwargs):
        batch = _batch.get()
        if batch is None:
            return await original_resolve_edge(llm_client, *args, **kwargs)
        edge = kwargs.get("extracted_edge") or args[0]
        # The dedupe proxy already bounds all paid calls made by this resolver.
        # When timestamp batching runs alone, enforce the same cap ourselves.
        client = batch.client_for(llm_client)
        try:
            result = await original_resolve_edge(client, *args, **kwargs)
        except BaseException as exc:
            batch.abort(exc)
            raise
        else:
            batch.edge_finished(edge)
            return result

    async def batched_gather(*coroutines, max_coroutines=None):
        batch = _batch.get()
        resolver_calls = bool(coroutines) and all(
            getattr(getattr(coro, "cr_code", None), "co_name", None)
            == "batched_resolve_edge"
            for coro in coroutines)
        if batch is not None and resolver_calls:
            batch.set_expected_edges(len(coroutines))
            max_coroutines = len(coroutines)
        return await original_gather(*coroutines, max_coroutines=max_coroutines)

    async def batched_extract(llm_client, edge, episode):
        batch = _batch.get()
        if batch is None:
            return await original_extract(llm_client, edge, episode)
        if (edge.valid_at is not None or edge.invalid_at is not None
                or episode is None or episode.valid_at is None):
            return await original_extract(llm_client, edge, episode)
        return await batch.extract(llm_client, edge, episode)

    async def batched_resolve(*args, **kwargs):
        episode = kwargs.get("episode") or args[2]
        bucket = int.from_bytes(hashlib.sha256(episode.uuid.encode()).digest()[:4], "big") % 100
        if bucket >= sample_percent:
            return await original_resolve(*args, **kwargs)
        batch = _TimestampBatch(original_extract, max_calls=helpers.SEMAPHORE_LIMIT)
        token = _batch.set(batch)
        try:
            return await original_resolve(*args, **kwargs)
        finally:
            _batch.reset(token)
            log.info("[timestamp_batch] episode=%s requests=%d edges=%d",
                     episode.uuid, batch.batch_requests, batch.batched_edges)

    batched_resolve._kg_timestamp_batch = True
    ops._extract_edge_timestamps = batched_extract
    ops.resolve_extracted_edge = batched_resolve_edge
    ops.semaphore_gather = batched_gather
    ops.resolve_extracted_edges = batched_resolve
    pipeline.resolve_extracted_edges = batched_resolve
