# GitHub Issue draft — for getzep/graphiti

**Target repo**: https://github.com/getzep/graphiti
**File path**: `getzep/graphiti` → Issues → New issue
**One-click open URL** (paste in browser, edit if needed):

```
https://github.com/getzep/graphiti/issues/new?title=add_episode+is+impractically+slow+for+%3E5KB+content+%E2%80%94+proposal%3A+skip_extraction+parameter&labels=enhancement
```

---

## Title (≤80 chars)

```
add_episode is impractically slow for >5KB content — proposal: skip_extraction parameter
```

## Body

````markdown
## Summary

`Graphiti.add_episode()` issues *N₁ + N_entities + N_edges + N_dedup* serial LLM calls per episode. On a rate-limited LLM endpoint (concurrency-capped Aliyun qwen3.6-plus, where each call costs 8-30s and `max_coroutines` must be set to 1 to avoid HTTP 429), this translates to:

| Episode body | Measured / projected wall time |
|---|---|
| 1 sentence (3 entities) | ~626 s (measured) |
| 5 KB markdown (~30 entities, ~50 edges) | ~30-50 min (projected from partial run) |
| **49 KB markdown** | **~5-10 hr (projected)** |

This makes ingesting **canonical documents** (READMEs, design docs, RFCs, ADRs) via `add_episode` impractical, even though the content is exactly the kind of pre-curated material a graph wants.

## Environment

| Component | Setting |
|---|---|
| graphiti-core | latest |
| Graph backend | FalkorDB 8.x (Docker) |
| LLM client | `AnthropicClient` (Aliyun-compatible) |
| LLM model | `qwen3.6-plus` |
| Embedder | `FastembedEmbedder` (local, `BAAI/bge-small-en-v1.5`) |
| `max_coroutines` | **1** (required for this LLM endpoint) |
| Per-LLM-call latency | 8-30 s (measured, all HTTP 200) |
| Entity / edge schema | 13 entity types, 13 edge types |

## Diagnostic notes

- It is **not** an LLM rate-limit issue — all calls return 200; no 429.
- It is **not** a FastEmbed / onnxruntime deadlock — 10 sequential embeddings take 1 s; 10 parallel take 0.1 s. ONNX worker threads in `WorkerData::SetBlocked` are *idle workers waiting for work*, not deadlocked.
- It is **not** a TCP / network issue — connection to LLM endpoint is intermittent only because each request opens and closes its own connection.
- It **is** call multiplicity × serialization. For a 5 KB doc that produces ~30 entities and ~50 edges, graphiti issues 30 (dedup) + 30 (attribute extraction) + 100 (edge fact + classify) ≈ 160 LLM calls. At 10 s/call × `max_coroutines=1` = **~27 min minimum, serialized**.

## Proposal: opt-in `skip_extraction` parameter

For use cases where the caller wants to register **pre-curated knowledge** (canonical docs / capsules / RFCs) as Episodic nodes *without* paying for LLM extraction:

```python
await g.add_episode(
    ...,
    skip_extraction=True,
)
```

When `True`:
- Build the `EpisodicNode` from `name / episode_body / source_description / reference_time` as today
- Skip `extract_nodes`, `resolve_extracted_nodes`, `_extract_and_resolve_edges`, `extract_attributes_from_nodes`
- Call `_process_episode_data` with `nodes=[]` and `entity_edges=[]` to persist the Episodic node
- Return `AddEpisodeResults` with empty nodes/edges

Patch attached (93-line unified diff): https://github.com/commiao/kg-hub/blob/main/docs/patches/graphiti-skip-extraction.patch

**Measured speedup** on the same 5 KB body: 30+ min (full extraction) → **0.03 s** (`skip_extraction=True`). That's >50,000×, because the LLM critical path is gone entirely.

The Episodic node remains queryable via FalkorDB `db.idx.fulltext.queryNodes('Episodic', ...)` (i.e., `kg_episode_search`-style consumers). Entities/edges from the doc can be opted into later via re-ingestion when the user has reason to want graph navigation over that content.

## Other fix candidates (mentioned for completeness)

1. **Docs**: Add a throughput-expectation note to README (`add_episode` issues N×(entities+edges) LLM calls; cap throughput planning at 1 entity + 1 edge per second / `max_coroutines`).
2. **Parallel entity ops**: Within a single episode, attribute extraction across entities can run in parallel; dedup-by-name can be a single batched query.
3. **Entity cap**: A `max_entities_per_episode` parameter would bound worst-case latency.

I'm happy to file a focused PR for **P1 (`skip_extraction`)** — it's the smallest surface change with the highest practical payoff for users on rate-limited endpoints.

## Reference

Full diagnostic + downstream context (kg-hub project):
- Bug report: https://github.com/commiao/kg-hub/blob/main/docs/BUG-add-episode-throughput.md
- Patch: https://github.com/commiao/kg-hub/blob/main/docs/patches/graphiti-skip-extraction.patch
- README: https://github.com/commiao/kg-hub/blob/main/README.md
````

---

## Labels to apply

- `enhancement`
- `performance` (if present in this repo's labels)

## How to submit (since `gh` CLI isn't authenticated)

1. Open the one-click URL above
2. Verify title and labels
3. Paste the body verbatim
4. Submit
