"""Freeze Graphiti 0.29.0's previous-episode input before any paid call.

Pinned upstream `Graphiti.add_episode` explicitly accepts
`previous_episode_uuids` and reads those episodes when supplied:
https://github.com/getzep/graphiti/blob/v0.29.0/graphiti_core/graphiti.py#L864-L972

This is one stage checkpoint, not a complete continuation adapter. The model
request journal replays exact saved responses, while its manual resume gate
blocks any changed request before the operator-authorized failed step.
"""

from __future__ import annotations

import hashlib
import inspect
import json


def _input_digest(kwargs: dict) -> str:
    values = {name: kwargs[name] for name in
              ("name", "episode_body", "source_description", "reference_time", "group_id")}
    payload = json.dumps(values, default=str, sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def add_episode_with_context_checkpoint(
    graphiti, journal, *, task_sd: str, task_sid: str, operation_id: str,
    relevant_schema_limit: int, **kwargs,
):
    """Call pinned Graphiti with a durable, immutable previous-episode set."""
    if "previous_episode_uuids" not in inspect.signature(graphiti.add_episode).parameters:
        raise RuntimeError("Graphiti add_episode lacks previous_episode_uuids")
    if "previous_episode_uuids" in kwargs:
        raise RuntimeError("previous episode context must come from checkpoint")
    digest = _input_digest(kwargs)
    previous = journal.read_episode_context(task_sd, task_sid, operation_id, digest)
    if previous is None:
        episodes = await graphiti.retrieve_episodes(
            kwargs["reference_time"], last_n=relevant_schema_limit,
            group_ids=[kwargs["group_id"]], source=kwargs["source"])
        previous = journal.save_episode_context(
            task_sd, task_sid, operation_id, digest,
            [str(episode.uuid) for episode in episodes])
    return await graphiti.add_episode(previous_episode_uuids=previous, **kwargs)
