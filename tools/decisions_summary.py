"""
kg-hub ingest filter — decisions log summarizer.

Reads data/.ingest_decisions.jsonl (written by utils/ingest_filter.py
every time the ingester scores an observation), filters to a recent
time window, and produces a markdown summary used by humans to decide
when to flip shadow_mode=false.

Read-only by design.

Output:
  - stdout: condensed summary
  - file:   ~/.kg-hub/reports/decisions-weekly-YYYY-MM-DD.md

Usage:
  python -m tools.decisions_summary                # default --window 7d
  python -m tools.decisions_summary --window 1d    # last 24h
  python -m tools.decisions_summary --window all   # full file
  python -m tools.decisions_summary --no-write     # stdout only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


DECISIONS_LOG = Path(__file__).resolve().parent.parent / "data" / ".ingest_decisions.jsonl"
REPORT_DIR = Path.home() / ".kg-hub" / "reports"


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def parse_window(spec: str) -> timedelta | None:
    if spec == "all":
        return None
    m = re.fullmatch(r"(\d+)([dhm])", spec.strip().lower())
    if not m:
        raise SystemExit(f"--window must be Nd / Nh / Nm or 'all', got {spec!r}")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(days=n) if unit == "d" else (
        timedelta(hours=n) if unit == "h" else timedelta(minutes=n)
    )


def load_decisions(window: timedelta | None) -> list[dict]:
    if not DECISIONS_LOG.exists():
        return []
    cutoff = None
    if window is not None:
        cutoff = datetime.now(tz=timezone.utc) - window
    rows: list[dict] = []
    for line in DECISIONS_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if cutoff is not None:
            try:
                ts = datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))
            except Exception:
                continue
            if ts < cutoff:
                continue
        rows.append(rec)
    return rows


def histogram(scores: list[float]) -> dict[str, int]:
    buckets = {
        "0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0,
        "80-99": 0, "100-149": 0, "150+": 0,
    }
    for s in scores:
        if s < 20: buckets["0-19"] += 1
        elif s < 40: buckets["20-39"] += 1
        elif s < 60: buckets["40-59"] += 1
        elif s < 80: buckets["60-79"] += 1
        elif s < 100: buckets["80-99"] += 1
        elif s < 150: buckets["100-149"] += 1
        else: buckets["150+"] += 1
    return buckets


def render(rows: list[dict], window_spec: str) -> str:
    if not rows:
        return f"# decisions summary — {window_spec}\n\nNo decisions in window. Either shadow mode hasn't accumulated yet, or the window is too narrow.\n"

    n = len(rows)
    accepts = [r for r in rows if r.get("would_accept")]
    rejects = [r for r in rows if not r.get("would_accept")]
    accept_pct = 100 * len(accepts) / n
    reject_pct = 100 - accept_pct

    # Time range actually covered
    ts_list = sorted(r.get("ts", "") for r in rows if r.get("ts"))
    ts_min, ts_max = ts_list[0] if ts_list else "?", ts_list[-1] if ts_list else "?"

    by_layer = Counter(r.get("layer", "?") for r in rejects)
    by_type = Counter(r.get("obs_type", "?") for r in rejects)
    by_platform = Counter(r.get("platform", "?") for r in rejects)
    by_project = Counter(r.get("project", "?") for r in rejects)

    type_decision_xtab: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        outcome = "accept" if r.get("would_accept") else "reject"
        type_decision_xtab[r.get("obs_type", "?")][outcome] += 1

    all_scores = [float(r.get("score", 0)) for r in rows]
    reject_scores = [float(r.get("score", 0)) for r in rejects]
    hist_all = histogram(all_scores)
    hist_rej = histogram(reject_scores)

    # Borderline accepts (just over threshold) — these are the risky "are we letting too much through?" zone
    borderline_accepts = sorted(
        [r for r in accepts if 60 <= r.get("score", 0) < 70],
        key=lambda r: r.get("score", 0),
    )[:10]
    borderline_rejects = sorted(
        [r for r in rejects if r.get("layer") == "score" and 50 <= r.get("score", 0) < 60],
        key=lambda r: -r.get("score", 0),
    )[:10]

    # High-value rejects — split by layer.
    #   * score-layer rejects of HV types = TRUE false-positive risk (debatable)
    #   * hard_gate rejects of HV types  = upstream empty/stub obs (not the filter's fault)
    hv_types = ("decision", "bugfix", "feature", "security_alert")
    hv_rejects = [r for r in rejects if r.get("obs_type") in hv_types]
    hv_score_rejects = [r for r in hv_rejects if r.get("layer") == "score"]
    hv_gate_rejects = [r for r in hv_rejects if r.get("layer") == "hard_gate"]

    # Verdict: ready to flip shadow_mode?
    # Heuristic — flip if:
    #   - >=500 decisions in window (sufficient sample)
    #   - reject_pct in [20%, 50%] (neither too lenient nor too aggressive)
    #   - high-value SCORE-layer rejects = 0 (any non-empty HV obs scored below threshold
    #     is a tuning bug, not acceptable). Hard-gate HV rejects are upstream stubs, OK.
    sample_ok = n >= 500
    reject_band_ok = 20 <= reject_pct <= 50
    hv_score_count = len(hv_score_rejects)
    hv_ok = hv_score_count == 0

    verdict_ready = sample_ok and reject_band_ok and hv_ok
    verdict_emoji = "✅" if verdict_ready else "⏳"

    L = []
    p = L.append

    p(f"# kg-hub Decisions Summary — window={window_spec}")
    p("")
    p(f"Generated: {now_iso()}")
    p(f"Time range: {ts_min}  →  {ts_max}")
    p("")

    p("## Verdict — ready to flip shadow_mode=false?")
    p("")
    p(f"### {verdict_emoji} {'READY' if verdict_ready else 'NOT YET'}")
    p("")
    p("| Criterion | Required | Observed | Status |")
    p("|---|---|---|---|")
    p(f"| Sample size | ≥ 500 decisions | {n} | {'✓' if sample_ok else '✗'} |")
    p(f"| Reject rate in healthy band | 20% – 50% | {reject_pct:.1f}% | {'✓' if reject_band_ok else '✗'} |")
    p(f"| High-value SCORE-layer rejects (real FP risk) | 0 | {hv_score_count} | {'✓' if hv_ok else '✗'} |")
    p(f"| _info_: high-value HARD_GATE rejects (upstream stubs) | — | {len(hv_gate_rejects)} | _ignore_ |")
    p("")
    if verdict_ready:
        p("All criteria met. To go live: edit `config/ingest_filter.json`, set "
          "`shadow_mode: false`. Next ingester run will start blocking rejects "
          "and writing them to `rejected_obs_ids` in the watermark.")
    else:
        p("**Not ready yet.** See criteria above. Common cases:")
        p("- If sample too small: wait for more ingester runs (~15-min cadence)")
        p("- If reject rate too high: relax `score_threshold` per platform")
        p("- If high-value FP share too high: review `hv_rejects` list below and tune scoring")
    p("")

    p("## TL;DR")
    p("")
    p(f"- Total decisions: **{n}**")
    p(f"- Would accept: **{len(accepts)} ({accept_pct:.1f}%)**")
    p(f"- Would reject: **{len(rejects)} ({reject_pct:.1f}%)**")
    p(f"- High-value type rejects: **{len(hv_rejects)}** "
      f"({hv_score_count} score-layer / {len(hv_gate_rejects)} hard-gate stubs)")
    p("")

    p("## Reject reasons by layer")
    p("")
    p("| Layer | Count | Share of rejects |")
    p("|---|---|---|")
    for layer, cnt in by_layer.most_common():
        p(f"| {layer} | {cnt} | {100*cnt/max(len(rejects),1):.1f}% |")
    p("")

    p("## Type × decision cross-tab")
    p("")
    p("| Type | Accept | Reject | Reject rate |")
    p("|---|---|---|---|")
    for typ in sorted(type_decision_xtab.keys(),
                      key=lambda t: -(type_decision_xtab[t]["accept"] + type_decision_xtab[t]["reject"])):
        a = type_decision_xtab[typ]["accept"]
        r = type_decision_xtab[typ]["reject"]
        total = a + r
        p(f"| {typ} | {a} | {r} | {100*r/max(total,1):.1f}% |")
    p("")

    p("## Platform breakdown")
    p("")
    p("| Platform | Reject count |")
    p("|---|---|")
    for plat, cnt in by_platform.most_common():
        p(f"| {plat} | {cnt} |")
    p("")

    p("## Top projects by reject count")
    p("")
    p("| Project | Count |")
    p("|---|---|")
    for proj, cnt in by_project.most_common(10):
        p(f"| {proj} | {cnt} |")
    p("")

    p("## Score distribution")
    p("")
    p("| Bucket | All | Rejects | Accepts |")
    p("|---|---|---|---|")
    for b in hist_all:
        p(f"| {b} | {hist_all[b]} | {hist_rej[b]} | {hist_all[b] - hist_rej[b]} |")
    p("")

    if borderline_accepts:
        p("## Borderline accepts (60–69, just over threshold) — false-negative risk")
        p("")
        p("| obs_id | score | type | platform | project |")
        p("|---|---|---|---|---|")
        for r in borderline_accepts:
            p(f"| {r.get('obs_id','?')} | {r.get('score','?')} | {r.get('obs_type','?')} | "
              f"{r.get('platform','?')} | {r.get('project','?')} |")
        p("")

    if borderline_rejects:
        p("## Borderline rejects (50–59) — false-positive risk")
        p("")
        p("| obs_id | score | type | platform | project | reason |")
        p("|---|---|---|---|---|---|")
        for r in borderline_rejects:
            reasons = (r.get("reasons") or ["?"])[0][:50]
            p(f"| {r.get('obs_id','?')} | {r.get('score','?')} | {r.get('obs_type','?')} | "
              f"{r.get('platform','?')} | {r.get('project','?')} | {reasons} |")
        p("")

    if hv_rejects:
        p("## High-value type rejects — REVIEW THESE")
        p("")
        p("| obs_id | score | type | platform | layer | reason |")
        p("|---|---|---|---|---|---|")
        for r in hv_rejects[:25]:
            reasons = (r.get("reasons") or ["?"])[0][:60]
            p(f"| {r.get('obs_id','?')} | {r.get('score','?')} | {r.get('obs_type','?')} | "
              f"{r.get('platform','?')} | {r.get('layer','?')} | {reasons} |")
        if len(hv_rejects) > 25:
            p(f"\n…and {len(hv_rejects) - 25} more. See `.ingest_decisions.jsonl` for full list.")
        p("")

    p("---")
    p("Generated by `tools/decisions_summary.py`.")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="7d",
                    help="time window: '7d', '24h', '60m', or 'all' (default: 7d)")
    ap.add_argument("--no-write", action="store_true", help="stdout only")
    args = ap.parse_args()

    window = parse_window(args.window)
    rows = load_decisions(window)
    md = render(rows, args.window)
    print(md)

    if not args.no_write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        date_tag = datetime.now().strftime("%Y-%m-%d")
        out = REPORT_DIR / f"decisions-{args.window}-{date_tag}.md"
        out.write_text(md)
        print(f"\n[report] {out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
