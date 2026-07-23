"""
OpenClaw capsule ingester.

Reads `notes/capsules/*.md` from an OpenClaw snapshot directory and feeds each
capsule into Graphiti as one episode, constrained by v0.2 schema (ENTITY_TYPES /
EDGE_TYPES / EDGE_TYPE_MAP).

Watermark: kg-hub/data/.ingested.json tracks (file_path → sha256, ingested_at)
so re-running only re-processes new/changed capsules.

Usage:
    python -m ingesters.openclaw_capsule \
        --snapshot data/openclaw-snapshot-2026-05-14 \
        --limit 5         # ingest at most 5 capsules (for smoke test)
        --fresh           # wipe existing graph (FalkorDB `default_db`) first

Backend: FalkorDB via Docker container kg-hub-falkordb (migrated 2026-05-15).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# add kg-hub root to path so `schema` and `graphiti_client` import cleanly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graphiti_core.nodes import EpisodeType  # noqa: E402

from graphiti_client import build_graphiti  # noqa: E402
from schema import ENTITY_TYPES, EDGE_TYPES, EDGE_TYPE_MAP  # noqa: E402
from utils.writer_lock import writer_lock, WriterLockBusy  # noqa: E402
from utils.wait_for_dependencies import wait_for_falkordb  # noqa: E402


WATERMARK_PATH = Path(__file__).resolve().parent.parent / "data" / ".ingested.json"


def load_watermark() -> dict[str, dict]:
    if WATERMARK_PATH.exists():
        return json.loads(WATERMARK_PATH.read_text())
    return {}


def save_watermark(wm: dict[str, dict]) -> None:
    WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATERMARK_PATH.write_text(json.dumps(wm, indent=2, ensure_ascii=False))


def capsule_bytes(path: Path) -> bytes:
    """Raw capsule content. OpenClaw gzips aging capsules in place
    (CAPSULE-x.md → CAPSULE-x.md.gz) — read through the compression so a
    capsule gzipped before its first successful ingest isn't silently lost
    (this happened to the whole 07-17/07-18 batch).

    Decompression is decided by gzip magic bytes, not filename: OpenClaw
    occasionally writes plain markdown under a .md.gz name (seen in prod:
    capsule-daily-extract-20260623.md.gz) — treat those as text, don't drop.
    """
    raw = path.read_bytes()
    if path.name.endswith(".gz"):
        if raw[:2] == b"\x1f\x8b":
            import gzip
            return gzip.decompress(raw)
        print(f"  [warn] {path.name}: .gz name but not gzip payload — treating as plain text")
    return raw


def sha256_of(path: Path) -> str:
    # Hash the *decompressed* content: same capsule keeps the same identity
    # across a later .md → .md.gz rename, so it won't re-ingest as a duplicate.
    return hashlib.sha256(capsule_bytes(path)).hexdigest()


def discover_capsules(snapshot_dir: Path, min_size: int = 1500) -> list[Path]:
    """Find all capsule markdown files anywhere in the snapshot.

    OpenClaw scatters capsules across many top-level subdirs of /home/admin/clawd:
        notes/      — notes/capsules/, notes/standards/, notes/plans/ (archive)
        memory/     — memory/archive/<month>/ (historical extracted capsules)
        plans/      — top-level live planning capsules
        reports/    — top-level live reporting capsules
        capsules/   — top-level explicit CAPSULE-* files
    Walk each that exists, recursively, looking for *.md whose name starts with
    capsule- or CAPSULE-.

    Skip files smaller than `min_size` bytes (defaults to 1500) — those are
    typically "marked_for_deletion" stubs with empty body, worthless for KG.
    """
    SEARCH_ROOTS = ["notes", "memory", "plans", "reports", "capsules"]
    roots = [snapshot_dir / r for r in SEARCH_ROOTS if (snapshot_dir / r).exists()]
    if not roots:
        raise FileNotFoundError(
            f"none of {SEARCH_ROOTS} exist under {snapshot_dir} — "
            "did you pull the OpenClaw snapshot first?"
        )

    candidates: list[Path] = []
    for root in roots:
        for pattern in ("*.md", "*.md.gz"):
            for p in root.rglob(pattern):
                name = p.name
                # Capsule filename heuristic: starts with CAPSULE- or capsule-
                if name.startswith("CAPSULE-") or name.startswith("capsule-"):
                    candidates.append(p)

    # Dedup by resolved path (some files appear under both absolute-path
    # and relative-path extraction sites after recursive untarring).
    seen: dict[str, Path] = {}
    for p in candidates:
        try:
            key = capsule_bytes(p)[:200].decode("utf-8", "ignore")
        except Exception:
            key = str(p)
        if key not in seen:
            seen[key] = p
    deduped = sorted(seen.values())

    # Filter trivially-empty stubs (size of the *content*, not the .gz)
    sized = []
    for p in deduped:
        try:
            if len(capsule_bytes(p)) >= min_size:
                sized.append(p)
        except Exception as exc:  # noqa: BLE001 — truncated gz / IO error:
            # skip so discovery survives, but never silently (silent loss is
            # exactly what this ingester exists to prevent).
            print(f"  [skip] {p.name}: unreadable capsule ({type(exc).__name__}: {exc})")
            continue
    return sized


def capsule_reference_time(path: Path) -> datetime:
    """Use file mtime as reference_time. (Future: parse front-matter `created` field.)"""
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def episode_name_from_path(path: Path) -> str:
    name = path.name
    for suf in (".md.gz", ".md"):  # CAPSULE-x.md.gz 的 stem 是 CAPSULE-x.md,要剥干净
        if name.endswith(suf):
            return f"openclaw-capsule-{name[: -len(suf)]}"
    return f"openclaw-capsule-{path.stem}"


# Project-wide group_id. graphiti's FalkorDriver has a broken default of "\_"
# (rejected by its own validate_group_id). Explicitly set a legal group_id to
# (a) bypass the upstream bug and (b) prepare for Phase 2 multi-source filtering
# (claude_mem_obs / openclaw_memory / openclaw_kb will use sibling group_ids).
GROUP_ID = "kg_hub"


async def ingest_one(g, path: Path, source_desc: str, ref_time: datetime) -> tuple[int, int]:
    body = capsule_bytes(path).decode("utf-8")
    result = await g.add_episode(
        name=episode_name_from_path(path),
        episode_body=body,
        source=EpisodeType.text,
        source_description=source_desc,
        reference_time=ref_time,
        group_id=GROUP_ID,
        entity_types=ENTITY_TYPES,
        edge_types=EDGE_TYPES,
        edge_type_map=EDGE_TYPE_MAP,
    )
    return len(result.nodes), len(result.edges)


async def _do_main(args) -> int:
    snapshot = args.snapshot.resolve()
    files = discover_capsules(snapshot)
    print(f"[discover] {len(files)} capsule files in {snapshot}")

    if args.fresh and WATERMARK_PATH.exists():
        WATERMARK_PATH.unlink()
    wm = load_watermark()

    # filter to new/changed only。同内容跨路径也跳过(sha 集合):
    # OpenClaw 会把已 ingest 的 CAPSULE-x.md 原地 gzip 成 .md.gz——路径变了
    # 内容没变,不能重复入图。
    known_shas = {v.get("sha256") for v in wm.values()}
    todo: list[Path] = []
    aliased = 0
    for f in files:
        key = str(f.relative_to(snapshot))
        h = sha256_of(f)
        if wm.get(key, {}).get("sha256") == h:
            continue
        if h in known_shas:
            wm[key] = {"sha256": h, "ingested_at": "dedup-alias",
                       "nodes": 0, "edges": 0}
            aliased += 1
            continue
        todo.append(f)
    if aliased:
        save_watermark(wm)  # persist aliases even when todo ends up empty
        print(f"[plan] {aliased} renamed-but-unchanged capsules aliased (skip re-ingest)")
    print(f"[plan] {len(todo)} new/changed capsules to ingest ({len(files) - len(todo)} unchanged)")

    if args.limit > 0:
        todo = todo[: args.limit]
        print(f"[plan] --limit {args.limit} → trimming to {len(todo)}")

    if not todo:
        print("[done] nothing to do")
        return 0

    g = await build_graphiti(fresh=args.fresh)
    print("[graphiti] ready, backend=FalkorDB")

    total_nodes = total_edges = 0
    for i, f in enumerate(todo, 1):
        key = str(f.relative_to(snapshot))
        h = sha256_of(f)
        ref = capsule_reference_time(f)
        try:
            n, e = await ingest_one(
                g,
                f,
                source_desc=f"openclaw-snapshot: {key}",
                ref_time=ref,
            )
            total_nodes += n
            total_edges += e
            wm[key] = {
                "sha256": h,
                "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
                "nodes": n,
                "edges": e,
            }
            save_watermark(wm)  # persist after each success
            print(f"  [{i}/{len(todo)}] {key}  nodes={n} edges={e}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(todo)}] {key}  FAILED: {type(exc).__name__}: {exc}")
            # keep going; failures are recorded by absence from watermark
            continue

    print(
        f"\n[summary] ingested {len(todo)} capsules — "
        f"raw nodes={total_nodes}, raw edges={total_edges}"
    )
    print(f"[watermark] {WATERMARK_PATH}")
    return 0


async def main() -> int:
    # Boot-race mitigation (same as claude_mem_obs.py): wait for FalkorDB.
    if not wait_for_falkordb(timeout_seconds=60.0):
        print("[fatal] FalkorDB not ready after 60s — exiting clean (next interval retries)")
        return 0

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help="path to extracted OpenClaw snapshot (containing notes/capsules/)",
    )
    ap.add_argument("--limit", type=int, default=0, help="ingest at most N capsules (0 = all)")
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="wipe FalkorDB graph (default_db) + watermark first",
    )
    args = ap.parse_args()

    # Serialize against other writers (claude_mem_obs etc.) to prevent
    # entity-dedup race when both processes mention the same entity.
    try:
        with writer_lock(owner="openclaw_capsule"):
            return await _do_main(args)
    except WriterLockBusy as exc:
        print(f"[lock] {exc} — exiting clean (will retry next interval)")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
