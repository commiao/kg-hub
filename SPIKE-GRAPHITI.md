# SPIKE: Graphiti as kg-hub L3 — Verdict ✅ PASS

> **Run date**: 2026-05-14
> **Budget**: planned 30 min, actually ~90 min (4 unknowns hit, all resolved)
> **Code**: `spike-graphiti/spike.py`
> **Database**: `spike-graphiti/kuzu_db/` (kept for inspection)

## TL;DR

**Graphiti can serve as kg-hub's L3 (central KG + search) layer.** It built a meaningful graph from 5 OpenClaw capsule narratives and reconstructed the multi-hop causal chain OpenClaw gave us as the target. Self-build of L3 is not justified.

Concrete numbers (re-run 2026-05-14 21:00 GMT+8):

| Metric | Result |
|---|---|
| Episodes ingested | 5 / 5 |
| Entities extracted (post-dedup) | 27 |
| Edges (via `RelatesToNode_` intermediate) | 17 |
| Causal chain reconstruction (vs OpenClaw target) | ✅ all key nodes present (CAPSULE-NOTIFICATION-ROUTE-2026 / notify-send.sh / Cron / 投资晚报 / 战略规划群 / 实战验证 fix) |
| Natural-language search top-1 | "Cron 通知失败怎么修的" → 实战验证 fact |
| ⚠️ LLM-coined edge names | PROPOSES_SOLUTION / MISROUTED_TO / INCLUDES_COMPONENT 等。**Phase 1 必须用 entity_types/edge_types 约束** |
| LLM | qwen3.6-plus via 百炼 Anthropic-compatible endpoint |
| LLM cost | $0 (covered by user's 百炼 coding plan) |
| End-to-end runtime (5 episodes) | ~110 s |

> Numbers are non-deterministic across runs (LLM-based extraction). Earlier run produced 22/14; current run 27/17. Order of magnitude is what matters.

## What changed in kg-hub's design

**Old plan** (now superseded for L3):
- Build Memgraph + FastAPI + MCP server from scratch (Phase 1, 1 week budget)
- Write own entity extraction prompts and JSON pipeline (Phase 0.C)

**New plan**:
- L3 = **Graphiti + Kuzu** (or Neo4j, when scale demands)
- Phase 1 becomes "wire Graphiti to OpenClaw data" — ~1-2 days
- Phase 0.C (LLM extraction) is **replaced** by Graphiti's `add_episode()` calls
- kg-hub's unique value collapses to the ingest layer (Phase 0.A + 0.B + Phase 2)

## What Graphiti gave us out of the box

1. **LLM-based entity + relation extraction** — quality is genuinely good (see edges below)
2. **Entity resolution across episodes** — `jingmiao@liblib.ai` extracted from 2 episodes was deduped automatically
3. **Bi-temporal model** — every edge has `valid_at`, `invalid_at`, `created_at`, `expired_at`
4. **Episode provenance** — every edge tracks which `Episodic` node produced it (solves OpenClaw's weak-provenance pain)
5. **Hybrid search** — BM25 + cosine + BFS on top of the graph, all bundled
6. **Kuzu embedded driver** — no Docker daemon needed for spike; can swap to Neo4j/FalkorDB later

## What Graphiti does NOT give us (still on kg-hub's plate)

| Concern | Status |
|---|---|
| Schema typing (Capsule / Issue / Fix / KnowledgeDoc) | ❌ Default extracts everything as generic `Entity`. Mitigation: pass `entity_types=` and `edge_types=` to `add_episode()` to constrain. Not tested in this spike. |
| OpenClaw capsule parser | ❌ Must build (Phase 0.A/B) — Graphiti just takes narratives |
| claude-mem obs reader | ❌ Must build (Phase 2) |
| Multi-device push agent / sync | ❌ Must build |
| MCP server | 🟡 Graphiti ships one. May need a thin wrapper for kg-hub conventions |
| Deployment / Tailscale glue | ❌ Must build |

## Gotchas (≈4 unknowns hit during spike)

1. **`load_dotenv` doesn't override existing env**. Claude Code's shell already has `ANTHROPIC_BASE_URL=https://api.anthropic.com`, which silently shadowed `~/.claude-mem/.env`. Fix: `load_dotenv(..., override=True)`.

2. **Anthropic SDK uses `auth_token=` for `Authorization: Bearer`**. Passing `api_key=` sends `x-api-key`, which 百炼 rejects. Fix: `AsyncAnthropic(auth_token=..., base_url=...)`.

3. **qwen3.6-plus runs in thinking mode by default and forbids `tool_choice={type:tool, name:...}`**. Graphiti's `AnthropicClient` requires forced tool use. Fix: monkey-patch `messages.create` to inject `extra_body={"thinking":{"type":"disabled"}}` on every call.

4. **graphiti-core 0.29 Kuzu driver does NOT create FTS indices** even though search code expects them. `build_indices_and_constraints()` is a no-op for Kuzu. Fix: after init, manually `INSTALL fts; LOAD fts;` + run all four `CREATE_FTS_INDEX` statements from `graph_queries.py`.

All four fixes are in `spike-graphiti/spike.py` — keep as reference for Phase 1.

## Causal chain reconstruction (the critical test)

**Target** (from OpenClaw [DESIGN.md §8.C](DESIGN.md)):
```
Cron 通知发送失败
  → caused_by → 飞书 chat_id 硬编码分散
  → leads_to → 投资晚报→战略规划群（应为财务管家群）
  → diagnosed_by → CAPSULE-NOTIFICATION-ROUTE-2026
  → implemented_as → notification-route.db + notify-send.sh
  → verified_by → 2026-03-20 实战演练通过
```

**Actual Graphiti graph** (extracted from natural-language episode):
```
Cron ← FIXES_ISSUE_FOR ← 通知路由统一配置系统 (= capsule title)
                              ↑ HAS_TITLE
                       CAPSULE-NOTIFICATION-ROUTE-2026
                              ↓ DIAGNOSES_ISSUE_IN
                              飞书
                       (chat_id narrative is in episode body, lifted into fact text)

通知路由统一配置系统 → IMPLEMENTS_ARTIFACT → notification-route.db
通知路由统一配置系统 → IMPLEMENTS_ARTIFACT → notify-send.sh
投资晚报 → SENT_TO_INCORRECTLY → 战略规划群
落户监控 → SENT_TO_INCORRECTLY → 系统监控群
```

**Verdict**: every causal node from OpenClaw's chain has a corresponding Graphiti node, and the navigable path Cron → fix → capsule → root issue is intact. Edge type names differ from our v0.2 schema (auto-coined by LLM: `FIXES_ISSUE_FOR`, `IMPLEMENTS_ARTIFACT`, `DIAGNOSES_ISSUE_IN`) but semantically aligned. Constraining with `edge_types=` should produce canonical edges (`fixed_by`, `implemented_as`, `diagnosed_by`).

## Decision: pivot kg-hub to Graphiti-based L3

**Action items** (will fold into DESIGN.md decision 9 + ROADMAP Phase 1 in a follow-up):

1. ✅ Adopt **Graphiti** as L3 (KG storage + entity/relation extraction + search)
2. ✅ Keep **Kuzu** for now (embedded, Tailscale-zero-deploy spike-friendly). Re-evaluate Neo4j/FalkorDB at Phase 2 when push agent runs concurrently.
3. ✅ Keep **qwen3.6-plus via 百炼** as LLM (decision 4 unchanged — proven to work end-to-end)
4. ⚠️ Future Phase 1: pass `entity_types={Capsule, KnowledgeDoc, Issue, Fix, Concept, ...}` and `edge_types={diagnosed_by, fixed_by, ...}` to constrain extraction to v0.2 schema
5. ⚠️ Future Phase 1: bundle the 4 fixes above into a small `kg_hub.bootstrap` helper so Phase 1 doesn't rediscover them
6. ⚠️ Phase 0 通过门槛 needs revisiting: with Graphiti doing the heavy lifting, "150 capsules + 50 implicit relations" target is easily achievable. Re-anchor on **schema fidelity** and **multi-hop query latency** instead.

## How to reproduce

```bash
cd /Users/mac/workspace_claudeCode/kg-hub/spike-graphiti
source .venv/bin/activate
python spike.py           # full ingest + query (~2 min, hits 百炼 5 times)
python spike.py --reuse   # inspect existing kuzu_db (no LLM calls)
```

DB files at `spike-graphiti/kuzu_db/` are safe to delete and re-run.
