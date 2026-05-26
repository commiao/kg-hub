# kg-hub ingest filter — observation phase guide

**Status**: ⏳ Active. Started 2026-05-20.
**Filter mode**: `shadow_mode=true` (logs decisions, does not block any obs).

## What's happening right now

Every 15 min the launchd cron `com.kg-hub.claude-mem-ingest` picks up new
observations from `~/.claude-mem/claude-mem.db`. For each one, the new filter
in `utils/ingest_filter.py`:

1. Scores it (deterministic Python, zero LLM)
2. Appends one JSON line to `data/.ingest_decisions.jsonl` with score breakdown
3. **Ingests it anyway** (because `shadow_mode=true`)

The point of observation phase is to accumulate enough live decision data to
verify the filter's choices match what a human would make, *before* flipping
the kill switch.

## Exit criteria — when is it safe to flip `shadow_mode=false`?

Auto-checked by `tools/decisions_summary.py`. All three must be ✓:

| Criterion | Required | Why |
|---|---|---|
| **Sample size** | ≥ 500 decisions in the window | Below this, statistics are noisy |
| **Reject rate** | between 20% – 50% | <20% = filter too lenient; >50% = too aggressive |
| **HV score-layer rejects** | exactly 0 | A `decision`/`bugfix`/`feature`/`security_alert` obs scoring below threshold is a tuning bug. (Hard-gate rejects on empty stubs don't count — those are upstream worker artifacts.) |

The summary script renders **✅ READY** or **⏳ NOT YET** at the top of every
weekly report; no manual math needed.

## How to monitor

### Active inspection (run any time)

```bash
cd ~/workspace_claudeCode/kg-hub

# Last 24h of decisions
./spike-graphiti/.venv/bin/python -m tools.decisions_summary --window 24h

# Last 7 days (default)
./spike-graphiti/.venv/bin/python -m tools.decisions_summary

# Full history
./spike-graphiti/.venv/bin/python -m tools.decisions_summary --window all
```

### Passive cadence

`com.kg-hub.weekly-report` (Sundays 09:00 local) automatically writes:
- `~/.kg-hub/reports/quality-baseline-YYYY-MM-DD.md` — KG composition snapshot
- `~/.kg-hub/reports/decisions-7d-YYYY-MM-DD.md` — last-week decisions + verdict
- `~/.kg-hub/reports/INDEX.md` — rolling pointer

Logs at `~/.kg-hub/logs/weekly-report.{out,err}.log`.

## What to look for each week

In the weekly report:

1. **Verdict block at the top** — ⏳ or ✅
2. **"Borderline accepts (60–69)"** — these are *just barely passing*. Skim 5–10
   titles. If most look valuable, the threshold is right. If most look like
   noise, raise `score_threshold` for that platform in
   `config/ingest_filter.json`.
3. **"Borderline rejects (50–59)"** — these are *just barely rejected*. Skim
   5–10. If any look truly valuable, lower the threshold OR adjust scoring
   weights to give that category more lift.
4. **"High-value type rejects — REVIEW THESE"** — every entry deserves a
   manual look. The verdict gate already separates `hard_gate` (upstream stub,
   ignorable) from `score` (real concern). Any `score`-layer entry here =
   stop and investigate.

## Tuning loop (during observation phase)

Edits to `config/ingest_filter.json` take effect on the **next** ingester run
(no service restart needed — the file is re-read every invocation).

After any tuning edit:
```bash
# Re-evaluate ALL historical obs against the new config
./spike-graphiti/.venv/bin/python -m tools.backfill_clean
```

This produces a fresh `backfill-dryrun-YYYY-MM-DD.{md,jsonl}` showing exactly
which past obs the new config would treat differently — without touching the
live KG.

## Exit — flipping `shadow_mode` to false

Only do this when:
- ✅ at the top of the weekly report
- You've reviewed at least one weekly report's borderline samples
- (Optional) You've also run `backfill_clean.py` with the same config and
  reviewed its rejected list

The flip:

```bash
# Open the file, change one boolean
$EDITOR ~/workspace_claudeCode/kg-hub/config/ingest_filter.json
#   "shadow_mode": true   →   "shadow_mode": false
```

That's it. The next ingester run (within 15 min) will start blocking rejects.
Rejected obs IDs go into the `rejected_obs_ids` list inside
`data/.ingested.claude_mem.json` so they're not re-evaluated every cycle.

If you regret it, flip back to `true`. Already-rejected obs stay rejected
unless you also manually clear `rejected_obs_ids`.

## Cleaning up historical pollution (optional, after flip)

`tools/backfill_clean.py` currently runs in dry-run mode only. To remove the
~340 historically-ingested low-value episodes from FalkorDB, an `--apply` flag
needs to be added — deliberately deferred until shadow phase concludes. The
dry-run output already shows exactly what would be removed.

## Logs and artifacts

| Path | Written by | Purpose |
|---|---|---|
| `data/.ingest_decisions.jsonl` | every ingester run | per-obs decision record |
| `data/.ingested.claude_mem.json` | ingester | watermark + (when not shadow) rejected list |
| `~/.kg-hub/reports/` | weekly_report cron | dated markdown reports |
| `~/.kg-hub/logs/claude-mem-ingest.{out,err}.log` | launchd | 15-min ingester runs |
| `~/.kg-hub/logs/weekly-report.{out,err}.log` | launchd | Sunday 09:00 reports |
