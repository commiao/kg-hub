# Evidence-limited source titles

An incomplete body does not establish that its title was later disproved. Keep
these two review dispositions distinct. Existing unqualified titles and explicit
`（后被推翻：…）` corrections retain their existing contracts.

After the service advertises `title_qualifiers: ["superseded",
"evidence_scope_v1"]`, a reviewer may retain the exact original title followed
by `（证据范围：…）`. Its complete translation includes the same limitation.
Do not use this extension against an older service.

The source's review row includes a separate `title_scope` object:

```json
{
  "kind": "evidence_limit",
  "qualification": "正文具体支持的范围及尚未建立的结论",
  "translated_qualification": "The complete translated evidence limitation.",
  "source": "body",
  "quote": "An exact quote from the frozen source supporting the reviewed scope.",
  "note": "Why the body does not establish the full title claim."
}
```

`qualification` exactly matches the first fact's suffix. The translated
qualification occurs in the second fact. Source, lookahead or another exact
frozen batch source may supply the evidence, under the existing evidence rules.
Every other fact, summary, extraction identity and full-review flag remains
required. A scope annotation cannot replace core coverage or fabricate reversal
evidence; source semantics remain the named reviewer's responsibility.

This changes the shared service/client quality validator. Publish and verify
the service capability before clients begin using the extension. A client-only
publication cannot enable it on an older service.

If the original owner stopped with `infrastructure_failed` after a successful
public `agent preflight` that lacked `evidence_scope_v1`, retain that receipt in
the campaign audit. After the provider is published, a coordinator can use
`campaign reconcile-local --id ID --model MODEL --slot SLOT --batch BATCH
--capability-receipt AUDIT_JSON --require-capability evidence_scope_v1 --reason
REASON`. This requires the same original terminal session, one unstarted pending
batch, the original command/output matching the retained receipt, no formal
writes or unknown operation, and a new successful provider capability readback.
It records an immutable continuation proof, performs no graph/model writes, and
does not approve source semantics or clear an actual permission/NAS refusal.
