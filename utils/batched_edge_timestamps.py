"""Bound Graphiti timestamp extraction calls by resolving small edge batches."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import logging
import os
from typing import Any

from pydantic import BaseModel, Field
from graphiti_core.llm_client.client import ModelSize
from graphiti_core.prompts.models import Message


BATCH_SIZE = 8
PROMPT_NAME = "extract_edges.extract_timestamps_batch"


class EdgeTimestampItem(BaseModel):
    index: int = Field(ge=0)
    valid_at: str | None = None
    invalid_at: str | None = None


class EdgeTimestampBatch(BaseModel):
    items: list[EdgeTimestampItem] = Field(default_factory=list)


def _messages(edges: list[Any], reference_time: str) -> list[Message]:
    facts = [{"index": i, "fact": edge.fact} for i, edge in enumerate(edges)]
    return [
        Message(role="system", content="You extract temporal bounds from facts. NEVER hallucinate dates."),
        Message(
            role="user",
            content=(
                "Given a REFERENCE TIME and a numbered list of FACTS, determine for each "
                "fact when it became true (valid_at) and when it stopped being true "
                "(invalid_at).\n\n"
                "Rules:\n"
                "- Resolve relative expressions using the REFERENCE TIME.\n"
                "- If a fact is ongoing (present tense), set valid_at to REFERENCE TIME.\n"
                "- If a change or end is expressed, set invalid_at to the relevant time.\n"
                "- Leave both null if no time is stated or resolvable.\n"
                "- If only a date is mentioned, assume 00:00:00.\n"
                "- Use ISO 8601 with Z suffix.\n"
                "- Do NOT hallucinate or infer dates from unrelated events.\n"
                "- Return exactly one item for every input index, without changing indices.\n\n"
                f"REFERENCE TIME:\n{reference_time}\n\n"
                "FACTS JSON:\n"
                f"{json.dumps(facts, ensure_ascii=False, separators=(',', ':'))}"
            ),
        ),
    ]


def install_batched_edge_timestamp_resolver(pipeline_module, edge_operations_module) -> None:
    """Install one durable, indexed timestamp request per up to eight new facts.

    The operation fails closed on a malformed or failed batch. Falling back to
    new per-edge requests after an uncertain paid batch could duplicate spend.
    """
    original_resolver = pipeline_module.resolve_extracted_edges
    original_timestamp_extractor = edge_operations_module._extract_edge_timestamps
    if getattr(original_resolver, "__kg_hub_batched_edge_timestamps__", False):
        return
    if getattr(original_timestamp_extractor, "__kg_hub_timestamp_dispatch__", False):
        raise RuntimeError("batched timestamp resolver installation drift")

    batched_edge_ids: ContextVar[frozenset[int]] = ContextVar(
        "kg_hub_batched_edge_timestamp_ids", default=frozenset()
    )
    logger = logging.getLogger("kg_hub.edge_timestamp_batch")

    async def timestamp_dispatch(llm_client, edge, episode):
        if id(edge) in batched_edge_ids.get():
            return None
        return await original_timestamp_extractor(llm_client, edge, episode)

    timestamp_dispatch.__kg_hub_timestamp_dispatch__ = True
    edge_operations_module._extract_edge_timestamps = timestamp_dispatch

    async def batched_resolver(clients, extracted_edges, episode, *args, **kwargs):
        if episode is None or episode.valid_at is None:
            return await original_resolver(clients, extracted_edges, episode, *args, **kwargs)

        pending = [
            edge for edge in extracted_edges
            if edge.valid_at is None and edge.invalid_at is None
        ]
        if len(pending) < 2:
            return await original_resolver(clients, extracted_edges, episode, *args, **kwargs)

        chunks = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]

        async def extract_chunk(chunk):
            raw = await clients.llm_client.generate_response(
                _messages(chunk, episode.valid_at.isoformat()),
                response_model=EdgeTimestampBatch,
                model_size=ModelSize.small,
                prompt_name=PROMPT_NAME,
            )
            batch = EdgeTimestampBatch.model_validate(raw)
            expected = set(range(len(chunk)))
            indices = [item.index for item in batch.items]
            if len(indices) != len(chunk) or set(indices) != expected:
                raise ValueError("batched edge timestamp indices incomplete")
            by_index = {item.index: item for item in batch.items}
            parsed = []
            for index in range(len(chunk)):
                item = by_index[index]
                valid_at = _parse_datetime(item.valid_at, edge_operations_module.ensure_utc)
                invalid_at = _parse_datetime(item.invalid_at, edge_operations_module.ensure_utc)
                parsed.append((chunk[index], valid_at, invalid_at))
            return parsed

        try:
            configured = max(1, min(8, int(os.environ.get("SEMAPHORE_LIMIT", "1"))))
        except (TypeError, ValueError):
            configured = 1
        semaphore = asyncio.Semaphore(configured)

        async def bounded(chunk):
            async with semaphore:
                return await extract_chunk(chunk)

        # Keep the same per-episode model-call concurrency Graphiti already uses.
        try:
            results = await asyncio.gather(*(bounded(chunk) for chunk in chunks))
        except Exception as exc:
            logger.warning(
                "Batched edge timestamp extraction failed closed (%d facts, %s)",
                len(pending), type(exc).__name__,
            )
            raise

        skipped_ids: set[int] = set()
        for result in results:
            for edge, valid_at, invalid_at in result:
                edge.valid_at = valid_at
                edge.invalid_at = invalid_at
                skipped_ids.add(id(edge))

        token = batched_edge_ids.set(frozenset(skipped_ids))
        try:
            return await original_resolver(clients, extracted_edges, episode, *args, **kwargs)
        finally:
            batched_edge_ids.reset(token)

    batched_resolver.__kg_hub_batched_edge_timestamps__ = True
    pipeline_module.resolve_extracted_edges = batched_resolver


def _parse_datetime(value: str | None, ensure_utc):
    if value is None or not value.strip():
        return None
    from datetime import datetime

    try:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise ValueError("batched edge timestamp is not ISO 8601") from exc
