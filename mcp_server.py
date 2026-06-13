"""
kg-hub MCP server — expose the Graphiti KG to AI tools via MCP stdio.

Tools exposed:
  - kg_search(query)         : natural-language search over edges; returns facts
  - kg_node_neighbors(name)  : find a node by name, return its 1-hop neighbors
  - kg_path_between(a, b)    : try to find a path between two named nodes
  - kg_episode_search(query) : full-text search over raw episodes (capsules etc.)
  - kg_stats()               : node / edge counts for sanity check

Backend: FalkorDB (Docker container `kg-hub-falkordb`).
  - Graphiti FalkorDB schema uses *direct* edges (a:Entity)-[:RELATES_TO]->(b:Entity),
    unlike Kuzu which reified edges as (Entity)-[:RELATES_TO]->(RelatesToNode_)-[:RELATES_TO]->(Entity).
  - All Cypher goes through driver.execute_query() instead of reaching into the
    underlying graph object — keeps us provider-agnostic for future migrations.

Launch (stdio mode, for Claude Code / Cursor / Codex):
    python /Users/mac/workspace_claudeCode/kg-hub/mcp_server.py

Settings.json snippet:
    {
      "mcpServers": {
        "kg-hub": {
          "command": "/Users/mac/workspace_claudeCode/kg-hub/spike-graphiti/.venv/bin/python",
          "args": ["/Users/mac/workspace_claudeCode/kg-hub/mcp_server.py"]
        }
      }
    }
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(Path.home() / ".claude-mem" / ".env", override=True)

import httpx
from mcp.server.fastmcp import FastMCP

from graphiti_client import build_graphiti


# ---------- HTTP client config for write path (Phase 3.B) ----------
# kg_add_episode is a thin wrapper that POSTs to kg_hub_server's /api/ingest.
# This guarantees the SAME auth / idempotency / writer_lock pipeline whether
# the write originates from MCP (this tool) or HTTP curl (OpenClaw / others).
KG_HUB_URL = os.environ.get("KG_HUB_URL", "http://127.0.0.1:8080")
KG_HUB_API_TOKEN = os.environ.get("KG_HUB_API_TOKEN")

# ---------- Client-side unreachable alert (L3 monitoring) ----------
# If kg-hub is unreachable/timing out from where the MCP runs, proactively push a
# Feishu alert. This is the client-vantage watcher (lives off the NAS), complementary
# to the NAS sidecar + VPS probe. Cooldown prevents spam.
import time as _time  # noqa: E402

KG_HUB_FEISHU_WEBHOOK = os.environ.get("KG_HUB_FEISHU_WEBHOOK", "").strip()
_alert_last: dict[str, float] = {}
_ALERT_COOLDOWN_SEC = 600  # at most one alert per kind per 10 min


def _looks_unreachable(exc: Exception) -> bool:
    s = f"{type(exc).__name__}: {exc}".lower()
    return any(k in s for k in (
        "connect", "timeout", "timed out", "refused", "unreachable",
        "cannot assign", "reset by peer", "name or service",
    ))


async def _alert_unreachable(where: str, exc: Exception) -> None:
    if not KG_HUB_FEISHU_WEBHOOK:
        return
    now = _time.time()
    if now - _alert_last.get(where, 0.0) < _ALERT_COOLDOWN_SEC:
        return
    _alert_last[where] = now
    text = (
        f"🔴 kg-hub MCP 连不上\n位置: {where}\nKG_HUB_URL: {KG_HUB_URL}\n"
        f"错误: {type(exc).__name__}: {str(exc)[:200]}"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            await c.post(KG_HUB_FEISHU_WEBHOOK, json={"msg_type": "text", "content": {"text": text}})
    except Exception:
        pass


# ---------- Graphiti singleton, lazily initialized ----------
_graphiti = None
_init_lock = asyncio.Lock()


async def get_graphiti():
    """Lazy-init Graphiti so server starts fast; first call pays the embedder load."""
    global _graphiti
    async with _init_lock:
        if _graphiti is None:
            try:
                _graphiti = await build_graphiti(fresh=False)
            except Exception as exc:
                if _looks_unreachable(exc):
                    await _alert_unreachable("graphiti_init", exc)
                raise
    return _graphiti


async def _query(cypher: str, **params) -> list[dict[str, Any]]:
    """Thin wrapper over driver.execute_query that returns records (list of dicts)."""
    g = await get_graphiti()
    try:
        records, _header, _ = await g.driver.execute_query(cypher, **params)
    except Exception as exc:
        if _looks_unreachable(exc):
            await _alert_unreachable("kg_query", exc)
        raise
    return records or []


# ---------- MCP server ----------
mcp = FastMCP("kg-hub")


@mcp.tool()
async def kg_search(query: str, num_results: int = 10) -> list[dict[str, Any]]:
    """
    Semantic search over the knowledge graph edges (facts).

    Use when you want to answer "what do I know about X" or
    "how did we resolve Y in the past". Returns a list of fact strings
    each tied to a source-target node pair.

    Args:
        query: Natural-language question, in Chinese or English.
        num_results: How many edges to return (default 10, max 30).

    Returns:
        List of {"fact", "source_node", "target_node", "valid_at"} dicts.
    """
    g = await get_graphiti()
    try:
        edges = await g.search(query=query, num_results=min(num_results, 30))
    except Exception as exc:
        if _looks_unreachable(exc):
            await _alert_unreachable("kg_search", exc)
        raise
    out = []
    for e in edges:
        out.append({
            "fact": e.fact,
            "source_node_uuid": str(e.source_node_uuid),
            "target_node_uuid": str(e.target_node_uuid),
            "valid_at": e.valid_at.isoformat() if e.valid_at else None,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })
    return out


@mcp.tool()
async def kg_node_neighbors(name: str, limit: int = 20) -> dict[str, Any]:
    """
    Find an entity by name (fuzzy match) and return its direct neighbors + edge labels.

    Args:
        name: Entity name to look up, e.g. "Cron" or "notify-send.sh".
        limit: Max neighbors to return (default 20).

    Returns:
        {"matched_node": str, "labels": [str], "neighbors": [{"name", "edge", "direction", "fact"}]}
    """
    # exact-or-contains match
    rows = await _query(
        "MATCH (n:Entity) "
        "WHERE n.name = $name OR n.name CONTAINS $name "
        "RETURN n.name AS name, labels(n) AS labels LIMIT 1",
        name=name,
    )
    if not rows:
        return {"matched_node": None, "labels": [], "neighbors": []}

    matched = rows[0]["name"]
    labels = list(rows[0].get("labels") or [])

    out_rows = await _query(
        "MATCH (a:Entity {name: $name})-[e:RELATES_TO]->(b:Entity) "
        "RETURN b.name AS name, e.name AS edge, e.fact AS fact "
        "LIMIT $lim",
        name=matched,
        lim=int(limit),
    )
    in_rows = await _query(
        "MATCH (a:Entity)-[e:RELATES_TO]->(b:Entity {name: $name}) "
        "RETURN a.name AS name, e.name AS edge, e.fact AS fact "
        "LIMIT $lim",
        name=matched,
        lim=int(limit),
    )

    neighbors = (
        [{**r, "direction": "out"} for r in out_rows]
        + [{**r, "direction": "in"} for r in in_rows]
    )
    return {"matched_node": matched, "labels": labels, "neighbors": neighbors}


@mcp.tool()
async def kg_path_between(source: str, target: str, max_hops: int = 4) -> list[list[str]]:
    """
    Find paths between two entities (by name substring match).

    Returns up to 3 paths, each as a list of node names along the path.
    Use when answering "is X related to Y, and how"?

    Args:
        source: Source entity name.
        target: Target entity name.
        max_hops: Maximum path length to consider (default 4).
    """
    hops = max(1, min(int(max_hops), 6))  # clamp to 1..6 to avoid blow-up
    cypher = (
        "MATCH path = (a:Entity)-[:RELATES_TO*1.." + str(hops) + "]->(b:Entity) "
        "WHERE a.name CONTAINS $src AND b.name CONTAINS $tgt "
        "RETURN [n IN nodes(path) | n.name] AS names "
        "LIMIT 3"
    )
    rows = await _query(cypher, src=source, tgt=target)
    return [r["names"] for r in rows if r.get("names")]


@mcp.tool()
async def kg_episode_search(query: str, num_results: int = 5) -> list[dict[str, Any]]:
    """
    Search the raw episodes (capsules / docs as originally ingested).

    Use when kg_search facts are too abstract and you want the original markdown context.

    Args:
        query: Natural-language query.
        num_results: How many episodes to return (max 15).
    """
    lim = max(1, min(int(num_results), 15))
    # FalkorDB fulltext: CALL db.idx.fulltext.queryNodes(<label>, <query>) YIELD node, score
    cypher = (
        "CALL db.idx.fulltext.queryNodes('Episodic', $q) YIELD node, score "
        "RETURN node.name AS name, node.content AS content, "
        "node.source_description AS source, score "
        "ORDER BY score DESC LIMIT " + str(lim)
    )
    try:
        rows = await _query(cypher, q=query)
    except Exception:
        # FalkorDB fulltext query rejects some chars / empty stopword-only queries.
        # Fall back to substring match over name + content.
        rows = await _query(
            "MATCH (n:Episodic) "
            "WHERE n.name CONTAINS $q OR n.content CONTAINS $q "
            "RETURN n.name AS name, n.content AS content, "
            "n.source_description AS source, 0.0 AS score "
            "LIMIT " + str(lim),
            q=query,
        )

    out = []
    for r in rows:
        body = (r.get("content") or "")[:600]
        out.append({
            "name": r.get("name"),
            "source": r.get("source"),
            "score": r.get("score"),
            "body_preview": body,
        })
    return out


@mcp.tool()
async def kg_add_episode(
    content: str,
    source_description: str,
    source_obs_id: str | None = None,
    name: str | None = None,
    reference_time: str | None = None,
) -> dict[str, Any]:
    """
    Write a new episode into the knowledge graph.

    Use this when an AI agent wants to commit information to KG — e.g.
    "save this conversation summary", "record this decision", "remember
    that we fixed X by doing Y". The episode body is processed by
    Graphiti (entity/edge extraction) and merged into the same graph
    that OpenClaw capsules and claude-mem obs live in.

    Idempotency: if (source_description, source_obs_id) was previously
    ingested, returns the existing episode_uuid without re-extracting.

    Args:
        content: Natural-language text. Be specific; Graphiti will extract
            entities and relations from it.
        source_description: Short label identifying who/what is writing.
            Examples: "cursor-manual", "codex-task-summary", "claude-code-decision".
        source_obs_id: Unique-within-source ID for idempotency.
            If omitted, a new UUID is generated (every call becomes unique).
        name: Short episode name (defaults to first 60 chars of content).
        reference_time: ISO 8601 timestamp of when the event happened
            (defaults to now).

    Returns:
        {"status": "ok", "episode_uuid": ..., "nodes": N, "edges": M}
        OR {"status": "skipped", "reason": "duplicate", "episode_uuid": ...}
        OR {"status": "error", "code": ..., "message": ...}
    """
    if not KG_HUB_API_TOKEN:
        return {
            "status": "error",
            "code": "missing_token",
            "message": "KG_HUB_API_TOKEN not set in ~/.claude-mem/.env",
        }

    body = {
        "name": name or (content[:60] + ("…" if len(content) > 60 else "")),
        "episode_body": content,
        "source_description": source_description,
        "reference_time": reference_time
        or datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_obs_id": source_obs_id or str(uuidlib.uuid4()),
    }

    # Client timeout MUST be > server lock-wait (180s) so we don't disconnect
    # while server is still holding for the lock. 240s = 180 + ~60s graphiti work.
    try:
        async with httpx.AsyncClient(timeout=240.0) as client:
            r = await client.post(
                f"{KG_HUB_URL}/api/ingest",
                json=body,
                headers={"Authorization": f"Bearer {KG_HUB_API_TOKEN}"},
            )
        return r.json()
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        await _alert_unreachable("kg_add_episode", exc)
        return {
            "status": "error",
            "code": "server_unreachable",
            "message": f"cannot reach kg_hub_server at {KG_HUB_URL}: {exc}. "
            f"Is it running? (python kg_hub_server.py)",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "code": "request_failed",
            "message": f"{type(exc).__name__}: {exc}",
        }


@mcp.tool()
async def kg_stats() -> dict[str, Any]:
    """
    Quick sanity check: total entity / edge / episode counts and top node types.
    """
    ent_rows = await _query("MATCH (n:Entity) RETURN count(n) AS c")
    edge_rows = await _query(
        "MATCH (a:Entity)-[e:RELATES_TO]->(b:Entity) RETURN count(e) AS c"
    )
    epi_rows = await _query("MATCH (n:Episodic) RETURN count(n) AS c")
    return {
        "entities": ent_rows[0]["c"] if ent_rows else 0,
        "edges": edge_rows[0]["c"] if edge_rows else 0,
        "episodes": epi_rows[0]["c"] if epi_rows else 0,
    }


if __name__ == "__main__":
    mcp.run()
