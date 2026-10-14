"""Keep Graphiti 0.29.0's contradiction verdicts out of the graph (T-0285).

For every extracted edge, resolve_extracted_edge asks the model which facts the
new one contradicts. The candidates come from a hybrid search over the whole
group (top 10, no shared endpoint or time window required), and the only guard
afterwards is ``old.valid_at < new.valid_at``. Any verdict therefore expires an
edge: either the old fact (invalid_at := new.valid_at) or, when a candidate is
dated later, the new fact itself at birth.

Measured 2026-10-14: 36,963 of 165,317 RELATES_TO edges (22%) were expired this
way. Sampled verdicts mostly paired unrelated facts ("go vet passed" expiring a
description of a function), a third of the expired edges share no node with
anything their invalidating episode extracted, and the rate was the same before
and after edge-dedupe batching (0.213 vs 0.223 expirations per new edge).
Retirement is owned by kg-hub's archived episodes instead (T-0281).

In ``shadow`` mode the verdicts are logged and then removed from the model's
answer before Graphiti reads it, so neither expiry branch runs; duplicate
detection is untouched. Invalid times the timestamp prompt reads from a fact's
own wording are untouched too. ``upstream`` installs nothing.

Independently of the verdicts, a new edge may not become valid after the episode
that reports it: once resolved, its valid_at is clamped to the episode's reference
time. Neither the extraction prompt nor the timestamp prompt enforces that bound,
and the broken episode window (utils/falkordb_episode_window.py) fed the extractor
later timestamps to copy; 23,061 stored edges are dated after their own episode.
Edges resolved to a stored duplicate keep their dates.

This resolver must sit innermost: the batch patches replace the client they
receive, so a wrapper outside them would never see the dedupe answers. install()
therefore refuses to run after them.
"""
from __future__ import annotations

import contextvars
import logging
from importlib.metadata import version

from graphiti_core.utils.datetime_utils import ensure_utc

RESOLVE_PROMPT = "dedupe_edges.resolve_edge"
MODES = ("shadow", "upstream")

log = logging.getLogger("kg_hub.edge_invalidation")
# One client wrapper per underlying client within an episode: the timestamp
# batch refuses to mix clients inside one episode.
_episode_clients: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "edge_invalidation_episode_clients", default=None)
_verdicts: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "edge_invalidation_verdicts", default=None)
_installed = False


def clamp_valid_at(edge, episode) -> bool:
    """Bound an extracted edge's valid_at by its episode's reference time."""
    ref = ensure_utc(getattr(episode, "valid_at", None))
    valid_at = ensure_utc(edge.valid_at)
    if ref is None or valid_at is None or valid_at <= ref:
        return False
    edge.valid_at = episode.valid_at
    return True


class _ShadowClient:
    """Moves contradicted_facts out of dedupe answers into the current edge's record."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def generate_response(self, messages, *args, **kwargs):
        response = await self._client.generate_response(messages, *args, **kwargs)
        record = _verdicts.get()
        if (kwargs.get("prompt_name") == RESOLVE_PROMPT and record is not None
                and isinstance(response, dict) and response.get("contradicted_facts")):
            record.extend(response["contradicted_facts"])
            response = {**response, "contradicted_facts": []}
        return response


def _shadow_client(client):
    cache = _episode_clients.get()
    if cache is None:
        return _ShadowClient(client)
    entry = cache.get(id(client))
    if entry is None:
        entry = cache[id(client)] = (client, _ShadowClient(client))
    return entry[1]


def _resolver_args(args, kwargs):
    names = ("extracted_edge", "related_edges", "existing_edges", "episode")
    values = list(args[:len(names)])
    values += [kwargs.get(name) for name in names[len(values):]]
    return values


def _verdict_uuids(indexes, related, existing):
    related, existing = related or [], existing or []
    out = []
    for idx in indexes:
        if isinstance(idx, int) and 0 <= idx < len(related):
            out.append(related[idx].uuid)
        elif isinstance(idx, int) and 0 <= idx - len(related) < len(existing):
            out.append(existing[idx - len(related)].uuid)
        else:
            out.append(f"idx:{idx}")
    return out


def install(mode: str):
    """Patch the pinned per-edge resolver, once; must precede the batch patches."""
    if mode not in MODES:
        raise ValueError(f"edge invalidation mode must be one of {MODES}, got {mode!r}")
    if mode == "upstream":
        return
    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for edge invalidation shadow")
    global _installed
    if _installed:
        return
    import graphiti_core.graphiti as pipeline
    import graphiti_core.utils.maintenance.edge_operations as ops

    resolve = ops.resolve_extracted_edges
    if (getattr(resolve, "_kg_dedupe_batch", False)
            or getattr(resolve, "_kg_timestamp_batch", False)):
        raise RuntimeError("edge invalidation shadow must be installed before the batch patches")
    original_resolve = resolve
    original_resolve_edge = ops.resolve_extracted_edge

    async def shadow_resolve_edge(llm_client, *args, **kwargs):
        edge, related, existing, episode = _resolver_args(args, kwargs)
        verdicts = []
        token = _verdicts.set(verdicts)
        try:
            result = await original_resolve_edge(_shadow_client(llm_client), *args, **kwargs)
        finally:
            _verdicts.reset(token)
        resolved = result[0]
        episode_uuid = getattr(episode, "uuid", None)
        # Not before resolving: the timestamp prompt dates new edges in there.
        if resolved is edge and clamp_valid_at(edge, episode):
            log.info("[edge_valid_at_clamp] episode=%s edge=%s valid_at=%s",
                     episode_uuid, edge.uuid, edge.valid_at)
        if verdicts:
            log.info("[edge_invalidation_shadow] episode=%s edge=%s contradicted=%s",
                     episode_uuid, resolved.uuid,
                     _verdict_uuids(verdicts, related, existing))
        return result

    async def shadow_resolve(*args, **kwargs):
        token = _episode_clients.set({})
        try:
            return await original_resolve(*args, **kwargs)
        finally:
            _episode_clients.reset(token)

    ops.resolve_extracted_edge = shadow_resolve_edge
    ops.resolve_extracted_edges = shadow_resolve
    pipeline.resolve_extracted_edges = shadow_resolve
    _installed = True
