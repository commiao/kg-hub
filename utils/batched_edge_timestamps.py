"""Batch Graphiti 0.29.0's per-edge temporal extraction within one episode.

The pinned Graphiti release already ships BatchEdgeTimestamps and its prompt,
but resolve_extracted_edge still calls the single-fact prompt for every edge.
Keep the upstream resolver (including duplicate/invalidation behavior) intact.
Only coalesce timestamp requests which it actually makes.
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
    def __init__(self, original, *, delay: float = 0.015):
        self.original = original
        self.delay = delay
        self.pending = []
        self.flush_task = None
        self.batch_requests = 0
        self.batched_edges = 0

    async def extract(self, llm_client, edge, episode):
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        self.pending.append((llm_client, edge, episode, done))
        if self.flush_task is None:
            self.flush_task = asyncio.create_task(self._flush())
        return await done

    async def _flush(self):
        try:
            await asyncio.sleep(self.delay)
        except BaseException as exc:
            pending, self.pending = self.pending, []
            self.flush_task = None
            for _, _, _, done in pending:
                if not done.done():
                    done.set_exception(exc)
            raise
        pending, self.pending = self.pending, []
        self.flush_task = None
        try:
            if len(pending) == 1:
                client, edge, episode, _ = pending[0]
                await self.original(client, edge, episode)
            else:
                clients = {id(item[0]) for item in pending}
                episodes = {item[2].uuid for item in pending}
                if len(clients) != 1 or len(episodes) != 1:
                    raise RuntimeError("timestamp batch mixed clients or episodes")
                facts = [
                    {"index": index, "fact": edge.fact,
                     "reference_time": episode.valid_at.isoformat()}
                    for index, (_, edge, episode, _) in enumerate(pending)
                ]
                response = await pending[0][0].generate_response(
                    prompt_library.extract_edges.extract_timestamps_batch({"facts": facts}),
                    response_model=_TimestampResponse,
                    model_size=ModelSize.small,
                    prompt_name="extract_edges.extract_timestamps_batch",
                )
                values = _TimestampResponse(**response).timestamps
                if [value.index for value in values] != list(range(len(pending))):
                    raise RuntimeError("timestamp batch response identity mismatch")
                parsed = [(_parse_timestamp(value.valid_at),
                           _parse_timestamp(value.invalid_at)) for value in values]
                self.batch_requests += 1
                self.batched_edges += len(pending)
                # Validate the entire response before mutating any graph object.
                for (_, edge, _, _), (valid_at, invalid_at) in zip(pending, parsed):
                    if valid_at is not None:
                        edge.valid_at = valid_at
                    if invalid_at is not None:
                        edge.invalid_at = invalid_at
        except BaseException as exc:
            for _, _, _, done in pending:
                if not done.done():
                    done.set_exception(exc)
        else:
            for _, _, _, done in pending:
                if not done.done():
                    done.set_result(None)


def install(sample_percent: int):
    """Patch only the pinned resolver's temporal call boundary, once."""
    if not 0 <= sample_percent <= 100:
        raise ValueError("timestamp batch sample percent must be 0..100")
    if sample_percent == 0:
        return
    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for timestamp batching")
    import graphiti_core.graphiti as pipeline
    import graphiti_core.utils.maintenance.edge_operations as ops

    if getattr(ops.resolve_extracted_edges, "_kg_timestamp_batch", False):
        return
    original_resolve = ops.resolve_extracted_edges
    original_extract = ops._extract_edge_timestamps

    async def batched_extract(llm_client, edge, episode):
        batch = _batch.get()
        if batch is None:
            return await original_extract(llm_client, edge, episode)
        return await batch.extract(llm_client, edge, episode)

    async def batched_resolve(*args, **kwargs):
        episode = kwargs.get("episode") or args[2]
        bucket = int.from_bytes(hashlib.sha256(episode.uuid.encode()).digest()[:4], "big") % 100
        if bucket >= sample_percent:
            return await original_resolve(*args, **kwargs)
        batch = _TimestampBatch(original_extract)
        token = _batch.set(batch)
        try:
            return await original_resolve(*args, **kwargs)
        finally:
            _batch.reset(token)
            log.info("[timestamp_batch] episode=%s requests=%d edges=%d",
                     episode.uuid, batch.batch_requests, batch.batched_edges)

    batched_resolve._kg_timestamp_batch = True
    ops._extract_edge_timestamps = batched_extract
    ops.resolve_extracted_edges = batched_resolve
    pipeline.resolve_extracted_edges = batched_resolve
