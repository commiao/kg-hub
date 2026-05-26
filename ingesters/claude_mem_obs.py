"""
claude-mem observations ingester.

Reads pre-extracted observations from ~/.claude-mem/claude-mem.db (the local
SQLite that claude-mem's worker writes) and feeds each one to Graphiti as an
episode in the same FalkorDB graph ("kg_hub") that OpenClaw capsules go into.

Key design:
  * Single graph (group_id="kg_hub") — same bucket as OpenClaw capsules so
    cross-source queries Just Work. If we later want per-source filtering,
    we can use the source_description field or add a "source_kind" property.
  * Read-only on claude-mem.db (decision: don't touch claude-mem itself).
  * Watermark: separate file data/.ingested.claude_mem.json keyed by obs.id.
    Set --no-watermark to bypass for testing.

Observation schema (relevant fields):
    id INTEGER PK, project TEXT, type TEXT, title TEXT, subtitle TEXT,
    facts TEXT (JSON array), narrative TEXT, concepts TEXT (JSON array),
    files_read TEXT (JSON array), files_modified TEXT (JSON array),
    created_at TEXT (ISO), content_hash TEXT, generated_by_model TEXT.

Usage:
    python -m ingesters.claude_mem_obs --limit 10           # smoke test
    python -m ingesters.claude_mem_obs --limit 50           # minimal slice
    python -m ingesters.claude_mem_obs                      # all unprocessed
    python -m ingesters.claude_mem_obs --no-watermark       # ignore prior progress
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graphiti_core.nodes import EpisodeType  # noqa: E402

from graphiti_client import build_graphiti  # noqa: E402
from schema import ENTITY_TYPES, EDGE_TYPES, EDGE_TYPE_MAP  # noqa: E402
from utils.writer_lock import writer_lock, WriterLockBusy  # noqa: E402
from utils.wait_for_dependencies import wait_for_falkordb  # noqa: E402
from utils.ingest_filter import (  # noqa: E402
    load_config as load_filter_config,
    evaluate as evaluate_obs,
    log_decision,
    summarize_decisions,
    QuotaTracker,
)


CLAUDE_MEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
WATERMARK_PATH = Path(__file__).resolve().parent.parent / "data" / ".ingested.claude_mem.json"
GROUP_ID = "kg_hub"


def load_watermark() -> tuple[set[int], set[int]]:
    """Return (ingested_ids, rejected_ids). Rejected ones are skipped on next run
    so the filter doesn't re-score the same obs every 15 min."""
    if WATERMARK_PATH.exists():
        data = json.loads(WATERMARK_PATH.read_text())
        return (
            set(data.get("ingested_obs_ids", [])),
            set(data.get("rejected_obs_ids", [])),
        )
    return set(), set()


