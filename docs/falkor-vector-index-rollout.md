# Broad edge vector search rollout

The optimistic ingest path queries `RELATES_TO.fact_embedding` twice per extracted
edge. The UUID-filtered dedupe query remains exact. The broad invalidation
query can use a vector index for candidate retrieval, followed by Graphiti's
original exact cosine formula on those candidates.

## Evidence and semantic boundary

On 2026-09-30, the `1c4820b` timing build measured broad edge queries taking
roughly 5 seconds during prepare and 9 seconds during concurrent prevalidation
in the initial production sample. Entity name queries were about 0.26 seconds
in prepare. These figures include live contention; the isolated evaluation
also competed with production for FalkorDB CPU, so they are not a clean
throughput baseline.

A copy of the live graph (`kg_hub_vector_eval_20260930`: 52,582 nodes and
213,397 relationships) was indexed with 384-dimensional cosine indexes.
Fifty recent real read-set vectors were compared on that *same snapshot*:

| Query | Complete top-result set matches | Exact order matches | Mean query time |
|---|---:|---:|---:|
| Original exact scan | reference | reference | 2.865 s |
| Index, 1024 candidates, exact rerank | 50/50 | 47/50 | 0.480 s |

Index `score` is a **distance**, so the indexed query reranks candidates with
`(2 - vec.cosineDistance(...))/2` and Graphiti's original `min_score` cutoff.
The three order differences are a residual semantic risk; the candidate *sets*
matched, but this sample does not prove future queries will always match.
At the time of the snapshot all 88,730 `RELATES_TO` relationships belonged to
`kg_hub`. The canary routes only this group and only unfiltered ingest queries.

## Rollout and rollback

Create the index on the production graph before enabling the code path:

```cypher
CREATE VECTOR INDEX FOR ()-[e:RELATES_TO]->() ON (e.fact_embedding)
OPTIONS {dimension:384, similarityFunction:'cosine'}
```

`KG_HUB_EDGE_VECTOR_INDEX_PERCENT` chooses a stable percentage by observation
ID. The initial release used 10%, then a 50% load test was stopped. The
repaired release passed new 10% and 50% canaries; the next stage uses a 100%
Compose default. Setting it to 0 restores exact search without dropping the index.
Index-related query failures fall
back to exact; read-set conflicts and unrelated graph errors retain their
normal handling.
The stored read dependency contains the query actually used, so a restored
round validates with the same semantics it used before the release.

Observe one full hour of `[ingest:vector_query]`, `[ingest:vector_fallback]`,
`[ingest:similarity_query]`, completed ingests, conflicts, and error rates.
Compare with a similar load window before increasing the percentage. To
remove the index after routing is back at 0:

```cypher
DROP VECTOR INDEX FOR ()-[e:RELATES_TO]->() ON (e.fact_embedding)
```

The `Entity.name_embedding` index was proven to work on the isolated graph,
but the prepare-time node query was much shorter, so this change leaves node
search exact.

## First production canary: 10%

The first hour after release `6952785` (2026-09-29 19:10–20:10 UTC) logged
34 distinct dispatched observations. One landed in the stable 10% bucket.
It executed 75 indexed broad-edge searches with zero index fallbacks. Across
prepare and validation, that observation's 95 broad-edge reads averaged
0.687 seconds; the other observations' 1,071 reads averaged 7.092 seconds.
These are concurrent workloads, not a controlled latency experiment.

The sole canary observation failed after three optimistic read-set conflicts;
two non-canary observations logged unrelated `InternalServerError`s. The
failed round's 30 sampled indexed reads produced identical digests on three
immediate repetitions. Five of 25 indexed edge dependencies in the latest
round differed from their saved results at a later audit, while the node and
filtered edge dependencies matched. That audit cannot separate intervening
graph writes from index instability; the later static-graph test below found
the latter under concurrent reads. The 10% sample has no successful canary
observation and cannot establish business success rate.

The 50% stage exposed a canary-specific failure pattern: six indexed
observations included three `GraphReadConflict` failures and one success,
with 13 conflicts in total. On the unchanged isolated graph, the same
asynchronous Falkor driver returned stable edge identity sets for all 12
stored queries when serialized, but unstable sets for all 12 with three
concurrent queries. Larger ANN candidate pools did not reliably fix this.
Release `0decfee` returned production routing to 0%.

