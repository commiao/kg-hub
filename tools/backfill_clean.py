"""
kg-hub backfill cleanup (dry-run by default).

Apply the live ingest filter retroactively to every claude-mem episode
already in kg-hub. Produces a markdown report listing episodes that
WOULD be removed if the filter were retro-applied, grouped by reason.

Default mode is dry-run: read-only against FalkorDB; writes nothing.
The `--apply` flag is intentionally not implemented in this revision —
the report alone is the first deliverable. Apply mode will land in a
separate change, gated on report review.

Output:
  - stdout: condensed summary
  - file:   ~/.kg-hub/reports/backfill-dryrun-YYYY-MM-DD.md
  - file:   ~/.kg-hub/reports/backfill-dryrun-YYYY-MM-DD.jsonl
            (one record per scored episode, full breakdown)

Usage:
  python -m tools.backfill_clean              # dry-run, write reports
  python -m tools.backfill_clean --no-write   # stdout only

What the report answers:
  - How many already-ingested episodes would today's filter reject?
  - Breakdown by layer, type, platform, project
  - Score distribution histogram
  - Per-episode list (top N + jsonl with all)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path.home() / ".claude-mem" / ".env", override=True)

from falkordb import FalkorDB  # noqa: E402
from utils.ingest_filter import (  # noqa: E402
    load_config as load_filter_config,
    evaluate as evaluate_obs,
)


CLAUDE_MEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
WATERMARK_PATH = Path(__file__).resolve().parent.parent / "data" / ".ingested.claude_mem.json"
REPORT_DIR = Path.home() / ".kg-hub" / "reports"


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _falkor_graph():
    db = FalkorDB(
        host=os.environ["KG_HUB_FALKORDB_HOST"],
        port=int(os.environ["KG_HUB_FALKORDB_PORT"]),
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
    )
    return db.select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))


def load_already_ingested() -> set[int]:
    if not WATERMARK_PATH.exists():
        return set()
    return set(json.loads(WATERMARK_PATH.read_text()).get("ingested_obs_ids", []))


def fetch_ingested_obs(ids: set[int]) -> list[dict]:
    """Pull the full obs rows for every id in the watermark, joining
    sdk_sessions for platform_source. Same column set as the live ingester."""
    if not ids:
        return []
    conn = sqlite3.connect(f"file:{CLAUDE_MEM_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # SQLite parameter limit is 999 by default; chunk if needed
    rows: list[dict] = []
    ids_list = sorted(ids)
    CHUNK = 800
    for i in range(0, len(ids_list), CHUNK):
        chunk = ids_list[i:i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        sql = (
            f"SELECT o.id, o.project, o.type, o.title, o.subtitle, o.facts, o.narrative, "
            f"o.concepts, o.files_read, o.files_modified, o.created_at, o.content_hash, "
            f"o.generated_by_model, o.relevance_count, s.platform_source "
            f"FROM observations o "
            f"LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id "
            f"WHERE o.id IN ({placeholders})"
        )
        rows.extend(dict(r) for r in conn.execute(sql, chunk).fetchall())
    conn.close()
    return rows


def fetch_episode_impact(obs_ids: list[int]) -> dict[int, dict]:
    """For each obs_id, find the corresponding Episodic node in FalkorDB
    and count entities it mentions. This is the 'blast radius' if we delete.

    Episode name format from ingester: 'claude-mem-obs-{id}'
    """
    if not obs_ids:
        return {}
    g = _falkor_graph()
    impact: dict[int, dict] = {}

    # FalkorDB doesn't love huge IN lists either; chunk
    CHUNK = 500
    for i in range(0, len(obs_ids), CHUNK):
        chunk = obs_ids[i:i + CHUNK]
        names = [f"claude-mem-obs-{x}" for x in chunk]
        q = """
            UNWIND $names AS nm
            OPTIONAL MATCH (e:Episodic {name: nm})
            OPTIONAL MATCH (e)-[r:MENTIONS]->(n:Entity)
            WITH nm, e, count(n) AS mentions
            RETURN nm, e IS NOT NULL AS exists, mentions
        """
        result = g.query(q, params={"names": names}).result_set
        for nm, exists, mentions in result:
            obs_id = int(nm.replace("claude-mem-obs-", ""))
            impact[obs_id] = {"exists_in_kg": bool(exists), "mentions": int(mentions)}
    return impact


def histogram_buckets(scores: list[float]) -> dict[str, int]:
    buckets = {
        "0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0,
        "80-99": 0, "100-149": 0, "150+": 0,
    }
    for s in scores:
        if s < 20:
            buckets["0-19"] += 1
        elif s < 40:
            buckets["20-39"] += 1
        elif s < 60:
            buckets["40-59"] += 1
        elif s < 80:
            buckets["60-79"] += 1
        elif s < 100:
            buckets["80-99"] += 1
        elif s < 150:
            buckets["100-149"] += 1
        else:
            buckets["150+"] += 1
    return buckets


def render_report(scored: list[dict], impact: dict[int, dict], cfg: dict) -> str:
    rejects = [r for r in scored if not r["would_accept"]]
    accepts = [r for r in scored if r["would_accept"]]

    by_layer = Counter(r["layer"] for r in rejects)
    by_type = Counter(r["obs_type"] for r in rejects)
    by_platform = Counter(r["platform"] for r in rejects)
    by_project = Counter(r["project"] for r in rejects)

    score_hist_all = histogram_buckets([r["score"] for r in scored])
    score_hist_reject = histogram_buckets([r["score"] for r in rejects])

    # Total mentions that would disappear (upper bound — assumes 0 cleanup of shared entities)
    total_mentions_blast = sum(
        impact.get(r["obs_id"], {}).get("mentions", 0) for r in rejects
    )
    # Of the rejected obs, how many actually have a corresponding episode in KG
    rejects_in_kg = sum(
        1 for r in rejects if impact.get(r["obs_id"], {}).get("exists_in_kg")
    )

    L = []
    p = L.append

    p(f"# kg-hub Backfill Cleanup — DRY RUN — {now_iso()}")
    p("")
    p("Retro-applied the current `config/ingest_filter.json` to every already-ingested")
    p("claude-mem episode. **Nothing was modified.** This report shows what cleanup")
    p("would look like if you flipped `--apply` (intentionally not implemented yet).")
    p("")
    p("## TL;DR")
    p("")
    p(f"- Scored episodes: **{len(scored)}**")
    p(f"- Would accept (keep): **{len(accepts)}** ({100*len(accepts)/max(len(scored),1):.1f}%)")
    p(f"- Would reject (remove): **{len(rejects)}** ({100*len(rejects)/max(len(scored),1):.1f}%)")
    p(f"- Of those rejected, present in FalkorDB right now: **{rejects_in_kg}**")
    p(f"- Total MENTIONS edges that would disappear (upper bound): **{total_mentions_blast}**")
    p("")
    p("## Reject reasons by layer")
    p("")
    p("| Layer | Count |")
    p("|---|---|")
    for layer, n in by_layer.most_common():
        p(f"| {layer} | {n} |")
    p("")
    p("## Reject by type")
    p("")
    p("| Type | Count |")
    p("|---|---|")
    for t, n in by_type.most_common():
        p(f"| {t} | {n} |")
    p("")
    p("## Reject by platform")
    p("")
    p("| Platform | Count |")
    p("|---|---|")
    for plat, n in by_platform.most_common():
        p(f"| {plat} | {n} |")
    p("")
    p("## Reject by project (top 10)")
    p("")
    p("| Project | Count |")
    p("|---|---|")
    for proj, n in by_project.most_common(10):
        p(f"| {proj} | {n} |")
    p("")
    p("## Score distribution")
    p("")
    p("| Bucket | All | Rejects |")
    p("|---|---|---|")
    for b in score_hist_all:
        p(f"| {b} | {score_hist_all[b]} | {score_hist_reject[b]} |")
    p("")

    # Sample rejected episodes — 20 lowest-scoring + a few borderline
    sorted_rejects = sorted(rejects, key=lambda r: r["score"])
    p("## Sample rejected episodes (20 lowest-scoring)")
    p("")
    p("| obs_id | score | type | platform | project | layer | reason |")
    p("|---|---|---|---|---|---|---|")
    for r in sorted_rejects[:20]:
        reasons = (r["reasons"] or ["—"])[0][:50]
        p(f"| {r['obs_id']} | {r['score']} | {r['obs_type']} | {r['platform']} | "
          f"{r['project']} | {r['layer']} | {reasons} |")
    p("")
    p("## Borderline rejects (score 30-39, just under threshold)")
    p("")
    p("| obs_id | score | type | platform | project |")
    p("|---|---|---|---|---|")
    borderline = sorted(
        [r for r in rejects if 30 <= r["score"] < 40 and r["layer"] == "score"],
        key=lambda r: -r["score"],
    )
    for r in borderline[:15]:
        p(f"| {r['obs_id']} | {r['score']} | {r['obs_type']} | {r['platform']} | {r['project']} |")
    p("")
    p("---")
    p(f"Generated by `tools/backfill_clean.py` at {now_iso()}.")
    p("")
    p(f"Filter config used: shadow_mode={cfg.get('shadow_mode')}, ")
    p(f"version={cfg.get('version')}")
    p("")
    p("**Next step**: review borderline rejects above. If too many look valuable,")
    p("the threshold is too tight — adjust `platforms.*.score_threshold` in")
    p("`config/ingest_filter.json` and re-run this report. Iterate until the")
    p("borderline list looks like noise. Only THEN consider building `--apply`.")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true",
                    help="print to stdout only, don't write report files")
    args = ap.parse_args()

    cfg = load_filter_config()
    ingested_ids = load_already_ingested()
    print(f"[backfill] watermark contains {len(ingested_ids)} ingested obs", file=sys.stderr)

    obs_rows = fetch_ingested_obs(ingested_ids)
    print(f"[backfill] pulled {len(obs_rows)} rows from claude-mem.db", file=sys.stderr)
    if len(obs_rows) != len(ingested_ids):
        missing = len(ingested_ids) - len(obs_rows)
        print(f"[backfill] WARNING: {missing} ids in watermark but not in claude-mem.db", file=sys.stderr)

    # Score every row WITHOUT quota tracking — backfill is retroactive, daily
    # quota doesn't apply to historical data.
    scored: list[dict] = []
    for obs in obs_rows:
        d = evaluate_obs(obs, cfg, quotas=None)
        breakdown = getattr(d, "_breakdown", None) or {}
        scored.append({
            "obs_id": d.obs_id,
            "obs_type": d.obs_type,
            "platform": d.platform,
            "project": d.project,
            "score": d.score,
            "threshold": d.threshold,
            "would_accept": d.would_accept,
            "layer": d.layer,
            "reasons": d.reasons,
            "score_breakdown": breakdown,
        })
    print(f"[backfill] scored {len(scored)} episodes", file=sys.stderr)

    # Pull KG impact data — only for rejected ones (saves a lot of round-trips
    # if most are accepts)
    reject_ids = [r["obs_id"] for r in scored if not r["would_accept"]]
    print(f"[backfill] fetching KG impact for {len(reject_ids)} rejects", file=sys.stderr)
    impact = fetch_episode_impact(reject_ids)

    md = render_report(scored, impact, cfg)
    print(md)

    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        date_tag = datetime.now().strftime("%Y-%m-%d")
        md_path = REPORT_DIR / f"backfill-dryrun-{date_tag}.md"
        jsonl_path = REPORT_DIR / f"backfill-dryrun-{date_tag}.jsonl"
        md_path.write_text(md)
        with jsonl_path.open("w") as f:
            for r in scored:
                r2 = dict(r)
                r2["kg_impact"] = impact.get(r["obs_id"], {"exists_in_kg": False, "mentions": 0})
                f.write(json.dumps(r2, ensure_ascii=False) + "\n")
        print(f"\n[report] {md_path}", file=sys.stderr)
        print(f"[detail] {jsonl_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
