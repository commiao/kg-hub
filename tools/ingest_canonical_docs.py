"""
kg-hub canonical docs ingester — one-shot, idempotent.

Reads kg-hub's own design / architectural docs (DESIGN.md, ROADMAP.md,
PHASE-3-REPORT.md, OBSERVATION-PHASE.md, ...) and pushes each into the
graph as one episode. graphiti's LLM-driven entity extraction will then
materialize them as Concept / Issue / Lesson / Capsule nodes per the
schema in schema.py.

Why this exists: as of 2026-05-28 the graph contained 0 episodes carrying
canonical-source content. claude-mem captures *session summaries*, not
*source documents*, so DESIGN.md's 5 痛点 etc. were unreachable. This
script closes that hole.

Idempotency: SHA-256 watermark at data/.ingested.canonical.json — re-running
only re-processes changed files.

Deliberately NOT on a launchd cron — canonical docs are stable, you re-run
this manually after editing them.

Usage:
    python -m tools.ingest_canonical_docs            # all canonical docs
    python -m tools.ingest_canonical_docs --list     # show what would be ingested
    python -m tools.ingest_canonical_docs --fresh    # ignore watermark, force re-ingest
    python -m tools.ingest_canonical_docs --only DESIGN.md
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graphiti_core.nodes import EpisodeType  # noqa: E402

from graphiti_client import build_graphiti  # noqa: E402
from schema import ENTITY_TYPES, EDGE_TYPES, EDGE_TYPE_MAP  # noqa: E402
from utils.writer_lock import writer_lock, WriterLockBusy  # noqa: E402
from utils.wait_for_dependencies import wait_for_falkordb  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
WATERMARK_PATH = REPO_ROOT / "data" / ".ingested.canonical.json"
GROUP_ID = "kg_hub"


# The canonical document registry. Add new docs here, re-run script.
# `name` is the episode's stable identifier (used for graphiti dedup).
# `desc` shows up in queries via source_description, so make it clear
# this is canonical, not session noise.
CANONICAL_DOCS = [
    {
        "path": "DESIGN.md",
        "name": "kg-hub-canonical-DESIGN",
        "desc": "kg-hub-canonical: DESIGN.md — locked architecture decisions and project motivation",
    },
    {
        "path": "ROADMAP.md",
        "name": "kg-hub-canonical-ROADMAP",
        "desc": "kg-hub-canonical: ROADMAP.md — 4-phase roadmap with gates",
    },
    {
        "path": "docs/PHASE-3-REPORT.md",
        "name": "kg-hub-canonical-PHASE-3-REPORT",
        "desc": "kg-hub-canonical: PHASE-3-REPORT.md — phase 3 delivery report",
    },
    {
        "path": "docs/OBSERVATION-PHASE.md",
        "name": "kg-hub-canonical-OBSERVATION-PHASE",
        "desc": "kg-hub-canonical: OBSERVATION-PHASE.md — ingest filter shadow-phase guide",
    },
    {
        "path": "docs/ONBOARDING.md",
        "name": "kg-hub-canonical-ONBOARDING",
        "desc": "kg-hub-canonical: ONBOARDING.md — self-service integration guide for new sources",
    },
]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_watermark() -> dict[str, dict]:
    if WATERMARK_PATH.exists():
        return json.loads(WATERMARK_PATH.read_text())
    return {}


def save_watermark(wm: dict[str, dict]) -> None:
    WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATERMARK_PATH.write_text(json.dumps(wm, indent=2, ensure_ascii=False))


def doc_reference_time(path: Path) -> datetime:
    """Use file mtime — matches openclaw_capsule.py convention."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


