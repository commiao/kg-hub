"""
kg-hub quality audit — read-only baseline diagnostics.

Purpose: produce a single markdown report quantifying kg-hub data quality
across five dimensions before any filtering is introduced. This is the
"before" snapshot against which post-filter improvements get measured.

Read-only by design — touches no graphs, no databases beyond SELECT/READ.

Output:
  - stdout: condensed summary
  - file:   ~/.kg-hub/reports/quality-baseline-YYYY-MM-DD.md

Usage:
  python -m tools.quality_audit
  python -m tools.quality_audit --no-write   # stdout only

Sections of the report:
  1. claude-mem source health (what's available to ingest)
  2. Ingestion state         (what's been ingested vs pending)
  3. kg-hub composition      (current entity/edge/episode totals)
  4. Pollution analysis      (low-value type share already in KG)
  5. Connectivity health     (degree distribution, orphans)
  6. KPIs vs target bands    (verdict + risk projection)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path.home() / ".claude-mem" / ".env", override=True)

from falkordb import FalkorDB  # noqa: E402


CLAUDE_MEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
WATERMARK_PATH = Path(__file__).resolve().parent.parent / "data" / ".ingested.claude_mem.json"
REPORT_DIR = Path.home() / ".kg-hub" / "reports"

LOW_VALUE_TYPES = {"discovery", "change", "note"}
HIGH_VALUE_TYPES = {"decision", "bugfix", "feature", "refactor",
                    "security_alert", "security_note"}

KPI_BANDS = {
    "edge_per_entity":      {"good": (3.0, 5.0), "warn": (2.5, 6.0)},
    "low_value_share_pct":  {"good": (0, 30),    "warn": (0, 40)},
    "orphan_share_pct":     {"good": (0, 10),    "warn": (0, 15)},
    "singleton_share_pct":  {"good": (0, 25),    "warn": (0, 35)},
}


# ---------- helpers ----------

def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def kpi_verdict(name: str, value: float) -> str:
    band = KPI_BANDS.get(name)
    if not band:
        return "—"
    g_lo, g_hi = band["good"]
    w_lo, w_hi = band["warn"]
    if g_lo <= value <= g_hi:
        return "OK"
    if w_lo <= value <= w_hi:
        return "WARN"
    return "BAD"


# ---------- claude-mem source ----------

def audit_claude_mem() -> dict:
    if not CLAUDE_MEM_DB.exists():
        raise SystemExit(f"claude-mem db not found: {CLAUDE_MEM_DB}")
    conn = sqlite3.connect(f"file:{CLAUDE_MEM_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]

    by_type = {
        r["type"]: r["n"]
        for r in conn.execute(
            "SELECT type, COUNT(*) AS n FROM observations GROUP BY type ORDER BY n DESC"
        )
    }

    by_platform = {
        r["platform_source"] or "(null)": r["n"]
        for r in conn.execute(
            """
            SELECT s.platform_source, COUNT(*) AS n
            FROM observations o
            LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id
            GROUP BY s.platform_source
            ORDER BY n DESC
            """
        )
    }

    by_platform_type = [
        {"platform": r["platform_source"] or "(null)", "type": r["type"], "n": r["n"]}
        for r in conn.execute(
            """
            SELECT s.platform_source, o.type, COUNT(*) AS n
            FROM observations o
            LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id
            GROUP BY s.platform_source, o.type
            ORDER BY n DESC
            """
        )
    ]

    by_project = [
        {"project": r["project"], "n": r["n"]}
        for r in conn.execute(
            "SELECT project, COUNT(*) AS n FROM observations GROUP BY project ORDER BY n DESC LIMIT 10"
        )
    ]

    daily = [
        {"date": r["d"], "n": r["n"]}
        for r in conn.execute(
            """
            SELECT date(created_at) AS d, COUNT(*) AS n
            FROM observations
            WHERE created_at_epoch > strftime('%s','now','-14 days')
            GROUP BY date(created_at)
            ORDER BY d DESC
            """
        )
    ]

    # Content density signals (median across recent obs)
    density = conn.execute(
        """
        SELECT
          COUNT(*) AS n,
          AVG(LENGTH(narrative)) AS avg_narrative_chars,
          AVG(CASE WHEN facts IS NOT NULL AND facts != '' AND facts != '[]' THEN 1 ELSE 0 END) AS frac_has_facts
        FROM observations
        WHERE created_at_epoch > strftime('%s','now','-14 days')
        """
    ).fetchone()

    conn.close()

    return {
        "total_obs": total,
        "by_type": by_type,
        "by_platform": by_platform,
        "by_platform_type": by_platform_type,
        "by_project": by_project,
        "daily_last_14d": daily,
        "recent_density": {
            "n": density["n"],
            "avg_narrative_chars": round(density["avg_narrative_chars"] or 0, 1),
            "frac_with_facts": round(density["frac_has_facts"] or 0, 3),
        },
    }


# ---------- ingestion state ----------

def audit_watermark(total_obs: int) -> dict:
    if not WATERMARK_PATH.exists():
        return {
            "ingested": 0,
            "pending": total_obs,
            "ingested_share_pct": 0.0,
            "last_updated": None,
        }
    data = json.loads(WATERMARK_PATH.read_text())
    ingested = len(data.get("ingested_obs_ids") or [])
    return {
        "ingested": ingested,
        "pending": max(total_obs - ingested, 0),
        "ingested_share_pct": round(100 * ingested / max(total_obs, 1), 1),
        "last_updated": data.get("last_updated"),
    }


# ---------- FalkorDB / kg-hub composition ----------

def _falkor_graph():
    db = FalkorDB(
        host=os.environ["KG_HUB_FALKORDB_HOST"],
        port=int(os.environ["KG_HUB_FALKORDB_PORT"]),
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
    )
    return db.select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))


def audit_kg_composition() -> dict:
    g = _falkor_graph()

    def q1(cypher: str) -> int:
        r = g.query(cypher).result_set
        return int(r[0][0]) if r else 0

    entities = q1("MATCH (n:Entity) RETURN count(n)")
    episodes = q1("MATCH (e:Episodic) RETURN count(e)")
    relates_to = q1("MATCH ()-[r:RELATES_TO]->() RETURN count(r)")
    mentions = q1("MATCH ()-[r:MENTIONS]->() RETURN count(r)")

    # Typed entity labels (the union of Entity + a specific type)
    typed_counts = {}
    for lbl in ("Concept", "Tool", "File", "Project", "Session", "Issue",
                "Fix", "Lesson", "Config", "Capsule", "KnowledgeDoc",
                "Person", "Observation", "Saga", "IngestedKey"):
        try:
            typed_counts[lbl] = q1(f"MATCH (n:{lbl}) RETURN count(n)")
        except Exception:
            typed_counts[lbl] = 0

    return {
        "entities": entities,
        "episodes": episodes,
        "edges_relates_to": relates_to,
        "edges_mentions": mentions,
        "edge_per_entity": round(relates_to / max(entities, 1), 2),
        "edge_per_episode": round(relates_to / max(episodes, 1), 2),
        "entity_per_episode": round(entities / max(episodes, 1), 2),
        "by_typed_label": typed_counts,
    }


def audit_episode_provenance() -> dict:
    """
    Parse `source_description` on Episodic nodes to attribute episodes
    to {source_kind, type, project}. Format produced by the ingester:
      "claude-mem obs id={id} project={p} type={t}"
    Anything else (e.g., OpenClaw capsules) lands in 'other'.
    """
    g = _falkor_graph()
    rows = g.query(
        "MATCH (e:Episodic) RETURN e.source_description AS sd LIMIT 100000"
    ).result_set

    by_source_kind = Counter()
    by_claude_mem_type = Counter()
    by_project = Counter()
    unparsed_examples: list[str] = []

    for (sd,) in rows:
        sd = sd or ""
        if sd.startswith("claude-mem obs"):
            by_source_kind["claude-mem"] += 1
            type_ = None
            project = None
            for tok in sd.split():
                if tok.startswith("type="):
                    type_ = tok[5:]
                elif tok.startswith("project="):
                    project = tok[8:]
            by_claude_mem_type[type_ or "(unknown)"] += 1
            by_project[project or "(unknown)"] += 1
        elif "openclaw" in sd.lower() or "capsule" in sd.lower():
            by_source_kind["openclaw"] += 1
        else:
            by_source_kind["other"] += 1
            if len(unparsed_examples) < 3 and sd:
                unparsed_examples.append(sd[:120])

    cm_total = sum(by_claude_mem_type.values())
    low_value_count = sum(by_claude_mem_type.get(t, 0) for t in LOW_VALUE_TYPES)
    high_value_count = sum(by_claude_mem_type.get(t, 0) for t in HIGH_VALUE_TYPES)

    return {
        "by_source_kind": dict(by_source_kind),
        "claude_mem_by_type": dict(by_claude_mem_type),
        "claude_mem_top_projects": dict(by_project.most_common(10)),
        "claude_mem_total": cm_total,
        "low_value_count": low_value_count,
        "high_value_count": high_value_count,
        "low_value_share_pct": round(100 * low_value_count / max(cm_total, 1), 1),
        "high_value_share_pct": round(100 * high_value_count / max(cm_total, 1), 1),
        "unparsed_examples": unparsed_examples,
    }


def audit_connectivity() -> dict:
    """Degree distribution over Entity nodes via RELATES_TO edges only.
    (MENTIONS edges are episode→entity, not entity↔entity, so excluded.)"""
    g = _falkor_graph()

    total = g.query("MATCH (n:Entity) RETURN count(n)").result_set[0][0]
    total = int(total)

    # Degree bucketed counts via aggregate query
    deg_rows = g.query(
        """
        MATCH (n:Entity)
        OPTIONAL MATCH (n)-[r:RELATES_TO]-()
        WITH n, count(r) AS deg
        RETURN deg, count(*) AS n
        ORDER BY deg
        """
    ).result_set

    buckets = {"0": 0, "1": 0, "2-3": 0, "4-7": 0, "8-15": 0, "16-31": 0, "32+": 0}
    deg_dist = []
    for deg, cnt in deg_rows:
        deg = int(deg)
        cnt = int(cnt)
        deg_dist.append({"degree": deg, "count": cnt})
        if deg == 0:
            buckets["0"] += cnt
        elif deg == 1:
            buckets["1"] += cnt
        elif deg <= 3:
            buckets["2-3"] += cnt
        elif deg <= 7:
            buckets["4-7"] += cnt
        elif deg <= 15:
            buckets["8-15"] += cnt
        elif deg <= 31:
            buckets["16-31"] += cnt
        else:
            buckets["32+"] += cnt

    # Top hubs
    top_hub_rows = g.query(
        """
        MATCH (n:Entity)
        OPTIONAL MATCH (n)-[r:RELATES_TO]-()
        WITH n, count(r) AS deg
        ORDER BY deg DESC
        LIMIT 15
        RETURN coalesce(n.name, 'unnamed') AS name, deg
        """
    ).result_set
    top_hubs = [{"name": r[0], "degree": int(r[1])} for r in top_hub_rows]

    return {
        "total_entities": total,
        "orphans": buckets["0"],
        "singletons": buckets["1"],
        "orphan_share_pct": round(100 * buckets["0"] / max(total, 1), 1),
        "singleton_share_pct": round(100 * buckets["1"] / max(total, 1), 1),
        "buckets": buckets,
        "top_hubs": top_hubs,
    }


# ---------- report rendering ----------

def render_markdown(data: dict) -> str:
    cm = data["claude_mem"]
    wm = data["watermark"]
    kg = data["kg_composition"]
    prov = data["provenance"]
    conn = data["connectivity"]
    ts = data["generated_at"]

    # --- KPI table
    edge_per_entity = kg["edge_per_entity"]
    low_value_share = prov["low_value_share_pct"]
    orphan_share = conn["orphan_share_pct"]
    singleton_share = conn["singleton_share_pct"]

    kpi_rows = [
        ("edge / entity ratio",          edge_per_entity,  "3.0–5.0", kpi_verdict("edge_per_entity", edge_per_entity)),
        ("low-value obs share (in KG)",  low_value_share,  "<30%",    kpi_verdict("low_value_share_pct", low_value_share)),
        ("orphan entities share",        orphan_share,     "<10%",    kpi_verdict("orphan_share_pct", orphan_share)),
        ("singleton (degree=1) share",   singleton_share,  "<25%",    kpi_verdict("singleton_share_pct", singleton_share)),
    ]

    lines = []
    p = lines.append

    p(f"# kg-hub Quality Baseline — {ts}")
    p("")
    p("Read-only diagnostic snapshot. Source of truth for the \"before\" state.")
    p("")
    p("## TL;DR — KPI verdict")
    p("")
    p("| KPI | Value | Target | Verdict |")
    p("|---|---|---|---|")
    for name, val, target, verdict in kpi_rows:
        marker = {"OK": "✓", "WARN": "⚠️", "BAD": "🔴"}.get(verdict, "—")
        p(f"| {name} | {val} | {target} | {marker} {verdict} |")
    p("")

    # --- Section 1
    p("## 1. claude-mem source health")
    p("")
    p(f"- Total observations: **{cm['total_obs']}**")
    p(f"- Recent (14d) avg narrative length: **{cm['recent_density']['avg_narrative_chars']} chars**")
    p(f"- Recent (14d) fraction with facts: **{cm['recent_density']['frac_with_facts']*100:.1f}%**")
    p("")
    p("### Daily rate (last 14d)")
    p("")
    p("| Date | Count |")
    p("|---|---|")
    for row in cm["daily_last_14d"]:
        p(f"| {row['date']} | {row['n']} |")
    p("")
    p("### Type distribution (all-time)")
    p("")
    p("| Type | Count | Share |")
    p("|---|---|---|")
    cm_total = cm["total_obs"]
    for t, n in cm["by_type"].items():
        p(f"| {t} | {n} | {100*n/max(cm_total,1):.1f}% |")
    p("")
    p("### Platform source distribution")
    p("")
    p("| Platform | Count |")
    p("|---|---|")
    for plat, n in cm["by_platform"].items():
        p(f"| {plat} | {n} |")
    p("")
    p("### Top 10 projects by obs count")
    p("")
    p("| Project | Count |")
    p("|---|---|")
    for row in cm["by_project"]:
        p(f"| {row['project']} | {row['n']} |")
    p("")

    # --- Section 2
    p("## 2. Ingestion state")
    p("")
    p(f"- Ingested into kg-hub: **{wm['ingested']} / {cm['total_obs']} ({wm['ingested_share_pct']}%)**")
    p(f"- Pending (in claude-mem, not yet in KG): **{wm['pending']}**")
    p(f"- Watermark last updated: {wm['last_updated']}")
    p("")

    # --- Section 3
    p("## 3. kg-hub composition")
    p("")
    p(f"- Entities: **{kg['entities']}**")
    p(f"- Episodes: **{kg['episodes']}**")
    p(f"- RELATES_TO edges (entity↔entity): **{kg['edges_relates_to']}**")
    p(f"- MENTIONS edges (episode→entity): **{kg['edges_mentions']}**")
    p(f"- edge/entity ratio: **{kg['edge_per_entity']}**")
    p(f"- entity/episode ratio: **{kg['entity_per_episode']}**")
    p(f"- edge/episode ratio: **{kg['edge_per_episode']}**")
    p("")
    p("### Typed entity labels")
    p("")
    p("| Label | Count |")
    p("|---|---|")
    for lbl, n in sorted(kg["by_typed_label"].items(), key=lambda x: -x[1]):
        if n > 0:
            p(f"| {lbl} | {n} |")
    p("")

    # --- Section 4
    p("## 4. Pollution analysis")
    p("")
    p("### Episode provenance")
    p("")
    p("| Source kind | Count |")
    p("|---|---|")
    for k, n in prov["by_source_kind"].items():
        p(f"| {k} | {n} |")
    p("")
    p("### claude-mem episodes by parsed type")
    p("")
    p("| Type | Count | Share | Bucket |")
    p("|---|---|---|---|")
    cmtot = prov["claude_mem_total"]
    for t, n in sorted(prov["claude_mem_by_type"].items(), key=lambda x: -x[1]):
        bucket = "LOW" if t in LOW_VALUE_TYPES else ("HIGH" if t in HIGH_VALUE_TYPES else "OTHER")
        p(f"| {t} | {n} | {100*n/max(cmtot,1):.1f}% | {bucket} |")
    p("")
    p(f"- **Low-value share (in KG): {prov['low_value_share_pct']}%** "
      f"({prov['low_value_count']} episodes)")
    p(f"- **High-value share (in KG): {prov['high_value_share_pct']}%** "
      f"({prov['high_value_count']} episodes)")
    p("")
    p("### Top claude-mem projects (in KG)")
    p("")
    p("| Project | Episodes |")
    p("|---|---|")
    for proj, n in prov["claude_mem_top_projects"].items():
        p(f"| {proj} | {n} |")
    p("")

    # --- Section 5
    p("## 5. Connectivity health")
    p("")
    p(f"- Total entities: {conn['total_entities']}")
    p(f"- **Orphans (degree=0): {conn['orphans']} ({conn['orphan_share_pct']}%)**")
    p(f"- **Singletons (degree=1): {conn['singletons']} ({conn['singleton_share_pct']}%)**")
    p("")
    p("### Degree distribution buckets")
    p("")
    p("| Bucket | Count |")
    p("|---|---|")
    for b, n in conn["buckets"].items():
        p(f"| {b} | {n} |")
    p("")
    p("### Top 15 hub entities")
    p("")
    p("| Name | Degree |")
    p("|---|---|")
    for hub in conn["top_hubs"]:
        p(f"| {hub['name']} | {hub['degree']} |")
    p("")

    # --- Section 6 — risk projection
    p("## 6. Risk projection")
    p("")
    last_7 = [r["n"] for r in cm["daily_last_14d"][:7]]
    avg_daily = sum(last_7) / max(len(last_7), 1)
    capacity_per_day = 4 * 24 * 10  # 4 runs/h × 24h × 10 obs/run = 960
    util_pct = round(100 * avg_daily / capacity_per_day, 1)
    p(f"- Recent 7d avg obs/day: **{avg_daily:.1f}**")
    p(f"- Ingester capacity: **{capacity_per_day}/day** (15-min interval × --limit 10)")
    p(f"- Current utilization: **{util_pct}%**")
    p("")
    p("**Cursor injection scenarios** (hypothetical) — same composition rules apply:")
    p("")
    p("| Scenario | Projected obs/day | Capacity util | Notes |")
    p("|---|---|---|---|")
    for mult, label in [(1.0, "baseline"), (3.0, "Cursor light"),
                        (6.0, "Cursor medium"), (10.0, "Cursor heavy")]:
        proj = avg_daily * mult
        u = 100 * proj / capacity_per_day
        flag = "" if u <= 80 else " 🔴 saturated"
        p(f"| {label} (×{mult}) | {proj:.0f} | {u:.1f}% | {flag} |")
    p("")
    p("> Note: capacity saturation is not the only risk. Even at 14% utilization the "
      f"low-value share ({low_value_share}%) is already polluting the KG. Filtering "
      "addresses signal-to-noise, not just throughput.")
    p("")

    p("---")
    p(f"Generated by `tools/quality_audit.py` at {ts}.")
    return "\n".join(lines)


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true",
                    help="print to stdout only, don't write report file")
    ap.add_argument("--json", action="store_true",
                    help="emit raw JSON to stdout instead of markdown")
    args = ap.parse_args()

    cm = audit_claude_mem()
    wm = audit_watermark(cm["total_obs"])
    kg = audit_kg_composition()
    prov = audit_episode_provenance()
    conn = audit_connectivity()

    data = {
        "generated_at": now_iso(),
        "claude_mem": cm,
        "watermark": wm,
        "kg_composition": kg,
        "provenance": prov,
        "connectivity": conn,
    }

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    md = render_markdown(data)
    print(md)

    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        date_tag = datetime.now().strftime("%Y-%m-%d")
        out = REPORT_DIR / f"quality-baseline-{date_tag}.md"
        out.write_text(md)
        print(f"\n[report written] {out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
