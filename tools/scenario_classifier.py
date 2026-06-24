"""tools/scenario_classifier.py — rule-based session scenario detection (v1, no LLM).

Self-evolving scoring is per-(capsule, scenario), so we must label each session's
scenario to know which bucket a reward updates (docs/SELF-EVOLVING.md §3).

v1 is deterministic rules over claude-mem observations (files_modified extensions +
observation types + text markers). Every verdict carries a `reason` so it is auditable
and tunable. Ambiguous → `unknown` (the caller then ABSTAINS — never learn from a
mislabeled session). LLM fallback for the genuinely-ambiguous middle is a later add.

Scenarios: coding (implemented downstream) · ops · writing · research · planning · unknown.

CLI:
  python -m tools.scenario_classifier --list 20      # recent sessions → scenario + reason
  python -m tools.scenario_classifier --route        # injections → which (capsule,scenario) bucket
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tools.engagement_audit import CLAUDE_MEM_DB, load_injections, parse_ts
from tools.capsule_score import ScoreStore

CODE_EXT = {"py", "go", "ts", "tsx", "js", "jsx", "java", "kt", "rs", "c", "cc",
            "cpp", "h", "hpp", "rb", "php", "swift", "scala", "sql", "sh", "vue", "m"}
DOC_EXT = {"md", "markdown", "rst", "txt", "adoc"}
CONFIG_EXT = {"yaml", "yml", "toml", "ini", "conf", "cfg", "env", "properties"}
CONFIG_NAMES = {"dockerfile", "docker-compose.yml", "makefile"}
ASSET_EXT = {"png", "jpg", "jpeg", "gif", "svg", "pdf", "ico", "webp", "mp4"}

CODE_TYPES = {"feature", "bugfix", "refactor", "change"}
OPS_MARKERS = ["告警", "incident", "故障", "部署", "deploy", "nacos", "容器",
               "restart", "重启", "回滚", "rollback", "sls", "日志查询"]
BUILD_TEST_MARKERS = ["pytest", "go test", "npm test", "cargo test", "mvn", "jest",
                      "编译", "build pass", "test pass", "测试通过", "ci 通过", "lint"]
RESEARCH_MARKERS = ["调研", "research", "对比", "评估", "方案", "文档", "search", "查阅"]

_PATH_RE = re.compile(r"[\w./\-]+\.([A-Za-z0-9]{1,6})\b")
_NAME_RE = re.compile(r"\b(dockerfile|docker-compose\.yml|makefile)\b", re.I)


def _file_buckets(files_blob: str) -> dict[str, int]:
    b = {"code": 0, "doc": 0, "config": 0, "asset": 0, "other": 0}
    seen = set()
    for ext in _PATH_RE.findall(files_blob or ""):
        e = ext.lower()
        # de-dup repeated same file across observations is hard; count occurrences is fine
        if e in CODE_EXT:
            b["code"] += 1
        elif e in DOC_EXT:
            b["doc"] += 1
        elif e in CONFIG_EXT:
            b["config"] += 1
        elif e in ASSET_EXT:
            b["asset"] += 1
        else:
            b["other"] += 1
    if _NAME_RE.search(files_blob or ""):
        b["config"] += 1
    return b


def classify(feat: dict) -> tuple[str, str]:
    """feat: {files:bucket-counts, types:Counter, text:lower-str}. → (scenario, reason)."""
    f = feat["files"]
    types = feat["types"]
    text = feat["text"]
    has_ops = any(m in text for m in OPS_MARKERS)
    has_build = any(m in text for m in BUILD_TEST_MARKERS)
    code_types = sum(types.get(t, 0) for t in CODE_TYPES)

    if f["code"] > 0:
        return "coding", f"改了 {f['code']} 个代码文件" + ("，且有构建/测试痕迹" if has_build else "")
    if f["code"] == 0 and code_types > 0 and not (f["config"] and has_ops):
        return "coding", f"无捕获代码文件，但 {code_types} 条 feature/bugfix/refactor 类观察" \
                         + ("（files_modified 常未采全）" if not f["doc"] else "")
    if f["config"] > 0 and f["code"] == 0:
        return "ops", f"改了 {f['config']} 个配置/部署文件" + ("，含运维痕迹" if has_ops else "")
    if has_ops and f["code"] == 0 and f["doc"] == 0:
        return "ops", "运维/排障痕迹为主，无代码改动"
    if f["doc"] > 0 and f["code"] == 0 and f["config"] == 0:
        return "writing", f"只改了 {f['doc']} 个文档文件"
    # 无文件改动
    if f["code"] == f["doc"] == f["config"] == 0:
        if types.get("decision", 0) > 0:
            return "planning", "无文件改动，含 decision 类观察（讨论/决策）"
        if any(m in text for m in RESEARCH_MARKERS):
            return "research", "无文件改动，调研/评估/对比痕迹"
        if types.get("discovery", 0) > 0:
            return "research", "无文件改动，discovery 类为主"
    return "unknown", "规则判不清（待 LLM 兜底或人工）"


def load_session_features(db: Path = CLAUDE_MEM_DB) -> dict[str, dict]:
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT memory_session_id, project, MIN(created_at_epoch)/1000.0 AS start, "
            "       count(*) AS n, "
            "       group_concat(coalesce(type,''), ',') AS types, "
            "       group_concat(coalesce(files_modified,''), ' ') AS files, "
            "       group_concat(coalesce(title,'')||' '||coalesce(text,'')||' '||"
            "         coalesce(facts,''), ' ') AS body "
            "FROM observations GROUP BY memory_session_id, project"
        ).fetchall()
    finally:
        con.close()
    out: dict[str, dict] = {}
    for sid, project, start, n, types, files, body in rows:
        if start is None:
            continue
        out[sid] = {
            "sid": sid, "project": project, "start": float(start), "n": int(n),
            "types": Counter(t for t in (types or "").split(",") if t),
            "files": _file_buckets(files or ""),
            "text": (body or "").lower(),
        }
    return out


def sessions_by_project(feats: dict[str, dict]) -> dict[str, list[dict]]:
    by = defaultdict(list)
    for f in feats.values():
        by[f["project"]].append(f)
    for v in by.values():
        v.sort(key=lambda s: s["start"])
    return by


def match(inj: dict, by_proj: dict[str, list[dict]]) -> dict | None:
    best, best_d = None, None
    for s in by_proj.get(inj["project"], []):
        d = s["start"] - inj["ts"]
        if -180 <= d <= 3600:
            ad = abs(d)
            if best_d is None or ad < best_d:
                best, best_d = s, ad
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", type=int, default=0, metavar="N",
                    help="print scenario+reason for the N most recent sessions")
    ap.add_argument("--route", action="store_true",
                    help="join injections→sessions, show which (capsule,scenario) bucket each routes to")
    ap.add_argument("--since", default="2026-06-16")
    args = ap.parse_args()

    feats = load_session_features()
    if not feats:
        print("[scenario] no observations found", file=sys.stderr)
        return 1

    if args.list:
        recent = sorted(feats.values(), key=lambda s: -s["start"])[: args.list]
        print(f"{'scenario':10} {'project':22} {'n':>3}  reason")
        print("-" * 90)
        for f in recent:
            scen, reason = classify(f)
            print(f"{scen:10} {f['project'][:22]:22} {f['n']:>3}  {reason}")
        return 0

    # default / --route : show injection → bucket routing
    by_proj = sessions_by_project(feats)
    injections = load_injections(parse_ts(args.since + "T00:00:00Z"))
    store = ScoreStore()
    matrix = defaultdict(Counter)      # capsule -> {scenario: count}
    unmatched = 0
    for inj in injections:
        s = match(inj, by_proj)
        if not s:
            unmatched += 1
            continue
        scen, _ = classify(s)
        for cap in inj["names"]:
            matrix[cap][scen] += 1
            store.note_exposure(cap, scen)
    store.save()

    scenarios = ["coding", "ops", "writing", "research", "planning", "unknown"]
    print(f"# 注入 → (胶囊, 场景) 路由  (since {args.since}; 未对上会话 {unmatched} 次)")
    print()
    header = "| 胶囊 | " + " | ".join(scenarios) + " |"
    print(header)
    print("|---" * (len(scenarios) + 1) + "|")
    for cap in sorted(matrix, key=lambda c: -sum(matrix[c].values())):
        cells = " | ".join(str(matrix[cap].get(s, 0)) for s in scenarios)
        print(f"| {cap.replace('kg-hub-canonical-','')} | {cells} |")
    print(f"\n[store] 曝光已按 (胶囊,场景) 记入 {store.path}（reward 待 step 2 CodingReward）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
