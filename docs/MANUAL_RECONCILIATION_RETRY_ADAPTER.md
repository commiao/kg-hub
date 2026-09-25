# Manual model-step retry: Graphiti 0.29.0 adapter boundary

Status: design gap. The read-only reconciliation API is implemented; **manual
retry is not implemented and must not be exposed or deployed as if it were**.

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
   After three actual calls with no usable result, close the step and move the
   business task to `failed`. Unknown admission remains frozen for review.
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