def save_watermark(ingested: set[int], rejected: set[int]) -> None:
    WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATERMARK_PATH.write_text(
        json.dumps(
            {
                "ingested_obs_ids": sorted(ingested),
                "rejected_obs_ids": sorted(rejected),
                "last_updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
            indent=2,
        )
    )


def fetch_observations(limit: int | None) -> list[dict]:
    """Return latest N observations as dicts (newest first).

    Joins sdk_sessions to pull platform_source so the ingest filter can
    apply per-platform thresholds. LEFT JOIN keeps legacy obs (predating
    sdk_sessions) ingestable with platform_source=NULL → filter falls
    back to _default profile."""
    if not CLAUDE_MEM_DB.exists():
        raise SystemExit(f"claude-mem db not found at {CLAUDE_MEM_DB}")
    conn = sqlite3.connect(f"file:{CLAUDE_MEM_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT o.id, o.project, o.type, o.title, o.subtitle, o.facts, o.narrative, "
        "o.concepts, o.files_read, o.files_modified, o.created_at, o.content_hash, "
        "o.generated_by_model, o.relevance_count, s.platform_source "
        "FROM observations o "
        "LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id "
        "ORDER BY o.created_at_epoch DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    conn.close()
    return rows


def _decode_json_list(field: str | None) -> list[str]:
    if not field:
        return []
    try:
        v = json.loads(field)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def build_episode_body(obs: dict) -> str:
    """Render a claude-mem obs row as a natural-language episode body."""
    parts: list[str] = []
    header = f"[{obs.get('type','obs').upper()}] {obs.get('title') or '(untitled)'}"
    parts.append(header)
    if obs.get("subtitle"):
        parts.append(obs["subtitle"])
    parts.append("")
    if obs.get("narrative"):
        parts.append("Narrative:")
        parts.append(obs["narrative"])
        parts.append("")
    facts = _decode_json_list(obs.get("facts"))
    if facts:
        parts.append("Key facts:")
        for f in facts:
            parts.append(f"- {f}")
        parts.append("")
    concepts = _decode_json_list(obs.get("concepts"))
    if concepts:
        parts.append(f"Concepts: {', '.join(concepts)}")
    files_modified = _decode_json_list(obs.get("files_modified"))
    if files_modified:
        parts.append(f"Files modified: {', '.join(files_modified[:20])}")
    files_read = _decode_json_list(obs.get("files_read"))
    if files_read:
        parts.append(f"Files read: {', '.join(files_read[:20])}")
    parts.append(f"Project: {obs.get('project','?')}")
    return "\n".join(parts)


def reference_time_from(obs: dict) -> datetime:
    iso = obs.get("created_at") or ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


async def ingest_one(g, obs: dict) -> tuple[int, int]:
    body = build_episode_body(obs)
    result = await g.add_episode(
        name=f"claude-mem-obs-{obs['id']}",
        episode_body=body,
        source=EpisodeType.text,
        source_description=f"claude-mem obs id={obs['id']} project={obs.get('project','?')} type={obs.get('type','?')}",
        reference_time=reference_time_from(obs),
        group_id=GROUP_ID,
        entity_types=ENTITY_TYPES,
        edge_types=EDGE_TYPES,
        edge_type_map=EDGE_TYPE_MAP,
    )
    return len(result.nodes), len(result.edges)


async def _do_main(args) -> int:
    all_rows = fetch_observations(limit=None)
    print(f"[discover] {len(all_rows)} total obs in claude-mem.db")

    if args.no_watermark:
        ingested, rejected = set(), set()
    else:
        ingested, rejected = load_watermark()
    print(f"[watermark] {len(ingested)} ingested, {len(rejected)} rejected (skipped)")

    seen = ingested | rejected
    todo = [r for r in all_rows if r["id"] not in seen]
    if args.limit > 0:
        todo = todo[: args.limit]
    print(f"[plan] {len(todo)} obs to evaluate this run")

    if not todo:
        print("[done] nothing new")
        return 0

    # --- Load filter config once per run (hot-reload on next invocation) ---
    filter_cfg = load_filter_config()
    shadow = bool(filter_cfg.get("shadow_mode", True))
    quotas = QuotaTracker()
    print(f"[filter] shadow_mode={shadow} (true = log only, do not block)")

    # --- Score all obs first; defer graphiti init until we know we need it ---
    decisions = []
    accept_list = []
    for obs in todo:
        d = evaluate_obs(obs, filter_cfg, quotas)
        decisions.append(d)
        log_decision(d)
        if d.accept:
            accept_list.append(obs)

    summary = summarize_decisions(decisions)
    print(
        f"[filter] evaluated={summary['n']} would_accept={summary['would_accept']} "
        f"would_reject={summary['would_reject']} reject_rate={summary['reject_rate_pct']}%"
    )
    print(f"[filter] by_layer={summary['by_layer']}")

    # If shadow mode, accept_list == todo. If real mode, accept_list excludes rejects.
    if not shadow:
        new_rejects = {d.obs_id for d in decisions if not d.would_accept}
        if new_rejects:
            rejected |= new_rejects
            save_watermark(ingested, rejected)
            print(f"[filter] {len(new_rejects)} obs added to rejected watermark")

    if not accept_list:
        print("[done] no obs to ingest after filter")
        return 0

    g = await build_graphiti(fresh=False)
    print("[graphiti] ready, backend=FalkorDB")

    total_nodes = total_edges = 0
    for i, obs in enumerate(accept_list, 1):
        try:
            n, e = await ingest_one(g, obs)
            total_nodes += n
            total_edges += e
            ingested.add(obs["id"])
            save_watermark(ingested, rejected)
            print(
                f"  [{i}/{len(accept_list)}] obs#{obs['id']} ({obs.get('project','?')}) "
                f"nodes={n} edges={e}  | {obs.get('title','')[:60]}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(accept_list)}] obs#{obs['id']} FAILED: {type(exc).__name__}: {exc}")
            continue

    print(
        f"\n[summary] ingested {len(accept_list)} obs — "
        f"raw nodes={total_nodes}, raw edges={total_edges}"
    )
    print(f"[watermark] {WATERMARK_PATH}")
    return 0


async def main() -> int:
    # Boot-race mitigation: wait for FalkorDB before doing anything else.
    # On Mac restart, Docker takes 10-30s to bring FalkorDB online; if this
    # script fires before then it would crash with ConnectionError and pollute
    # the watermark / log. Up to 60s grace, exit clean if still down.
    if not wait_for_falkordb(timeout_seconds=60.0):
        print("[fatal] FalkorDB not ready after 60s — exiting clean (next interval retries)")
        return 0

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="ingest at most N obs (0 = all)")
    ap.add_argument(
        "--no-watermark",
        action="store_true",
        help="ignore prior watermark; re-ingest from latest N regardless",
    )
    ap.add_argument(
        "--wait-seconds",
        type=float,
        default=0.0,
        help="seconds to wait for writer.lock before giving up (default 0 = fail fast)",
    )
    args = ap.parse_args()

    # Serialize against other writers (openclaw_capsule etc.) to prevent
    # entity-dedup race when both processes mention the same entity.
    try:
        with writer_lock(owner="claude_mem_obs", timeout_seconds=args.wait_seconds):
            return await _do_main(args)
    except WriterLockBusy as exc:
        print(f"[lock] {exc} — exiting clean (will retry next interval)")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
