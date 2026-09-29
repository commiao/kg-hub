"""Batch Graphiti 0.29.0's per-edge duplicate/contradiction requests per episode.

resolve_extracted_edge sends one ``dedupe_edges.resolve_edge`` prompt for every
extracted edge that has graph candidates (about 10.6 paid calls per observation
on 2026-09-28). Each upstream prompt is kept verbatim as one self-contained task
inside a single request, and each answer is handed back to the unchanged
upstream resolver, which still owns index validation, invalidation and temporal
rules.

The upstream gather runs at most SEMAPHORE_LIMIT edges at once, which would cap
any coalescing window at that many edges. Only that per-edge gather is widened
while a batch is active; every other paid call made on behalf of those edges
goes through a per-episode limit of the same size.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
from importlib.metadata import version

from graphiti_core.llm_client.client import ModelSize
from graphiti_core.prompts.models import Message
from pydantic import BaseModel, Field

from utils.batch_answer import BatchAnswerMismatch, is_bad_batch_answer, note_fallback

RESOLVE_PROMPT = "dedupe_edges.resolve_edge"
BATCH_PROMPT = "dedupe_edges.resolve_edge_batch"


class _DedupeRow(BaseModel):
    index: int = Field(description="TASK index this answer belongs to")
    duplicate_facts: list[int] = Field(
        description="idx values of duplicate facts, only from that TASK's EXISTING FACTS")
    contradicted_facts: list[int] = Field(
        description="idx values of contradicted facts from that TASK's full idx range")


class _DedupeResponse(BaseModel):
    results: list[_DedupeRow]


_batch: contextvars.ContextVar[_DedupeBatch | None] = contextvars.ContextVar(
    "edge_dedupe_batch", default=None)
_current_edge: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "edge_dedupe_current_edge", default=None)
log = logging.getLogger("kg_hub.edge_dedupe_batch")


def _task_digest(messages: list[Message]) -> str:
    return hashlib.sha256("\x00".join(
        f"{m.role}\x00{m.content}" for m in messages).encode("utf-8")).hexdigest()


def batch_messages(tasks: list[list[Message]]) -> list[Message]:
    body = "\n\n".join(
        f'<TASK index="{i}">\n{messages[-1].content.strip()}\n</TASK>'
        for i, messages in enumerate(tasks))
    return [
        Message(role="system", content=tasks[0][0].content),
        Message(role="user", content=(
            f"Below are {len(tasks)} independent fact deduplication TASKs. Each TASK is "
            "self-contained: its idx values refer only to the lists inside that same "
            "TASK, and its instructions apply exactly as written. Never let one TASK's "
            "facts influence another TASK's answer.\n\n"
            f"{body}\n\n"
            f"Return exactly {len(tasks)} results, one per TASK in TASK order, each with "
            "`index` equal to its TASK index.")),
    ]


class _DedupeBatch:
    def __init__(self, client, *, limit: int, max_items: int, delay: float = 0.015):
        self.client = client
        self.limit = asyncio.Semaphore(max(1, limit))
        self.max_items = max(1, max_items)
        self.delay = delay
        self.pending = []
        self.flush_task = None
        self.batch_requests = 0
        self.batched_edges = 0
        self.expected_edges = None
        self.requested_edges = set()
        self.completed_edges = set()
        self.failure = None
        self.proxy = _DedupeClient(self)

    async def call(self, messages, **kwargs):
        async with self.limit:
            return await self.client.generate_response(messages, **kwargs)

    def set_expected_edges(self, edge_ids):
        if self.expected_edges is not None:
            raise RuntimeError("dedupe batch resolver boundary was set twice")
        self.expected_edges = set(edge_ids)
        self._maybe_flush()

    def edge_finished(self, edge_id):
        if (edge_id in (self.expected_edges or set()) and self.failure is None):
            self.completed_edges.add(edge_id)
            self._maybe_flush()

    def abort(self, exc):
        if self.failure is not None:
            return
        self.failure = exc
        pending, self.pending = self.pending, []
        for _, _, done in pending:
            if not done.done():
                done.set_exception(exc)

    def _maybe_flush(self):
        resolved = self.requested_edges | self.completed_edges
        if (self.expected_edges is not None and self.expected_edges <= resolved
                and self.flush_task is None and self.failure is None):
            self.flush_task = asyncio.create_task(self._flush())

    async def resolve(self, messages, kwargs, edge_id=None):
        if self.failure is not None:
            raise self.failure
        done = asyncio.get_running_loop().create_future()
        self.pending.append((messages, kwargs, done))
        if edge_id is not None:
            self.requested_edges.add(edge_id)
        if self.expected_edges is None:
            if self.flush_task is None:
                self.flush_task = asyncio.create_task(self._flush())
        else:
            self._maybe_flush()
        return await done

    async def _flush(self):
        try:
            if self.expected_edges is None:
                await asyncio.sleep(self.delay)
        except BaseException as exc:
            pending, self.pending = self.pending, []
            self.flush_task = None
            for _, _, done in pending:
                if not done.done():
                    done.set_exception(exc)
            raise
        pending, self.pending = self.pending, []
        self.flush_task = None
        # Content order, not arrival order: the request bytes, and so the
        # durable idempotency key, must be identical when the stage is replayed.
        pending.sort(key=lambda item: _task_digest(item[0]))
        chunks = [pending[start:start + self.max_items]
                  for start in range(0, len(pending), self.max_items)]
        for position, chunk in enumerate(chunks):
            failure = await self._request(chunk)
            if failure is not None:
                # The episode fails as a whole; do not pay for the rest.
                self.failure = failure
                for rest in chunks[position + 1:]:
                    for _, _, done in rest:
                        if not done.done():
                            done.set_exception(failure)
                return

    async def _request(self, chunk) -> Exception | None:
        try:
            if len(chunk) == 1:
                messages, kwargs, _ = chunk[0]
                results = [await self.call(messages, **kwargs)]
            else:
                try:
                    results = await self._batched(chunk)
                except Exception as exc:
                    if not is_bad_batch_answer(exc):
                        raise
                    note_fallback("edge_dedupe", len(chunk), exc)
                    results = await asyncio.gather(*(
                        self.call(messages, **kwargs) for messages, kwargs, _ in chunk))
        except BaseException as exc:
            for _, _, done in chunk:
                if not done.done():
                    done.set_exception(exc)
            if not isinstance(exc, Exception):
                raise
            return exc
        for (_, _, done), result in zip(chunk, results):
            if not done.done():
                done.set_result(result)
        return None

    async def _batched(self, chunk) -> list[dict]:
        response = await self.call(
            batch_messages([messages for messages, _, _ in chunk]),
            response_model=_DedupeResponse,
            model_size=ModelSize.small,
            prompt_name=BATCH_PROMPT,
        )
        rows = _DedupeResponse(**response).results
        if [row.index for row in rows] != list(range(len(chunk))):
            raise BatchAnswerMismatch("edge dedupe batch response identity mismatch")
        self.batch_requests += 1
        self.batched_edges += len(chunk)
        return [{"duplicate_facts": row.duplicate_facts,
                 "contradicted_facts": row.contradicted_facts} for row in rows]


class _DedupeClient:
    """LLM client handed to per-edge resolution while a batch is active."""

    def __init__(self, batch: _DedupeBatch):
        self._batch = batch
        self._kg_model_call_limit_managed = True

    def __getattr__(self, name):
        return getattr(self._batch.client, name)

    async def generate_response(self, messages, response_model=None, max_tokens=None,
                                model_size=ModelSize.medium, group_id=None,
                                prompt_name=None):
        kwargs = {"response_model": response_model, "max_tokens": max_tokens,
                  "model_size": model_size, "group_id": group_id,
                  "prompt_name": prompt_name}
        if prompt_name == RESOLVE_PROMPT:
            return await self._batch.resolve(messages, kwargs, _current_edge.get())
        return await self._batch.call(messages, **kwargs)


def _in_bucket(episode_uuid: str, sample_percent: int) -> bool:
    bucket = int.from_bytes(hashlib.sha256(episode_uuid.encode()).digest()[:4], "big") % 100
    return bucket < sample_percent


def install(sample_percent: int, max_items: int = 12):
    """Patch the pinned per-edge resolver and its gather, once."""
    if not 0 <= sample_percent <= 100:
        raise ValueError("edge dedupe batch sample percent must be 0..100")
    if sample_percent == 0:
        return
    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for edge dedupe batching")
    import graphiti_core.graphiti as pipeline
    import graphiti_core.helpers as helpers
    import graphiti_core.utils.maintenance.edge_operations as ops

    if getattr(ops.resolve_extracted_edges, "_kg_dedupe_batch", False):
        return
    original_resolve = ops.resolve_extracted_edges
    original_resolve_edge = ops.resolve_extracted_edge
    original_gather = ops.semaphore_gather

    async def batched_resolve_edge(llm_client, *args, **kwargs):
        batch = _batch.get()
        if batch is None:
            return await original_resolve_edge(llm_client, *args, **kwargs)
        edge = kwargs.get("extracted_edge") or args[0]
        edge_id = id(edge)
        token = _current_edge.set(edge_id)
        try:
            result = await original_resolve_edge(batch.proxy, *args, **kwargs)
        except BaseException as exc:
            batch.abort(exc)
            raise
        else:
            batch.edge_finished(edge_id)
            return result
        finally:
            _current_edge.reset(token)

    def pending_dedupe_edge_id(coroutine):
        frame = getattr(coroutine, "cr_frame", None)
        if frame is None:
            return None
        local = frame.f_locals
        args = local.get("args", ())
        kwargs = local.get("kwargs", {})
        edge = kwargs.get("extracted_edge") or (args[0] if args else None)
        related = kwargs.get("related_edges")
        if related is None and len(args) > 1:
            related = args[1]
        existing = kwargs.get("existing_edges")
        if existing is None and len(args) > 2:
            existing = args[2]
        if edge is None or not (related or existing):
            return None
        normalized_fact = ops._normalize_string_exact(edge.fact)
        if any(candidate.source_node_uuid == edge.source_node_uuid
               and candidate.target_node_uuid == edge.target_node_uuid
               and ops._normalize_string_exact(candidate.fact) == normalized_fact
               for candidate in (related or [])):
            return None
        return id(edge)

    async def edge_gather(*coroutines, max_coroutines=None):
        batch = _batch.get()
        is_edge_gather = bool(coroutines) and all(
            getattr(c, "__name__", None) == batched_resolve_edge.__name__
            for c in coroutines)
        if batch is not None and is_edge_gather:
            batch.set_expected_edges(filter(None, map(pending_dedupe_edge_id, coroutines)))
            max_coroutines = len(coroutines)
        return await original_gather(*coroutines, max_coroutines=max_coroutines)

    async def batched_resolve(*args, **kwargs):
        episode = kwargs.get("episode") or args[2]
        clients = kwargs.get("clients") or args[0]
        if not _in_bucket(episode.uuid, sample_percent):
            return await original_resolve(*args, **kwargs)
        batch = _DedupeBatch(clients.llm_client, limit=helpers.SEMAPHORE_LIMIT,
                             max_items=max_items)
        token = _batch.set(batch)
        try:
            return await original_resolve(*args, **kwargs)
        finally:
            _batch.reset(token)
            log.info("[edge_dedupe_batch] episode=%s requests=%d edges=%d",
                     episode.uuid, batch.batch_requests, batch.batched_edges)

    batched_resolve._kg_dedupe_batch = True
    ops.resolve_extracted_edge = batched_resolve_edge
    ops.semaphore_gather = edge_gather
    ops.resolve_extracted_edges = batched_resolve
    pipeline.resolve_extracted_edges = batched_resolve