The repair serializes only relationship vector-index reads across observations
and validation phases in the server event loop. Other graph reads retain
their existing concurrency. A unit test exercises both behaviors. The
Compose default returned to 10% for a new canary.

## Repaired 10% canary

During the full hour after release `6945fc8` (2026-09-29 21:10–22:10 UTC),
48 distinct dispatched observations included two in the stable 10% bucket.
Both completed successfully. The window logged 36 indexed queries, no index
fallbacks, one canary read-set conflict and ten non-canary conflicts. The
canary's 78 broad-edge reads across prepare and validation averaged 1.173
seconds (median 0.492, maximum 17.301 including serial wait); 1,031 other
broad-edge reads averaged 6.765 seconds (median 5.722). Four non-canary
errors were logged: three `InternalServerError` and one `ValidationError`.
The service stayed healthy.

Two successful canary observations establish that the repaired path can
complete business writes, but do not estimate its failure rate precisely.
The next 50% stage is a separate load and correctness gate; return to 0% if
indexed errors or excess read-set conflicts reappear.

## Repaired 50% canary

During the first full hour after release `74bbe20` (2026-09-29 22:18–23:18
UTC), 52 distinct observations were dispatched. Twenty-four were in the stable
50% bucket; 21 completed, none failed, and three were still in flight at the
window end. There were 263 indexed queries, no index fallbacks, three canary
read-set conflicts and 12 non-canary conflicts. Two non-canary observations
failed, one with `ValidationError` and one with `GraphReadConflict`. The
service remained healthy.

The canary's 563 broad-edge reads across prepare and validation averaged
1.331 seconds (median 0.487, maximum 33.251 including serial wait), versus
5.832 seconds (median 4.826) for 678 non-canary reads. These are different
observations under shared load, not a paired benchmark. The long indexed tail
shows that serial queueing remains relevant as routing rises. The 100% stage
therefore needs a separate full-hour gate on successful completions, errors,
conflicts, index fallbacks, and read latency before the rollout is considered
complete.

## 100% production observation

Release `936ee48` enabled the 100% default at 2026-09-29 23:30:50 UTC. Its
first six indexed observations completed successfully with no conflict or
fallback before an independent dashboard release replaced the container at
23:43:58 UTC. That release (`6b177e4`) added flow metrics without changing the
vector route.

Between 23:43:58 and the last pre-replacement sample at 00:38:36 UTC, the
`6b177e4` container dispatched 39 distinct observations: 37 completed, one
failed after all three `GraphReadConflict` prevalidation rounds, and one was
still in flight. There were 15 conflict events across the cohort, 524 vector
queries and no index fallbacks. Its 1,002 broad-edge reads averaged 0.341
seconds (median 0.172, maximum 2.270). A separate `ValidationError` in that
container belonged to a resumed task with no broad-edge similarity read; it
is included in the overall error count but not the 39-dispatch cohort.

At 00:39:39 UTC another dashboard-only release (`fc5bfd3`) replaced the
container. The effective vector percentage remained 100 and the service was
healthy. By 00:44:31 UTC, its four dispatched observations were still in
flight; 37 vector queries and 48 broad-edge reads had completed with no
fallback or error. These reads averaged 0.193 seconds (median 0.118, maximum
0.747). Container replacement leaves approximately one minute between the
last old-container sample and the new-container start without retained task
logs, so the two segments must not be presented as one uninterrupted log
series. The vector implementation was unchanged across both dashboard releases.

This evidence supports keeping 100% routing: index reads remain fast, there
are no index fallbacks, and the one conflict failure has not formed a repeat
pattern. It does not prove that indexing alone increased completed ingests per
hour; writer-lock wait and workload mix also affect throughput. Continue
watching conflict failures and long-tail index waits, and return routing to
0% if a canary-specific failure pattern recurs.

The same deployment included the reconciliation projection fix from PR #49.
The protected read-only endpoint now answers successfully. At the final check,
eight recent `GraphReadConflict` error keys remained: each had one failed
execution, no persisted episode UUID, no in-flight work, and no unknown model
outcome. This check did not clear or replay those keys; their recovery remains
under the existing refinery policy.

