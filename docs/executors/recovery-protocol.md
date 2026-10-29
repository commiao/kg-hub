# Recovering an interrupted campaign owner

Recovery keeps the original session, source quota and failure history. The
coordinator uses the published CLI; a service readback is not a model probe.

* `campaign reconcile-settled --id ID --batch BATCH` handles an exact native
  capacity failure during sequential formal submission. It requires the original
  idle operation lease, original acknowledged prefix, exact accepted payload
  hashes, terminal service executions and an absent unaccepted suffix. Newly
  adopted records use `receipt_kind=source-settlement-v1` and `http=null`; the
  original HTTP receipts are retained. Repeating reconciliation never recounts
  a source. `reconcile-worker` then binds that proof to the original owner without
  an additional paid readiness call. Only the unaccepted suffix is submitted.
* `campaign repair-decision --id ID --batch BATCH --file REQUEST --reviewer NAME`
  registers one independently reviewed source repair for a submitted own batch.
  The replacement quality context preserves the original source, lookahead,
  rules and extraction identity. This appends a separate repair receipt and never
  changes the original production decisions or random-review denominator.
* `campaign reconcile-local --id ID --model MODEL --slot SLOT --reason REASON`
  permits bounded same-owner recovery after a completed read-only preview with
  HTTP502 rows and fresh service readiness. The owner must obtain a complete
  whole-batch preview before formal submission. For an original closed-batch
  `campaign decision` rejection, additionally pass `--batch BATCH --repair-file
  REQUEST --reviewer NAME`; this exact disposition selects the separate repair
  protocol, preserving the original rejection. Other permission or write failures
  require their own disposition.

The manager fingerprints each slot's actual prerequisites. A new native attempt,
another slot's progress, or restarting the observer does not clear an unchanged
blocked condition. Source-bound uncertain operations remain isolated and cannot
be replayed; unattributed operations and real approval/NAS refusals retain the
shared stop boundary. Read-only preview recovery is bounded by persisted events.

After a worker's exact `nodes preview` summary refusal, the coordinator may use
`reconcile-local --id ID --model MODEL --slot SLOT --batch BATCH --summary-handoff
AUDIT_JSON --reason REASON`. This is an identity-only handoff, not permission to
repeat summary governance. The hash-bound `shared-summary-handoff-v1` file must
identify the original campaign/batch/model/slot/session, events SHA, refused
command ID and scope; include the coordinator's node UUID, complete identity
preview, reviewer, note, `accepted:true`, and
`disposition:identity_only_no_summary_change`. Every original review error needs
a distinct repair ID and an explicit accepted source review; the CLI freshly
checks the exact source/model/tool/run repair is applied and verified. All input
references (`scope`, `identity_preview`, repair `acceptance`, extra `evidence`)
use `{path,sha256}` inside the campaign audit. This requires a settled submitted
batch and no pending batch on that original owner. No write or denied action is
replayed, and the old errors and refusal remain. Summary changes, if needed,
still require the separate all-backing-sources governance protocol. Evidence
integrity does not certify semantic accuracy; the coordinator owns that review.

An exact terminal native capacity failure may follow the CLI's known WebSocket
reset reconnect notice. That notice alone no longer disqualifies bounded
same-session recovery. Unrelated/authentication errors, approval/NAS refusals,
unknown writes and non-capacity terminal failures still block it.

An original title and its complete translation remain intact. If the body does
not establish the title's claim, record the evidence limitation as a separate
supported fact and summary. Missing evidence is not evidence of a later reversal.

These contracts verify attribution, evidence and execution boundaries. They do
not establish semantic correctness or replace the coordinator's source review.

A repair's optional `extractor_session` may remain null under the original repair
protocol. The exact source, frozen model/tool/run identity and original slot
session still bind the separate repair receipt; a foreign non-null session is
rejected.

For a completed original owner stopped by `delegated campaign backend identity
differs`, `reconcile-local --id ID --model MODEL --slot SLOT --batch BATCH
--prior-backend OLD_COMMIT --reason REASON` requires the exact public prepare
rejection, no formal writes for that pending batch, and a successful read-only
public operation-status through the known prior recursion marker. A known prior
published marker triggers one selection of the active hash-verified adapter;
unknown identities and repeated mismatches remain errors. Native workers do not
inherit the caller's recursion marker. The original refusal remains recorded.

The bundle-selected public interpreter runs with `-I`: a previous native's
PYTHONPATH cannot override the installed core/adapters of the active composition.
Explicit standalone interpreter overrides retain their existing behavior.
An exact read-only `llm-eval: bundle core differs` failure can use
`reconcile-local ... --batch BATCH --prior-backend OLD_COMMIT --prior-bundle
INSTALLED_OLD_BUNDLE --reason REASON`. It requires the original completed owner,
no formal writes, the known installed bundle/backend binding, and a successful
public read-only probe under that old overlay. Unknown write states and other
bundle/permission failures remain blocked. A pending batch with no operation
receipt is normalized identically when recording and validating its proof.
