"""tools/engagement_audit.py — Tier 1 contribution signal: exposure vs engagement.

The PUSH hook's `usage_count` measures EXPOSURE (how often a capsule was injected),
not CONTRIBUTION (whether the session actually used it). See DESIGN.md 已知局限 L1
and docs/CONTRIBUTION-SIGNAL.md.

Tier 1 closes part of the gap deterministically (no LLM): for each injection event
in the PUSH hook log, find the claude-mem session it started, and check whether that
session's observations contain DISTINCTIVE terms drawn from the injected capsule.
If they do, the capsule was plausibly engaged with; if not, it was injected and
ignored. Aggregated per capsule, this yields engagement_count alongside usage_count
and surfaces the "injected a lot but never actually used" capsules.

Join (no session_id in the hook log, so time+project):
  injection (ts, kw==project, picked names)  ↔  the session in `project` whose first
  observation starts nearest-after the injection timestamp.

Scope caveat: reads the LOCAL Mac artifacts only —
  - data/.push_hook.log         (this machine's Claude Code injections)
  - ~/.claude-mem/claude-mem.db (this machine's captured observations)
Cursor / Codex / other machines are not covered. It is also a CORRELATION signal
(distinctive-term overlap), not proof of causation — Tier 2/3 (LLM judge / ablation)
refine it later.

Usage:
  python -m tools.engagement_audit                 # report to stdout + file
  python -m tools.engagement_audit --since 2026-06-16
  python -m tools.engagement_audit --min-shared 3  # stricter overlap threshold
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import json
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "data" / ".push_hook.log"
CLAUDE_MEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
REPORTS = Path.home() / ".kg-hub" / "reports"

# Session-start to first-observation tolerance for matching an injection to a session.
MATCH_BEFORE_SEC = 180      # session's first obs may predate the logged hook line slightly
MATCH_AFTER_SEC = 3600      # ...or trail it (user's first prompt comes minutes later)

OK_RE = re.compile(
    r"^(?P<ts>\S+)\s+OK fmt=\S+ kw=(?P<kw>\S+) picked=\d+ names=\[(?P<names>[^\]]*)\]"
)

# Distinctive-term extraction: identifiers / paths / code / CamelCase — the tokens
# that, if they show up in a session, plausibly came from the capsule (not generic prose).
_TERM_RES = [
    re.compile(r"`([^`\n]{3,40})`"),                       # `code spans`
    re.compile(r"\b([A-Za-z][A-Za-z0-9]*[_./-][A-Za-z0-9_./-]{2,})\b"),  # snake/path/dotted
    re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z0-9]+)+)\b"),   # CamelCase
]
_GENERIC = {  # too common across the kg-hub corpus to be evidence of a specific capsule
    "kg-hub", "kg_hub", "claude-mem", "claude_mem", "falkordb", "graphiti",
    "workspace_claudecode", "usage_count", "api/ingest", "api/search",
}


def parse_ts(s: str) -> float:
    # hook log: 2026-06-22T12:30:18Z ; obs: 2026-06-22T13:23:26.953Z
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


def load_injections(since_epoch: float) -> list[dict]:
    out = []
    if not LOG_PATH.exists():
        return out
    for line in LOG_PATH.read_text(errors="ignore").splitlines():
        m = OK_RE.match(line)
        if not m:
            continue
        try:
            ts = parse_ts(m.group("ts"))
        except Exception:
            continue
        if ts < since_epoch:
            continue
        names = [n.strip() for n in m.group("names").split(",") if n.strip()]
        canon = [n for n in names if n.startswith("kg-hub-canonical-")]
        if canon:
            out.append({"ts": ts, "project": m.group("kw"), "names": canon})
    return out


def load_sessions() -> dict[str, list[dict]]:
    """project -> [ {sid, start, text} ] sorted by start."""
    if not CLAUDE_MEM_DB.exists():
        return {}
    con = sqlite3.connect(f"file:{CLAUDE_MEM_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT memory_session_id, project, "
            "       MIN(created_at_epoch)/1000.0 AS start, "
            "       group_concat(coalesce(text,'')||' '||coalesce(title,'')||' '||"
            "         coalesce(subtitle,'')||' '||coalesce(facts,'')||' '||"
            "         coalesce(narrative,'')||' '||coalesce(concepts,''), ' ') AS body "
            "FROM observations GROUP BY memory_session_id, project"
        ).fetchall()
    finally:
        con.close()
    by_proj: dict[str, list[dict]] = defaultdict(list)
    for sid, project, start, body in rows:
        if start is None:
            continue
        by_proj[project].append({"sid": sid, "start": float(start), "text": (body or "").lower()})
    for v in by_proj.values():
        v.sort(key=lambda s: s["start"])
    return by_proj


def match_session(inj: dict, by_proj: dict[str, list[dict]]) -> dict | None:
    cands = by_proj.get(inj["project"], [])
    best, best_d = None, None
    for s in cands:
        d = s["start"] - inj["ts"]
        if -MATCH_BEFORE_SEC <= d <= MATCH_AFTER_SEC:
            ad = abs(d)
            if best_d is None or ad < best_d:
                best, best_d = s, ad
    return best


def fetch_capsule_texts() -> dict[str, str]:
    base = (os.environ.get("KG_HUB_URL") or "http://127.0.0.1:8080").rstrip("/")
    tok = os.environ.get("KG_HUB_API_TOKEN") or ""
    req = urllib.request.Request(
        f"{base}/api/canonical_context?kw=kg-hub&top_n=20&bump=0",
        headers={"Authorization": f"Bearer {tok}"} if tok else {},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    return {x["name"]: (x["content"] or "") for x in d.get("picked", [])
            if x["name"].startswith("kg-hub-canonical-")}


def fetch_usage() -> dict[str, int]:
    base = (os.environ.get("KG_HUB_URL") or "http://127.0.0.1:8080").rstrip("/")
    tok = os.environ.get("KG_HUB_API_TOKEN") or ""
    req = urllib.request.Request(
        f"{base}/api/usage_ranking?top_n=50",
        headers={"Authorization": f"Bearer {tok}"} if tok else {},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    uc = {x["name"]: int(x["usage_count"]) for x in d.get("top_canonical", [])}
    for x in d.get("demote", []):
        uc.setdefault(x["name"], 0)
    return uc


def extract_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for rx in _TERM_RES:
        for m in rx.findall(text):
            t = m.lower().strip(" .,:;`")
            if len(t) >= 4 and t not in _GENERIC:
                terms.add(t)
    return terms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-16", help="ISO date; ignore injections before it")
    ap.add_argument("--min-shared", type=int, default=2,
                    help="engaged if >= this many distinctive terms overlap (or >=1 capsule-unique term)")
    args = ap.parse_args()

    since_epoch = parse_ts(args.since + "T00:00:00Z")
    injections = load_injections(since_epoch)
    by_proj = load_sessions()
    try:
        cap_text = fetch_capsule_texts()
        usage = fetch_usage()
    except Exception as exc:
        print(f"[engagement_audit] cannot reach kg-hub server: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    # Distinctive terms per capsule + document frequency across the corpus.
    cap_terms = {name: extract_terms(txt) for name, txt in cap_text.items()}
    df: dict[str, int] = defaultdict(int)
    for terms in cap_terms.values():
        for t in terms:
            df[t] += 1
    unique_terms = {name: {t for t in terms if df[t] == 1} for name, terms in cap_terms.items()}

    stat = defaultdict(lambda: {"injected": 0, "matched": 0, "engaged": 0})
    for inj in injections:
        sess = match_session(inj, by_proj)
        for name in inj["names"]:
            st = stat[name]
            st["injected"] += 1
            if not sess:
                continue
            st["matched"] += 1
            body = sess["text"]
            uhit = sum(1 for t in unique_terms.get(name, ()) if t in body)
            shit = sum(1 for t in cap_terms.get(name, ()) if t in body)
            if uhit >= 1 or shit >= args.min_shared:
                st["engaged"] += 1

    # Render
    L = [
        f"# kg-hub 胶囊 曝光 vs 参与 (Tier 1) — {datetime.now(tz=timezone.utc).date()}",
        f"窗口: injections since {args.since} · 本机 Claude Code only · 相关性信号(非因果)",
        "",
        "| 胶囊 | usage(曝光) | 本机注入 | 对上会话 | 参与 | 参与率 |",
        "|---|---|---|---|---|---|",
    ]
    names = sorted(set(stat) | set(usage),
                   key=lambda n: -(stat[n]["engaged"] if n in stat else 0))
    for name in names:
        st = stat.get(name, {"injected": 0, "matched": 0, "engaged": 0})
        uc = usage.get(name, 0)
        rate = f"{100*st['engaged']/st['matched']:.0f}%" if st["matched"] else "—"
        L.append(f"| {name.replace('kg-hub-canonical-','')} | {uc} | "
                 f"{st['injected']} | {st['matched']} | {st['engaged']} | {rate} |")
    L += [
        "",
        "- **曝光**：`usage_count`（全平台累计被注入次数）。",
        "- **本机注入 / 对上会话**：本机日志里该胶囊的注入数 / 成功对上 claude-mem 会话的数。",
        "- **参与**：对上的会话里，其 observations 命中了该胶囊独有/独特术语 → 大概率真被用到。",
        "- 参与率低而曝光高 = 「被钉得多但没人真用」的噪音候选；参与率高 = 真在贡献。",
        "- 这是确定性相关信号，非因果；Tier 2（LLM 裁判）/ Tier 3（消融）进一步校准。见 docs/CONTRIBUTION-SIGNAL.md。",
    ]
    md = "\n".join(L)
    print(md)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"engagement-{datetime.now().strftime('%Y-%m-%d')}.md").write_text(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
