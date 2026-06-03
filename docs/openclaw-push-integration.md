# OpenClaw / Feishu — kg-hub PUSH Integration

## Why PUSH and not just keep PULL

OpenClaw on the VPS already exposes a `kg-query` skill that calls
`kg_search`/`kg_episode_search` over Tailscale. That's **PULL**: works only
when the agent (or the user) remembers to invoke it. Production data shows
2 real queries lifetime — i.e., **agents forget over time**, even though
the skill is registered.

The fix is the same as Claude Code / Cursor: convert PULL → PUSH. For
OpenClaw, that means modifying the **skill prompt template** so canonical
content is automatically fetched and prepended to the response context on
every relevant Feishu message, not optionally invoked.

## What changes (architecture)

```
Today (PULL):
  Feishu msg → OpenClaw → LLM gen prompt → (agent decides to call kg-query?)
                                          ├─ usually no  → no kg-hub data
                                          └─ rarely yes  → 1 query

Target (PUSH):
  Feishu msg → OpenClaw → skill prompt template:
                          1. ALWAYS prepend: latest kg_episode_search hits for
                             keyword derived from user message / project context
                          2. then user message
                          3. then LLM gen response
                        → every reply grounded in canonical content
```

Result: **every Feishu reply** is grounded in kg-hub canonical content, not
just the ones where the agent decides to query.

## Implementation steps (require VPS SSH access)

> **Note**: I can write the prompt template here; activating it needs SSH to
> the OpenClaw VPS (Tailscale IP `100.79.177.102` per prior memory).

### Step 1 — locate the kg-query skill prompt on VPS

```bash
ssh openclaw-vps  # or whatever your tailnet alias is
cd /home/admin/clawd     # or wherever OpenClaw is installed
find . -name "*.md" -path "*kg-query*" -o -name "kg-query.yaml"
```

Expected: a skill definition file (markdown front-matter + system prompt body).

### Step 2 — prepend an auto-fetch block to the skill prompt

Current shape (PULL):

```markdown
---
name: kg-query
description: Query kg-hub knowledge graph
---

When the user asks about past decisions, you may call kg_search.
```

Target shape (PUSH):

```markdown
---
name: kg-query
description: Query kg-hub knowledge graph
auto_fetch:
  - tool: kg_episode_search
    query_template: "{user_message_keywords}"
    inject_as: prepended_context
    max_results: 3
---

The following kg-hub canonical context has been auto-fetched for your reply:

{auto_fetch_results}

When responding, integrate this context naturally. If the canonical content
doesn't match the question, say so and call `kg_search` for related edges.
```

(Exact YAML/template syntax depends on OpenClaw's skill engine — check the
existing skill files for the actual key names.)

### Step 3 — test via Feishu

Send a message to the bot like "kg-hub 项目动机是什么". Verify the response:
1. Cites canonical DESIGN.md content (5 痛点)
2. NOT just the LLM guessing from training data

### Step 4 — verify usage_count accumulates

Run on the Mac:

```bash
cd ~/workspace_claudeCode/kg-hub
./spike-graphiti/.venv/bin/python -m tools.usage_ranking
```

After a few real Feishu messages, the canonical episodes' `usage_count`
should grow — but only if OpenClaw's auto-fetch path is configured to
bump usage_count via a direct FalkorDB UPDATE after each fetch (or via the
kg-hub HTTP `/api/search` endpoint, if we add a usage-bump side-effect there).

### Step 5 — make usage_count bump real

For OpenClaw's PUSH to feed the Lindy ranking, each auto-fetch needs to
trigger the same usage_count bump that `kg_push_hook.py` does for Claude Code.

Two options:

1. **Modify kg-hub server** to bump `usage_count` automatically when
   `/api/search` returns hits (one-line cypher: `MATCH (n:Episodic) WHERE
   n.uuid IN $hits SET n.usage_count = coalesce(n.usage_count, 0) + 1`)
2. **Add a usage-bump endpoint** (`POST /api/usage/bump`) that OpenClaw
   calls after fetch

Option 1 is simpler and benefits all PULL paths (MCP `kg_search`, MCP
`kg_episode_search`, HTTP `/api/search` — they'd all contribute to the
implicit-feedback signal).

## Rough cost

| Step | Effort |
|---|---|
| 1-2: locate + edit skill prompt on VPS | 20 min (with SSH) |
| 3: test | 10 min |
| 4-5: usage_count plumbing | 30 min |
| **Total** | **~1 hour** with VPS access |

## Why this is the highest-value PUSH extension

OpenClaw / Feishu is the **only place where real external users** interact
with kg-hub (the 2 measured queries this lifetime came from Feishu). Making
its responses ground in canonical content is the biggest single quality
upgrade we can land — bigger than Claude Code or Cursor, which mainly serve
the maintainer's own sessions.

## Out of scope for now

Without SSH access to the OpenClaw VPS in this session, this is documented
but not executed. Path forward:

1. Get VPS SSH alias / credentials
2. Follow Step 1-5 above
3. Update this file with the actual paths and code that worked
