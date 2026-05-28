# Bug Report: `graphiti.add_episode` Throughput is Impractical for Documents >5KB

**Project**: [zep-ai/graphiti](https://github.com/getzep/graphiti) (consumed by kg-hub via `graphiti-core`)
**Reporter**: kg-hub maintainer (jingmiao@liblib.ai)
**Date**: 2026-05-28
**Severity**: Functional (not crash) — blocks practical use of `add_episode` for canonical document ingestion
**Affects**: `graphiti-core` ≥ current; FalkorDB backend; Anthropic-compatible LLM client

---

## TL;DR

`graphiti.add_episode` issues **N₁ + N_entities + N_edges + N_dedup** LLM calls per single episode. With:
- a small episode body (1 sentence, 3 entities) → ~5-10 calls → ~10 min on a throttled endpoint
- a moderate body (5KB markdown, 30 entities, 50 edges) → ~80+ calls → ~30-50 min projected
- a large body (49KB markdown) → ~1000+ calls → **5-10 hours projected**

Combined with `max_coroutines=1` (required to avoid HTTP 429 on rate-limited LLM endpoints), this makes
**single-shot ingestion of canonical project docs (DESIGN.md / README.md / RFCs) infeasible**.

The issue is not LLM latency (per-call cost is reasonable). The issue is the **call multiplicity** of
`add_episode`'s internal pipeline.

---

## Environment

| Component | Version / Setting |
|---|---|
| graphiti-core | (pinned in kg-hub `spike-graphiti/.venv`) |
| Graph backend | FalkorDB 8.x via Docker (`kg-hub-falkordb`) |
| LLM client | `AnthropicClient` (with `coding.dashscope.aliyuncs.com/apps/anthropic` base URL) |
| LLM model | `qwen3.6-plus` (Aliyun 百炼 coding plan) |
| Embedder | `FastembedEmbedder` (local `BAAI/bge-small-en-v1.5`, no network) |
| Cross-encoder | `NoOpCrossEncoder` |
| `max_coroutines` | **1** (deliberate — see "Workaround" below) |
| Entity/edge schema | 13 entity types, 13 edge types, custom `EDGE_TYPE_MAP` |

---

## Reproducer

```python
from datetime import datetime, timezone
from graphiti_core.nodes import EpisodeType
from graphiti_client import build_graphiti     # see kg-hub graphiti_client.py
from schema import ENTITY_TYPES, EDGE_TYPES, EDGE_TYPE_MAP

g = await build_graphiti(fresh=False)
# Use any 5KB+ markdown file as body (e.g. an RFC, project README, etc.)
body = open('docs/OBSERVATION-PHASE.md').read()  # 5080 bytes

result = await g.add_episode(
    name="repro-001",
    episode_body=body,
    source=EpisodeType.text,
    source_description="repro-throughput",
    reference_time=datetime.now(tz=timezone.utc),
    group_id="kg_hub",
    entity_types=ENTITY_TYPES,
    edge_types=EDGE_TYPES,
    edge_type_map=EDGE_TYPE_MAP,
)
```

---

## Observed Behavior

Run with `python -u` + httpx DEBUG logging:

### Step 1: `build_graphiti` slow when worker contention exists

```
========== build_graphiti: 192.07s ==========
```

(Expected: <1s after first run, since FastEmbed model is cached.
192s is observed when claude-mem-ingest cron and multiple mcp_server.py processes
share the same FastEmbed/onnxruntime stack.)

### Step 2: `add_episode` issues many serial LLM calls

```
========== body len=5080 chars ==========
[anthropic._base_client] Request options: {'method': 'post', 'url': '/v1/messages',
  'timeout': Timeout(connect=5.0, read=600, write=600, pool=600), ...}
[httpx] HTTP Request: POST .../v1/messages "HTTP/1.1 200 OK"
[httpx]   req-cost-time: 8787 ms
[httpx] HTTP Request: POST .../v1/messages "HTTP/1.1 200 OK"
[httpx]   req-cost-time: 9353 ms
[httpx] HTTP Request: POST .../v1/messages "HTTP/1.1 200 OK"
[httpx]   req-cost-time: 26916 ms
... (continues for ~30-50 min total) ...
```

After 5 minutes, only 3 LLM calls completed. Projected total: ~80+ calls per 5KB doc.

### Step 3: Behavior under load

- LLM endpoint responds normally (all calls return 200, no 429)
- TCP connection to LLM is **transient** — opened, used for one call, closed, re-opened for next
- 0% CPU between calls (everything is in LLM-await)

### Step 4: Trivial body still slow

Even a **single-sentence body** (3 entities, 2 edges) takes ~626s to complete:
```
add_episode trivial: 626.3s nodes=3 edges=2
```

This suggests ~10 LLM calls for the smallest meaningful episode.

---

## Root-Cause Hypothesis

`add_episode` internally appears to call the LLM separately for:

1. Extract candidate entities from episode body — 1 call
2. For each candidate entity (typed):
   - dedup against existing entities — possibly 1 call
   - extract attributes / classify type — 1 call
3. For each pair of entities mentioned together:
   - generate fact text — 1 call
   - classify edge type — possibly 1 call

For 5KB content with ~30 candidate entities and ~50 candidate edges:
30 (dedup) + 30 (attrs) + 50×2 (edges) ≈ 160 calls. At 10s/call ÷ `max_coroutines=1` =
**~27 min minimum, serialized**.

This is the **count multiplicity** problem, not a per-call performance problem.

---

## Workaround We Use

We pin `max_coroutines=1` in `graphiti_client.py`:

```python
g = Graphiti(
    ...,
    # 百炼 coding plan throttles on concurrent LLM calls. Serialize.
    max_coroutines=1,
)
```

If we raise concurrency to e.g. 8, the endpoint returns HTTP 429 within seconds:
```
RateLimitError: Error code: 429 - {'error': {'code': 'throttling',
'message': 'concurrency allocated quota exceeded'}}
```

So we cannot escape the serialization on this LLM endpoint.

---

## Suggested Fixes (priority order)

### 🟢 P0 — Document the throughput expectation

`graphiti-core` README should explicitly state:
> `add_episode` issues N×(entities + edges) LLM calls. Throughput at 1 RPS is approximately 1 entity + 1 edge per second. For documents >2KB or LLM endpoints with concurrency limits, use chunking or batch APIs.

This alone would have saved us **days of misdiagnosis** (we initially thought it was FastEmbed deadlock, rate-limiting, or onnxruntime thread pool — none correct).

### 🟢 P1 — Add an explicit batch / parallel-by-default mode for entity ops

When `max_coroutines > 1`, allow:
- Parallel attribute extraction for entities discovered in the same episode (they don't depend on each other)
- Parallel edge fact generation for non-overlapping entity pairs
- Single-call multi-entity dedup ("for these 30 entities, which ones already exist?")

### 🟡 P2 — Provide a "skip LLM extraction" path

Some upstream users want to register **pre-structured knowledge** (e.g., already-curated capsules, ADRs,
RFCs) as Episodic nodes without paying for LLM extraction. Suggested API:

```python
await g.add_episode(
    ...,
    skip_extraction=True,   # do not run LLM entity/edge extraction
)
```

This would write the episode body verbatim, make it queryable via fulltext, and let consumers
opt in to LLM extraction later. (Current `EpisodeType.text` does extract; `EpisodeType.json` is
structured but not "skip extraction".)

### 🟡 P3 — Cap entity-set per call

Even with `max_coroutines=1`, the per-episode multiplicity grows quadratically with entity count.
Hard cap (e.g., top-N entities by salience) would bound worst-case latency.

---

## Workaround Plan in kg-hub (downstream)

Since the upstream fix is not on our critical path:

1. For **canonical documents** (DESIGN.md, ROADMAP.md, ADRs): use a `direct_canonical_insert.py`
   script that writes `:Episodic` nodes directly via FalkorDB cypher, skipping `add_episode` entirely.
   These docs become retrievable via `kg_episode_search` (FalkorDB fulltext) immediately.
2. For **session-level observations** (claude-mem cron): continue using `add_episode` since
   bodies are small (~500-2000 chars) and per-episode cost is acceptable (~1-3 min).
3. For **large-source ingest** (OpenClaw capsules ≤15KB, occasional reports): chunk by H2 headers
   and ingest sequentially in off-hours.

---

## Asks of the graphiti maintainers

1. Confirm whether the multiplicity is intentional or an artifact of the schema-constrained extraction path
2. Comment on feasibility of P1 (parallel entity ops) and P2 (skip-extraction mode)
3. Document P0 expected throughput so downstream users size accordingly

We're happy to file a more focused issue on a single proposed fix if the team wants.
