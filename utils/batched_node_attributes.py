"""Batch Graphiti 0.29 entity attributes without changing summaries or embeddings.

Upstream makes one paid call per typed entity. All those calls read the same
episode. A bounded structured response can return the same per-entity schemas
in one call; required ordinal keys keep duplicate names and mixed types apart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from pydantic import ConfigDict, Field, ValidationError, create_model
from graphiti_core.llm_client.client import ModelSize
from graphiti_core.prompts.models import Message
from graphiti_core.utils.maintenance import node_operations as upstream

from utils.batch_answer import note_fallback

_original = upstream.extract_attributes_from_nodes
BATCH_SIZE = 8
logger = logging.getLogger("kg_hub.attributes")


async def extract_attributes_from_nodes(
    clients, nodes, episode=None, previous_episodes=None, entity_types=None,
    should_summarize_node=None, edges=None, skip_fact_appending=False,
    include_type_descriptions=False,
):
    # Preserve the less common upstream mode whose summary prompt uses schemas.
    if include_type_descriptions or len({node.group_id for node in nodes}) > 1:
        return await _original(
            clients, nodes, episode, previous_episodes, entity_types,
            should_summarize_node, edges, skip_fact_appending,
            include_type_descriptions,
        )
    typed = []
    for node in nodes:
        label = next((label for label in node.labels if label != "Entity"), "")
        schema = (entity_types or {}).get(label)
        if schema is not None and schema.model_fields:
            typed.append((node, schema))

    # Validate all batches before mutating any node. An invalid batch answer
    # keeps its valid entities; only the rest are re-asked one entity each.
    updates = []
    started = time.monotonic()
    for offset in range(0, len(typed), BATCH_SIZE):
        batch = typed[offset:offset + BATCH_SIZE]
        fields = {
            f"entity_{i}": (schema, Field(..., description=f"Attributes of {node.name}"))
            for i, (node, schema) in enumerate(batch)
        }
        response_model = create_model(
            "EntityAttributeBatch", __config__=ConfigDict(extra="forbid"), **fields
        )
        context = upstream._build_episode_context({}, episode, previous_episodes)
        entities = {
            f"entity_{i}": {"name": node.name, "entity_types": node.labels,
                            "attributes": node.attributes}
            for i, (node, _) in enumerate(batch)
        }
        try:
            response = await clients.llm_client.generate_response(
                [Message(role="system", content=(
                    "You are an entity attribute extraction specialist. NEVER hallucinate "
                    "or infer values not explicitly stated. Update each entity independently "
                    "using only the supplied messages and its existing attributes. Return "
                    "every entity key with attributes matching its schema; never transfer "
                    "attributes between entities. Unknown attributes remain null."
                )), Message(role="user", content=json.dumps({
                    "previous_episodes": context["previous_episodes"],
                    "episode_content": context["episode_content"], "entities": entities,
                }, ensure_ascii=False))],
                response_model=response_model, model_size=ModelSize.small,
                group_id=batch[0][0].group_id,
                prompt_name="extract_nodes.extract_attributes_batch",
            )
            validated = response_model.model_validate(response)
        except ValidationError as exc:
            note_fallback("node_attributes", len(batch), exc)
            updates.extend(await _salvage(clients, batch, _rejected_payload(exc),
                                          episode, previous_episodes))
            continue
        updates.extend((node, getattr(validated, f"entity_{i}").model_dump())
                       for i, (node, _) in enumerate(batch))
    for node, attributes in updates:
        node.attributes.update(attributes)
    if typed:
        logger.info("[attributes:batch] entities=%d calls=%d elapsed=%.1fs",
                    len(typed), (len(typed) + BATCH_SIZE - 1) // BATCH_SIZE,
                    time.monotonic() - started)

    # No schemas means upstream skips ONLY attribute calls. Its existing batch
    # summaries, edge-fact append policy and embeddings still run unchanged.
    return await _original(
        clients, nodes, episode, previous_episodes, None,
        should_summarize_node, edges, skip_fact_appending, False,
    )


def _rejected_payload(exc: ValidationError) -> dict:
    """The top-level object the batch model rejected, if pydantic kept it."""
    for error in exc.errors():
        if len(error.get("loc", ())) == 1 and error.get("type") == "missing":
            if isinstance(error.get("input"), dict):
                return error["input"]
    return {}


async def _salvage(clients, batch, payload, episode, previous_episodes):
    names = [node.name for node, _ in batch]
    answers, missing = {}, []
    for i, (node, schema) in enumerate(batch):
        value = payload.get(f"entity_{i}")
        # Name keys are unambiguous only when no other entity shares the name.
        if value is None and names.count(node.name) == 1:
            value = payload.get(node.name)
        try:
            answers[i] = schema.model_validate(value).model_dump()
        except ValidationError:
            missing.append(i)
    retried = await asyncio.gather(*(
        upstream._extract_entity_attributes(
            clients.llm_client, batch[i][0], episode, previous_episodes, batch[i][1])
        for i in missing))
    answers.update(zip(missing, retried))
    logger.info("[attributes:salvage] entities=%d kept=%d retried=%d",
                len(batch), len(batch) - len(missing), len(missing))
    return [(node, answers[i]) for i, (node, _) in enumerate(batch)]
