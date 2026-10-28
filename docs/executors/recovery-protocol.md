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
