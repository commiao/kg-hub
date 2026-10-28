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
