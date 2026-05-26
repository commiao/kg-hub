"""
kg-hub stats — dump node / edge type distribution + sample causal chains.

Backend: FalkorDB (migrated 2026-05-17 from Kuzu).
  * Reads directly via FalkorDB client (not through graphiti) so we don't
    touch driver init paths during normal sessions.
  * Pure read-only — safe to run while ingest jobs are holding writer.lock.

Usage:
    python stats.py                # all sections
    python stats.py --chains       # additionally show 10 longest paths
    python stats.py --graph kg_hub # query a non-default graph name
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

# Load FalkorDB connection settings from ~/.claude-mem/.env
from dotenv import load_dotenv
load_dotenv(Path.home() / ".claude-mem" / ".env", override=True)

from falkordb import FalkorDB

sys.path.insert(0, str(Path(__file__).resolve().parent))


def open_graph(graph_name: str):
    host = os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1")
    port = int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379"))
    password = os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None
    db = FalkorDB(host=host, port=port, password=password)
    graphs = list(db.list_graphs())
    if graph_name not in graphs:
        sys.exit(f"graph {graph_name!r} not found; existing: {graphs}")
    return db.select_graph(graph_name)


def _rows(graph, cypher: str, **params):
    return graph.query(cypher, params).result_set


def node_stats(graph) -> None:
    print("=== Node type distribution ===")
    # FalkorDB applies schema entity types as actual node labels (in addition
    # to "Entity"). Pick the most-specific custom label per node.
    rows = _rows(graph, "MATCH (n:Entity) RETURN labels(n) AS ls")
    by_type: Counter[str] = Counter()
    for row in rows:
        labels = row[0] or []
        custom = [l for l in labels if l != "Entity"]
        by_type[custom[0] if custom else "Entity (unclassified)"] += 1
    total = sum(by_type.values())
    for t, c in by_type.most_common():
        pct = c * 100.0 / total if total else 0
        print(f"  {c:4d}  ({pct:4.1f}%)  {t}")
    print(f"  total: {total}")


def edge_stats(graph) -> None:
    print("\n=== Edge type distribution ===")
    # FalkorDB uses DIRECT edges (no RelatesToNode_ reification).
    rows = _rows(
        graph,
        "MATCH (:Entity)-[e:RELATES_TO]->(:Entity) RETURN e.name AS n",
    )
    edges: Counter[str] = Counter()
    for row in rows:
        edges[row[0] or "(unnamed)"] += 1
    total = sum(edges.values())
    from schema import EDGE_TYPES
    canonical = {n.upper() for n in EDGE_TYPES.keys()}
    for n, c in edges.most_common():
        mark = "✓" if n.upper() in canonical else "▲"
        pct = c * 100.0 / total if total else 0
        print(f"  {c:4d}  ({pct:4.1f}%)  {mark}  {n}")
    print(f"  total edges: {total}")
    matched = sum(c for n, c in edges.items() if n.upper() in canonical)
    if total:
        print(f"  v0.2 schema-aligned: {matched} ({100.0 * matched / total:.1f}%)")
        print(f"  LLM-coined         : {total - matched} ({100.0 * (total - matched) / total:.1f}%)")


def edge_dup_check(graph) -> None:
    """Detect UPPER/lower-case duplicates of the same logical edge."""
    print("\n=== Edge name duplicate audit (UPPER vs lower) ===")
    rows = _rows(graph, "MATCH (:Entity)-[e:RELATES_TO]->(:Entity) RETURN e.name AS n")
    by_lower: dict[str, Counter[str]] = {}
    for row in rows:
        n = row[0] or ""
        by_lower.setdefault(n.lower(), Counter())[n] += 1
    dups = {lo: cnt for lo, cnt in by_lower.items() if len(cnt) > 1}
    if not dups:
        print("  (none — clean)")
        return
    for lo, variants in sorted(dups.items()):
        print(f"  '{lo}':")
        for v, c in variants.most_common():
            print(f"      {c:4d}  {v}")


def episode_stats(graph) -> None:
    print("\n=== Episode count ===")
    rows = _rows(graph, "MATCH (e:Episodic) RETURN count(e) AS c")
    n = rows[0][0] if rows else 0
    print(f"  {n} episodes ingested")


def top_entities_by_degree(graph, k: int = 10) -> None:
    print(f"\n=== Top {k} entities by edge degree (hub-ness) ===")
    rows = _rows(
        graph,
        "MATCH (n:Entity)-[r:RELATES_TO]-(:Entity) "
        "WITH n, count(r) AS deg "
        "ORDER BY deg DESC LIMIT $k "
        "RETURN n.name AS name, labels(n) AS ls, deg",
        k=k,
    )
    for row in rows:
        labels = [l for l in (row[1] or []) if l != "Entity"] or ["—"]
        print(f"  deg={row[2]:3d}  [{labels[0]:18}]  {row[0]}")


def sample_chains(graph, k: int = 10) -> None:
    print(f"\n=== {k} sample multi-hop paths (Entity → ...4-6 hops...→ Entity) ===")
    rows = _rows(
        graph,
        "MATCH p = (a:Entity)-[:RELATES_TO*4..6]->(b:Entity) "
        "WHERE a.name <> b.name "
        "RETURN a.name AS src, b.name AS tgt LIMIT $k",
        k=k * 3,
    )
    seen: set[tuple[str, str]] = set()
    shown = 0
    for row in rows:
        pair = (row[0], row[1])
        if pair in seen:
            continue
        seen.add(pair)
        print(f"  {row[0]}  ─→…─→  {row[1]}")
        shown += 1
        if shown >= k:
            break


def watermark_summary() -> None:
    print("\n=== Watermarks ===")
    data_dir = Path(__file__).parent / "data"
    openclaw_wm = data_dir / ".ingested.json"
    claude_mem_wm = data_dir / ".ingested.claude_mem.json"

    if openclaw_wm.exists():
        wm = json.load(open(openclaw_wm))
        raw_n = sum(v.get("nodes", 0) for v in wm.values())
        raw_e = sum(v.get("edges", 0) for v in wm.values())
        print(f"  openclaw    files={len(wm):4d}  raw_nodes={raw_n}  raw_edges={raw_e}")

    if claude_mem_wm.exists():
        wm = json.load(open(claude_mem_wm))
        ids = wm.get("ingested_obs_ids", [])
        print(f"  claude-mem  obs={len(ids):4d}  last_updated={wm.get('last_updated','?')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", default="kg_hub", help="FalkorDB graph name (default: kg_hub)")
    ap.add_argument("--chains", action="store_true", help="also show sample multi-hop paths")
    args = ap.parse_args()

    graph = open_graph(args.graph)
    node_stats(graph)
    edge_stats(graph)
    edge_dup_check(graph)
    episode_stats(graph)
    top_entities_by_degree(graph)
    if args.chains:
        sample_chains(graph)
    watermark_summary()


if __name__ == "__main__":
    main()
