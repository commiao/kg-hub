# Manual model-step retry: Graphiti 0.29.0 adapter boundary

Status: design gap. The read-only reconciliation API is implemented; **manual
retry is not implemented and must not be exposed or deployed as if it were**.

An isolated partial adapter now exists in `utils/graphiti_stage_adapter.py`.
It wraps the pinned 0.29.0 extraction helper and stores immutable serialized
`EntityNode` objects (including their generated UUIDs and attribution map),
ordered semantic candidate sets, and resolved nodes. It rejects input/UUID
drift and provides a one-way durable
`begin_graph_commit` fence. `tests/test_graphiti_stage_adapter.py` exercises
candidate changes and process-style restoration against real pinned Graphiti
node types and resolver helpers. It now also stores complete edge and attribute
stage outputs and a typed graph commit receipt. An edge or attribute stage that
started but did not save its output freezes on continuation, since Graphiti
may have made multiple subcalls or read changing graph state inside that stage.
The graph write has a durable one-way fence; a crash after the graph write but
before the receipt also freezes. The adapter is **not wired into live ingest**:
these uncertainty windows require finer internal checkpoints or an atomic
business graph commit receipt before a manual retry endpoint can safely promise
a terminal outcome. A repeated commit attempt never writes again.

### Crash-window readback and remaining internal hooks

Pinned Graphiti 0.29.0 `_process_episode_data` calls
`add_nodes_and_edges_bulk` (`graphiti.py:683-690`), whose episode/entity nodes
and MENTIONS/RELATES_TO relationships run in one graph driver write transaction
(`bulk_utils.py:128-148,151-259`). Saga association is written in separate
operations afterwards (`graphiti.py:694-732`). The adapter now persists the
exact episode, node, and entity-edge identities **before** entering that write.
`inspect_started_graph_commit` reads them back using only `MATCH/RETURN` and
reports `core_absent`, `partial`, `core_materialized`, or `unknown`, with the
saved-receipt and saga-required flags. A core-materialized result is useful
evidence, but it is not a terminal business receipt: generated MENTIONS UUIDs
were not pre-snapshotted, and saga links may be missing after a crash. The
function never writes or promotes a task.

The edge stage contains `extract_edges.edge` plus parallel per-edge resolution
(`edge_operations.py:116-206,324-534`), with potential dedupe, custom
attribute, and timestamp model calls (`:597-601,661-669,715-720,777-790`).
It also re-reads graph edge candidates (`:364-415`). The attribute stage runs
per-node model calls in parallel and later batched summary model calls
(`node_operations.py:725-765,875-890,959-965`). The existing model SDK hook
journals individual HTTP requests, but it cannot checkpoint these changing
in-memory per-edge/per-node inputs or all concurrent results. The safe next
fork point is before each per-edge/per-node work item: persist its typed inputs,
ordered graph candidates, output and subcall IDs, then resume only the failed
item after all prior successful outputs are restored. `asyncio.gather` may
leave sibling calls running after one exception, so recovery must also wait
for or explicitly account for every sibling's journal state.

The pinned dependency is `graphiti-core==0.29.0`. Its `Graphiti.add_episode`
reads recent episodes, creates an in-memory episode, runs `extract_nodes`,
`resolve_extracted_nodes`, `_extract_and_resolve_edges`, and
`extract_attributes_from_nodes`, then writes the graph with
`_process_episode_data`. It exposes no continuation argument or stage output
injection. Passing `uuid` fetches an existing episode and does not assign a
fresh stable UUID. See
https://github.com/getzep/graphiti/blob/v0.29.0/graphiti_core/graphiti.py#L961-L1048.

The kg-hub SDK journal can replay a completed response only when the exact
request digest matches. Calling `add_episode` again can read a different graph,
generate new in-memory UUIDs, and produce different downstream prompts. A new
prompt is a different model step; allowing it through would silently repeat
paid work and break the operator's one-step authorization. Replaying the whole
ingest also duplicates predigest parent/child graph writes.

## Pinned 0.29.0 continuation audit (2026-09-26)

The isolated pinned-wheel inspection establishes a concrete unsafe boundary:

- `graphiti_core/graphiti.py:1052-1065` constructs a fresh `EpisodicNode` if
  `uuid` is absent. Supplying `uuid` instead calls `get_by_uuid`, which requires
  the episode to have already been written; it cannot inject the saved
  pre-commit episode into a resumed extraction.