async def ingest_one(g, doc: dict, repo_root: Path) -> tuple[int, int]:
    path = repo_root / doc["path"]
    body = path.read_text(encoding="utf-8")
    ref = doc_reference_time(path)
    # skip_extraction=True bypasses LLM entity/edge extraction.
    # Canonical docs go in as Episodic nodes verbatim — queryable via
    # kg_episode_search but no auto-extracted entities/relations.
    # This is a deliberate design choice: ~0.03s per doc vs ~30-50 min for
    # full extraction (see docs/BUG-add-episode-throughput.md). When/if the
    # graphiti throughput issue is fixed upstream, change this to False to
    # opt into automatic structuring.
    result = await g.add_episode(
        name=doc["name"],
        episode_body=body,
        source=EpisodeType.text,
        source_description=doc["desc"],
        reference_time=ref,
        group_id=GROUP_ID,
        entity_types=ENTITY_TYPES,
        edge_types=EDGE_TYPES,
        edge_type_map=EDGE_TYPE_MAP,
        skip_extraction=True,
    )
    return len(result.nodes), len(result.edges)


async def _do_main(args) -> int:
    # Filter the registry per CLI args
    docs = CANONICAL_DOCS
    if args.only:
        docs = [d for d in docs if d["path"] == args.only or d["name"] == args.only]
        if not docs:
            print(f"[error] --only {args.only!r} not in registry. Known:")
            for d in CANONICAL_DOCS:
                print(f"  - {d['path']}")
            return 1

    if args.list:
        print("Canonical docs registry:")
        for d in docs:
            p = REPO_ROOT / d["path"]
            present = "✓" if p.exists() else "✗ MISSING"
            print(f"  {present}  {d['path']:35s} → episode={d['name']}")
        return 0

    # Pre-flight: which docs need ingest?
    if args.fresh and WATERMARK_PATH.exists():
        WATERMARK_PATH.unlink()
    wm = load_watermark()

    todo = []
    for d in docs:
        p = REPO_ROOT / d["path"]
        if not p.exists():
            print(f"  [skip] {d['path']} — file not found")
            continue
        h = sha256_of(p)
        if wm.get(d["name"], {}).get("sha256") == h:
            print(f"  [unchanged] {d['name']}")
            continue
        todo.append((d, h))

    if not todo:
        print("[done] nothing to ingest (all up to date)")
        return 0

    print(f"[plan] {len(todo)} canonical doc(s) to ingest")

    g = await build_graphiti(fresh=False)
    print("[graphiti] ready")

    total_n = total_e = 0
    for i, (doc, h) in enumerate(todo, 1):
        path = REPO_ROOT / doc["path"]
        size_kb = path.stat().st_size / 1024
        print(f"  [{i}/{len(todo)}] {doc['name']}  ({size_kb:.1f} KB)  — extracting...")
        try:
            n, e = await ingest_one(g, doc, REPO_ROOT)
            total_n += n
            total_e += e
            wm[doc["name"]] = {
                "sha256": h,
                "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
                "nodes": n,
                "edges": e,
                "source_path": doc["path"],
            }
            save_watermark(wm)
            print(f"      ✓ nodes={n} edges={e}")
        except Exception as exc:  # noqa: BLE001
            print(f"      ✗ FAILED: {type(exc).__name__}: {exc}")
            continue

    print(f"\n[summary] ingested {len(todo)} canonical doc(s) — nodes={total_n} edges={total_e}")
    print(f"[watermark] {WATERMARK_PATH}")
    return 0


async def main() -> int:
    if not wait_for_falkordb(timeout_seconds=60.0):
        print("[fatal] FalkorDB not ready after 60s")
        return 1

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="store_true",
                    help="show registry, don't ingest")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore watermark, force re-ingest of all")
    ap.add_argument("--only", type=str, default=None,
                    help="ingest only this path or episode name")
    args = ap.parse_args()

    # No writer_lock: skip_extraction=True means no Entity dedup runs, so
    # canonical_docs cannot collide with claude_mem_obs / openclaw_capsule
    # entity races. The lock exists to serialize entity-dedup writers, not
    # pure Episodic inserts. Skipping it lets us run alongside the 15-min cron.
    return await _do_main(args)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
