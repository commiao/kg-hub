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
repaired release returns to a 10% Compose default. Setting it to 0 restores
exact search without dropping the index. Index-related query failures fall
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
Compose default returns to 10% for a new canary; do not raise it until
indexed observations complete and the read-set conflict rate is acceptable.