## Semantic search (T-0345, 2026-10-22)

`graphiti_client.search_edges_semantic` (the `/api/search` semantic leg,
`/api/search_semantic`, `tools/kg_eval`) can use the same index:
`KG_HUB_SEARCH_VECTOR_INDEX=1` takes `max(1024, 20 × limit)` index
candidates and reranks them with Graphiti's formula and `min_score`; the
retired-edge filter and refetch rounds are unchanged. **The Compose default is
0** until the production index is rebuilt and accepted (below).

### Evidence

- Production, read-only: one indexed query takes 11–17 ms (k=1024) against
  0.6–1.1 s for Graphiti's exact scan over 172,188 edges. Eight serial
  `/api/search` samples (search run locally against the live graph, bump=0,
  NAS below 85 °C before each): p50 777 → 129 ms, max 968 → 194 ms; semantic
  leg p50 729 → 87 ms. All eight top-10 lists were identical.
- A local FalkorDB 4.18.10 (same version as production) loaded with the same
  MiniLM vectors and index options: for all 235 evaluation queries (golden 35,
  cross-language 100 Chinese + 100 English) the semantic leg's deduplicated
  fact list equals the exact scan's at limit 10, 30 and 40. With 512
  candidates 18 of 235 differ, hence the 1024 floor. The six evaluation groups'
  top-3/5/10/20 hits are therefore unchanged.

### Concurrent vector queries return corrupted results

On an unchanged graph, two `db.idx.vector.queryRelationships` calls running at
the same time (separate connections included) return duplicated
relationships that push correct candidates out: of 1,000 concurrent queries,
715 were wrong and every one contained duplicates; the 285 without
duplicates were all correct. Concurrent non-vector reads (scans, point
lookups) do not cause it. This is the instability the ingest canary above
ran into.

The search path therefore takes the same per-event-loop lock as ingest
vector reads (`utils.graphiti_parallel._vector_read_lock`), treats a candidate
list with duplicates as corrupted, retries up to three times, and then falls
back to the exact scan. Other processes (e.g. `tools/kg_eval` in the weekly
report container) are not covered by the lock; if one overlaps with ingest,
the ingest read has no duplicate check. Keep `kg_eval` on the exact path
unless that overlap is ruled out.

### The production index is degraded; rebuild before enabling

After the 2026-10-21 model switch rewrote every `fact_embedding` in place, the
index reports `numDocuments` 172,188 but `maxDocId` 390,109. In a serial
production sample of 24 queries, 4 missed edges from the exact top 50 —
including ranks 2 and 3 for one query — and 4096 candidates did not recover
them; the missed edges' stored vectors equal the recomputed ones. Replaying
the history locally (index built on the old bge vectors, then all vectors SET
to MiniLM) reproduces it: 23 of 235 semantic legs differ and larger candidate
counts do not help, while a freshly built index differs on none. In that
replay the six groups' top-10 hits still did not change, but the production
sample shows misses at the very top, so enable search only on a rebuilt index.
Ingest's indexed broad-edge lookup (100% above) reads the same degraded index
whenever the circuit breaker is closed.

Rebuild (a graph write; off-peak, NAS temperature watched, decision is the
operator's):

```cypher
DROP VECTOR INDEX FOR ()-[e:RELATES_TO]-() ON (e.fact_embedding)
CREATE VECTOR INDEX FOR ()-[e:RELATES_TO]-() ON (e.fact_embedding)
OPTIONS {dimension:384, similarityFunction:'cosine', M:16, efConstruction:200, efRuntime:10}
```

Wait until `CALL db.indexes()` shows the index operational, then run the
read-only acceptance check `python -m tools.vector_index_check` (8 golden
queries by default; each runs one exact scan, so it pauses between queries).
It passes when no result has duplicates and no edge from the exact top 30 is
missed; on the local fresh index all 35 golden queries pass, on the replayed
degraded one 26 of 35 fail. Only then set `KG_HUB_SEARCH_VECTOR_INDEX=1`.
Rewriting all vectors again (any future model switch) needs the same rebuild.