- `graphiti_core/utils/maintenance/node_operations.py:282-332` converts even
  a replayed extraction answer into fresh `EntityNode` objects, each with a
  `uuid4` default (`graphiti_core/nodes.py:93-98`). The node-to-episode map is
  keyed by these new UUIDs. Thus an exact cached model answer alone cannot
  reproduce the original intermediate graph objects.
- `node_operations.py:406-449,626-640` searches the live graph again for
  semantic dedup candidates. A concurrent graph change, or a partial earlier
  write, can alter the candidate set and the next resolution prompt. The
  current context checkpoint pins only `previous_episode_uuids`; it does not
  pin this candidate search or the extracted/resolved node objects.
- `graphiti_core/graphiti.py:1074-1131` passes these phase outputs into edge
  resolution, attribute extraction, and graph commit. The pinned public
  `add_episode` signature has no stage-output or candidate-snapshot argument.

The isolated orchestration test demonstrates that a *mocked deterministic*
extract/resolve path replays saved responses, stops on digest drift, and admits
one granted retry. It does not prove continuation through the real candidate
search or fresh UUID construction. Therefore the grant and replay primitives
must stay unconnected to an HTTP retry endpoint or live worker. A one-shot
grant cannot repair a prompt that no longer addresses the same step.

The minimum safe implementation is a pinned Graphiti adapter/fork that writes
the episode object and `extract_nodes` output (including UUIDs and attribution
map) before `resolve_extracted_nodes`; writes the exact ordered candidate
snapshot plus resolved nodes/UUID map before edge work; then checkpoints edge
and attribute phase outputs before graph commit. Recovery loads each completed
stage object verbatim, verifies its input digest and Graphiti/schema version,
and permits a new HTTP call only at the granted failed step. A commit token on
the final episode write and predigest child checkpoints are also required.
Validation must use real 0.29.0 helpers with two runs against deliberately
changed candidate search results and fresh UUID generation: all saved paid
steps must be cache hits, exactly one failed step may be admitted, and any
unsnapshotted drift must freeze before HTTP. Until that gate passes, no
business-queue retry command is safe to expose.

## Minimum adapter/fork contract

1. Persist one operation envelope before the first model call: business task ID,
   immutable input digest, episode UUID and creation timestamp, reference time,
   previous episode UUIDs, graph candidate snapshot/version, and Graphiti schema
   version. For predigest, persist the selected route, observation list, parent
   UUID, and each child identity before writing a child.
2. Add a resumable `add_episode` adapter that accepts the envelope and stage
   checkpoints. Checkpoint the serialized output of each of the four phases
   above, with input digests, before starting the next phase. A process restart
   must load completed phases rather than rerun them. The final graph write
   needs an idempotent commit key tied to the business task/episode UUID.
3. At every model call, the client must look up the exact step by task ID and
   request digest. A saved successful response is replayed locally. A previously
   failed step may receive a new Idempotency-Key only under a durable, one-shot
   operator grant. The grant is consumed atomically before HTTP admission and
   records the attempt ordinal. Proven gateway preflight refusal consumes no
   *actual-call* slot; admitted, timed-out, or admission-unknown attempts do.
   A locally persisted HTTP-start marker with no usable response counts as a
   failed business attempt after the maximum timeout, even if gateway/provider
   admission remains unknown. A prepared intent with no local HTTP-start marker
   and no gateway admission proof stays frozen. Proven pre-provider refusal
   counts as zero. After three counted calls with no usable result, close the
   step and move the business task to `failed`.
4. During resume, any uncheckpointed or different model request before the
   granted failed step must stop without HTTP. After that step succeeds, new
   downstream steps may execute only as part of the same authorized business
   continuation and each gets its own journal identity/count. A second failure
   stops the run for another explicit operator action.
5. The retry endpoint must CAS the task from `needs_reconciliation` into a
   durable queued/resuming state, persist the grant, and send that same task to
   the original business worker. It must not call the model inline. The worker
   must use the checkpointed operation, and only persisted graph/business output
   may mark `ok`. No scheduler or cleanup job may create a grant.

Integration points: `graphiti_client.py` builds the pinned Graphiti client;
`kg_hub_server.py::_do_extract_inner` and `_locked_add_episode` call
`add_episode`; `model_gateway_client.py::create_with_gateway_contract` owns
HTTP identity; `utils/model_attempt_journal.py` owns durable attempt evidence.
The currently exposed `POST /api/ingest/reconciliation/check` performs only
status/evidence checks and cannot authorize a new paid call.
