"""
Normalize edge name casing in FalkorDB (kg_hub graph).

Problem (found by stats.py 2026-05-17 audit):
    The LLM extracts the same logical edge with inconsistent casing —
    e.g. REFERENCES (59 edges) and references (20 edges) coexist as 79
    edges when they should be one canonical 'references' with 79 edges.
    12 such pairs detected; ~230 edges are duplicates.

Strategy:
    1. Query all distinct e.name values
    2. Group by .lower() — any group with >1 distinct casing = dup
    3. For each dup group, pick the lowercase variant as canonical
       (matches schema.py EDGE_TYPES which uses snake_case lowercase)
    4. Rename non-canonical variants to canonical

Safety:
    * Dry-run mode default (use --apply to actually write)
    * Live mode acquires writer.lock — serializes against any ingest
    * Only touches edges where >1 case variant exists; pure UPPER-only or
      pure-lower-only edges are left alone (they may be LLM-coined names
      that don't have schema equivalents but are still meaningful)

Usage:
    python -m tools.normalize_edge_names                # dry-run, default
    python -m tools.normalize_edge_names --apply        # actually rename
    python -m tools.normalize_edge_names --apply --wait-seconds 1800  # wait for lock
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path.home() / ".claude-mem" / ".env", override=True)

from falkordb import FalkorDB

from utils.writer_lock import writer_lock, WriterLockBusy


def open_graph(graph_name: str = "kg_hub"):
    host = os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1")
    port = int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379"))
    password = os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None
    db = FalkorDB(host=host, port=port, password=password)
    graphs = list(db.list_graphs())
    if graph_name not in graphs:
        sys.exit(f"graph {graph_name!r} not found; existing: {graphs}")
    return db.select_graph(graph_name)


def find_dup_groups(graph) -> list[tuple[str, dict[str, int]]]:
    """Return [(canonical_lower, {variant_name: count})] where len(variants) > 1."""
    rows = graph.query(
        "MATCH ()-[e:RELATES_TO]->() RETURN e.name AS n, count(e) AS c"
    ).result_set
    by_lower: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows:
        name = row[0] or ""
        cnt = int(row[1])
        if not name:
            continue
        by_lower[name.lower()][name] = cnt
    return [(lo, variants) for lo, variants in by_lower.items() if len(variants) > 1]


def report(dups: list[tuple[str, dict[str, int]]]) -> None:
    if not dups:
        print("[clean] no edge name case dups — graph is already normalized")
        return
    print(f"[found] {len(dups)} dup groups:")
    total_to_rename = 0
    for canonical, variants in sorted(dups, key=lambda x: -sum(x[1].values())):
        print(f"  → canonical: {canonical}")
        for variant, cnt in sorted(variants.items(), key=lambda x: -x[1]):
            tag = "  KEEP" if variant == canonical else "RENAME"
            print(f"      [{tag}] {cnt:4d}  {variant}")
            if variant != canonical:
                total_to_rename += cnt
    print(f"\n[plan] {total_to_rename} edges to rename across {len(dups)} groups")


def normalize(graph, dups: list[tuple[str, dict[str, int]]]) -> int:
    total = 0
    for canonical, variants in dups:
        for variant, _expected_cnt in variants.items():
            if variant == canonical:
                continue
            res = graph.query(
                "MATCH ()-[e:RELATES_TO]->() WHERE e.name = $v "
                "SET e.name = $c RETURN count(e) AS c",
                {"v": variant, "c": canonical},
            )
            renamed = res.result_set[0][0] if res.result_set else 0
            total += renamed
            print(f"  [renamed] {variant:30s} → {canonical:30s}  ({renamed} edges)")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually write changes (default = dry-run)",
    )
    ap.add_argument(
        "--wait-seconds",
        type=float,
        default=0.0,
        help="seconds to wait for writer.lock before giving up (apply mode only)",
    )
    args = ap.parse_args()

    graph = open_graph()
    dups = find_dup_groups(graph)
    report(dups)

    if not dups:
        return 0

    if not args.apply:
        print("\n[dry-run] no changes made. Re-run with --apply to commit.")
        return 0

    try:
        with writer_lock(owner="normalize_edge_names", timeout_seconds=args.wait_seconds):
            print("\n[locked] writer.lock acquired — applying renames")
            total = normalize(graph, dups)
            print(f"\n[done] renamed {total} edges total")
    except WriterLockBusy as exc:
        print(f"[lock] {exc} — re-run later or with --wait-seconds > 0")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
