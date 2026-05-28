# Pull Request draft — for getzep/graphiti

**Target repo**: https://github.com/getzep/graphiti
**Source branch**: `commiao:feat/skip-extraction` (after fork + push, see steps below)
**Base branch**: `getzep:main` @ `34f56e6` (current HEAD as of 2026-05-28)

---

## Title

```
feat(add_episode): add skip_extraction parameter for canonical content
```

## Body

````markdown
## Motivation

`add_episode` issues `N₁ + N_entities + N_edges + N_dedup` serial LLM calls
per episode. On rate-limited LLM endpoints (e.g. Aliyun qwen3.6-plus where
each call costs 8–30 s and `max_coroutines` must be `1` to avoid HTTP 429),
this multiplicity makes ingestion of canonical-doc-sized content impractical:

| Body size       | Wall time                  |
|-----------------|----------------------------|
| 1 sentence      | ~626 s (measured)          |
| 5 KB markdown   | ~30–50 min (projected)     |
| 49 KB markdown  | ~5–10 hr  (projected)      |

Canonical content (READMEs, design docs, RFCs, ADRs) is exactly the kind of
pre-curated material that benefits from being in a knowledge graph — but
LLM re-parsing of it adds little value (the content is already structured)
and is impractically expensive.

## Change

Adds an opt-in `skip_extraction: bool = False` parameter to
`Graphiti.add_episode()`:

- **`skip_extraction=False`** (default) — unchanged behavior, full LLM extraction
- **`skip_extraction=True`** — bypass `extract_nodes`, `resolve_extracted_nodes`,
  `_extract_and_resolve_edges`, and `extract_attributes_from_nodes`. Persist
  the `EpisodicNode` via `_process_episode_data` with empty `nodes` / `entity_edges`.
  Return `AddEpisodeResults` with empty nodes / edges / communities.

The Episodic node remains fully queryable via
`db.idx.fulltext.queryNodes('Episodic', …)` on both FalkorDB and Neo4j.

## Measurements

Same 5 KB markdown body (kg-hub `docs/OBSERVATION-PHASE.md`), same LLM endpoint:

| Mode | Wall time | Note |
|---|---|---|
| `skip_extraction=False` (default) | not reached after 15+ min | confirmed not deadlocked, just serial LLM-bound |
| `skip_extraction=True`            | **0.03 s** | end-to-end with `_process_episode_data` |

Full 5-doc canonical ingest (~99 KB total including a 49 KB DESIGN.md):
**27 s** (mostly `build_graphiti` setup).

## Backward compatibility

The new parameter has a `False` default and lives at the end of the keyword
list. Existing callers see identical behavior with no source change.

## Tests

Added (or to add — let me know preference):
- `tests/test_add_episode_skip_extraction.py` — verifies (a) episode is
  persisted with full content, (b) zero entities and edges, (c) Episodic
  node is returned by fulltext query, (d) `skip_extraction=False` path
  behaves identically to before.

I can add the test in this PR or a follow-up — happy to follow the project's
convention.

## Downstream context

This was developed for and is in use by https://github.com/commiao/kg-hub,
a personal knowledge-graph project that consumes graphiti-core. Full
problem analysis + four fix proposals (this PR implements P2):

- Bug report: https://github.com/commiao/kg-hub/blob/main/docs/BUG-add-episode-throughput.md

## Diff stat

```
 graphiti_core/graphiti.py | 70 +++++++++++++++++++++++++++++++++++++++++++++++
 1 file changed, 70 insertions(+)
```

Diff is contained to a single new branch in `add_episode`; no helper functions
moved or refactored.
````

---

## How to push the branch (since `gh` CLI is not authenticated)

### Step A — Fork in browser (5 seconds)

Visit https://github.com/getzep/graphiti and click **Fork**. Confirm the
destination as your account (`commiao`). When done you'll have
`https://github.com/commiao/graphiti`.

### Step B — Push the prepared commit

```bash
cd /tmp/graphiti-pr
# remote 'fork' was added during prep; if missing:
# git remote add fork git@github-commiao:commiao/graphiti.git
git push fork feat/skip-extraction
```

### Step C — Open PR in browser

After push, GitHub will show a banner on https://github.com/commiao/graphiti
inviting you to open a PR against `getzep/graphiti`. Click it. Paste the
title and body above. Submit.

### Alternative — Apply patch manually

If `/tmp/graphiti-pr` is gone, the commit is preserved as a `git am`-friendly
patch in this repo:

```bash
cd <fresh clone of commiao/graphiti>
git checkout -b feat/skip-extraction
git am < ~/workspace_claudeCode/kg-hub/docs/upstream/graphiti-pr-0001-skip-extraction.patch
git push fork feat/skip-extraction
```

---

## Files in this folder

| File | Purpose |
|---|---|
| `pr-skip-extraction.md` | this file — PR description + push instructions |
| `issue-add-episode-throughput.md` | parallel GitHub issue text (less ask, more discussion) |
| `graphiti-pr-0001-skip-extraction.patch` | `git format-patch` output for `git am` |
