"""
tools/usage_ranking.py — surface the PUSH-hook usage signal.

Reads `usage_count` and `last_used_at` from `:Episodic` nodes in FalkorDB
(written by `tools/kg_push_hook.py` each time it injects a canonical doc
into a SessionStart) and produces three rankings:

  1. Top 10 canonical episodes by usage  — the Lindy signal: what's
     consistently relevant to live sessions.
  2. Top 10 NON-canonical with usage > 0 — **promote candidates**:
     ordinary observations that the PUSH hook found relevant enough
     to inject. These deserve thought: should they be canonicalized?
  3. Bottom 10 canonical with usage == 0 — **demote candidates**:
     canonical docs that no session has ever queried. Either no one
     needs them, or they're worded such that the hook never picks them.

Read-only. Generates a markdown report; never modifies the graph.

Exit: 0 always (informational; not a gating signal).

Usage:
  python -m tools.usage_ranking            # write report, print to stdout
  python -m tools.usage_ranking --no-write # stdout only
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

REPORT_DIR = Path.home() / ".kg-hub" / "reports"
TOP_N = 10


def _connect():
    """Open FalkorDB connection; returns (graph, None) on success."""
    from falkordb import FalkorDB
    host = os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1")
    if host == "localhost":
        host = "127.0.0.1"
    port = int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379"))
    pw = os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None
    return FalkorDB(host=host, port=port, password=pw).select_graph(
        os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub")
    )


def query_top_canonical_used(graph, n: int = TOP_N) -> list[tuple]:
    result = graph.query(
        "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
        "AND coalesce(n.usage_count, 0) > 0 "
        "RETURN n.name AS name, coalesce(n.usage_count, 0) AS uc, n.last_used_at AS last "
        "ORDER BY uc DESC LIMIT $n",
        params={"n": n},
    )
    return list(result.result_set)


def query_promote_candidates(graph, n: int = TOP_N) -> list[tuple]:
    """Non-canonical episodes that have nonetheless earned usage hits."""
    result = graph.query(
        "MATCH (n:Episodic) WHERE NOT (n.name STARTS WITH 'kg-hub-canonical') "
        "AND coalesce(n.usage_count, 0) > 0 "
        "RETURN n.name AS name, coalesce(n.usage_count, 0) AS uc, "
        "       substring(coalesce(n.content, ''), 0, 80) AS preview "
        "ORDER BY uc DESC LIMIT $n",
        params={"n": n},
    )
    return list(result.result_set)


def query_demote_candidates(graph, n: int = TOP_N) -> list[tuple]:
    """Canonical episodes that have never been queried."""
    result = graph.query(
        "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
        "AND coalesce(n.usage_count, 0) = 0 "
        "RETURN n.name AS name, n.created_at AS created "
        "ORDER BY n.created_at LIMIT $n",
        params={"n": n},
    )
    return list(result.result_set)


def query_stats(graph) -> dict:
    result = graph.query(
        "MATCH (n:Episodic) RETURN "
        "  count(n) AS total, "
        "  sum(coalesce(n.usage_count, 0)) AS total_usage, "
        "  sum(CASE WHEN coalesce(n.usage_count, 0) > 0 THEN 1 ELSE 0 END) AS used_count"
    )
    row = result.result_set[0] if result.result_set else (0, 0, 0)
    canonical_result = graph.query(
        "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
        "RETURN count(n) AS total, sum(coalesce(n.usage_count, 0)) AS used"
    )
    crow = canonical_result.result_set[0] if canonical_result.result_set else (0, 0)
    return {
        "total_episodes": int(row[0] or 0),
        "total_usage_events": int(row[1] or 0),
        "episodes_with_usage": int(row[2] or 0),
        "canonical_total": int(crow[0] or 0),
        "canonical_total_usage": int(crow[1] or 0),
    }


def render(stats: dict, top: list, promote: list, demote: list) -> str:
    L: list[str] = []
    p = L.append

    p(f"# kg-hub Usage Ranking — {datetime.now(tz=timezone.utc).isoformat()}")
    p("")
    p("Lindy / implicit-feedback ranking from the **PUSH hook** signal.")
    p("Each Claude Code SessionStart bumps `usage_count` on the canonical")
    p("episodes the hook chose to inject into that session.")
    p("")
    p("## TL;DR")
    p("")
    p("| Metric | Value |")
    p("|---|---|")
    p(f"| Total Episodic nodes | {stats['total_episodes']} |")
    p(f"| Episodes with any usage | "
      f"{stats['episodes_with_usage']} "
      f"({100*stats['episodes_with_usage']/max(stats['total_episodes'],1):.1f}%) |")
    p(f"| Total PUSH-hook usage events | {stats['total_usage_events']} |")
    p(f"| Canonical episodes | {stats['canonical_total']} "
      f"(total usage: {stats['canonical_total_usage']}) |")
    p("")

    p("## 1. Top 10 most-used canonical (Lindy signal)")
    p("")
    p("_The canonical docs that real sessions keep finding relevant._")
    p("")
    if top:
        p("| Name | Usage | Last used |")
        p("|---|---|---|")
        for name, uc, last in top:
            p(f"| `{name}` | {int(uc)} | {last or '—'} |")
    else:
        p("_(none — no canonical episode has been injected by the hook yet)_")
    p("")

    p("## 2. Promote candidates — non-canonical with usage > 0")
    p("")
    p("_Ordinary observations the PUSH hook chose over a canonical when the")
    p("canonical didn't yield a content-match. Consider re-ingesting these")
    p("via `skip_extraction=True` with a `kg-hub-canonical-*` name + Capsule label._")
    p("")
    if promote:
        p("| Name | Usage | Preview |")
        p("|---|---|---|")
        for name, uc, preview in promote:
            safe = (preview or "").replace("|", "\\|").replace("\n", " ").strip()[:80]
            p(f"| `{name}` | {int(uc)} | {safe} |")
    else:
        p("_(none — PUSH hook currently filters to canonical-first)_")
    p("")

    p("## 3. Demote candidates — canonical with 0 usage")
    p("")
    p("_Canonical docs no session has ever pulled. Either the project they")
    p("describe isn't being worked on, or their content is worded such that")
    p("the cwd-keyword match never fires. Consider archiving or restructuring._")
    p("")
    if demote:
        p("| Name | Created |")
        p("|---|---|")
        for name, created in demote:
            p(f"| `{name}` | {created or '—'} |")
    else:
        p("_(none — every canonical episode has at least 1 usage event)_")
    p("")

    p("---")
    p("")
    p("Generated by `tools/usage_ranking.py`. Surfaced weekly via `tools/weekly_report.py`.")
    p("")
    p("**Interpretation guide**:")
    p("- Table 1 rising over time = healthy: real work keeps using known docs")
    p("- Table 2 growing = PUSH hook is finding good non-canonical docs;")
    p("  promote them or expand canonical set")
    p("- Table 3 large = canonical set has dead weight; demote or rewrite")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true",
                    help="print to stdout only, do not write report file")
    args = ap.parse_args()

    try:
        graph = _connect()
    except Exception as exc:
        print(f"[usage_ranking] FATAL: cannot connect to FalkorDB — {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    try:
        stats = query_stats(graph)
        top = query_top_canonical_used(graph)
        promote = query_promote_candidates(graph)
        demote = query_demote_candidates(graph)
    except Exception as exc:
        print(f"[usage_ranking] query failed — {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    md = render(stats, top, promote, demote)
    print(md)

    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORT_DIR / f"usage-ranking-{datetime.now().strftime('%Y-%m-%d')}.md"
        out.write_text(md)
        print(f"\n[report] {out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
