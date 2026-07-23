"""tools/tag_provenance.py — tag OpenClaw capsule nodes with provenance.

Walks every `openclaw-capsule-*` Episodic node in FalkorDB, classifies its
来源 line via utils.provenance, and sets:

    n.provenance = firsthand | external-article | external-community
    n.verified   = false        (external only; never clobbers a human tag)

Classifying from *node content* (not snapshot files) matters: OpenClaw gzips
old capsules on the VPS, so the 07-17/07-18 batches no longer exist as .md in
the snapshot — but their nodes are in the graph and still need tagging.

Idempotent: nodes with provenance already set are skipped (--force re-tags).
Run standalone or via sync_openclaw.py (which invokes it after each ingest).

Usage:
    python -m tools.tag_provenance             # tag untagged capsule nodes
    python -m tools.tag_provenance --dry-run   # classify + report, no writes
    python -m tools.tag_provenance --force     # re-tag all (after rule change)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

from utils.provenance import classify_provenance  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="classify + report only, no writes")
    ap.add_argument("--force", action="store_true", help="re-tag nodes that already have provenance")
    args = ap.parse_args()

    from falkordb import FalkorDB  # deferred: import cost + optional dep

    db = FalkorDB(
        host=os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1"),
        port=int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379")),
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
    )
    g = db.select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))

    where = "" if args.force else "AND n.provenance IS NULL "
    rows = g.query(
        "MATCH (n:Episodic) WHERE n.name STARTS WITH 'openclaw-capsule-' "
        + where +
        "RETURN n.uuid, n.name, n.content"
    ).result_set
    print(f"[tag_provenance] {len(rows)} capsule nodes to classify"
          f"{' (force)' if args.force else ''}")
    if not rows:
        print("[done] nothing to do")
        return 0

    counts: dict[str, int] = {}
    for uuid, name, content in rows:
        prov = classify_provenance(content or "")
        counts[prov] = counts.get(prov, 0) + 1
        if args.dry_run:
            print(f"  [dry] {prov:<20} {name}")
            continue
        if prov.startswith("external"):
            # verified 只在未被人工打过时置 false,不覆盖人工判定
            g.query(
                "MATCH (n:Episodic {uuid: $u}) "
                "SET n.provenance = $p, n.verified = coalesce(n.verified, false)",
                params={"u": uuid, "p": prov},
            )
        else:
            g.query(
                "MATCH (n:Episodic {uuid: $u}) SET n.provenance = $p",
                params={"u": uuid, "p": prov},
            )

    verb = "would tag" if args.dry_run else "tagged"
    print(f"[summary] {verb}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
