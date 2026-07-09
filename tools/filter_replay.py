"""WS-3 回放验证器：证明「武装 ops_noise」带来的**新增拦截**只砸在运维自指 bugfix 上。

见 docs/LANDING-PLAN-cognitive-asset.md（WS-3 步骤 4）。

做法：读本机 claude-mem.db 最近 N 条 obs，对每条跑两遍过滤器——
  - 现行配置 cur（config/ingest_filter.json 原样，ops_noise.enabled 通常 false）
  - 候选配置 cand（cur 的深拷贝，**只** flip ops_noise.enabled=true，其它一字不改）
delta = 在 cur 下 would_accept=True、在 cand 下 would_accept=False 的 obs（= 武装带来的净增拦截）。

报告：delta 条数、其中 is_ops_noise 占比、delta 的 type 分布、以及硬断言——
decision/security_alert/security_note 与「不含 kg-hub 自我标记」的 obs **不得**出现在 delta。
纯读，不写图、不写库、不动线上；本地跑（claude-mem.db 在 Mac）。

用法：
  python -m tools.filter_replay --last 800
  python -m tools.filter_replay --last 800 --report data/ws3-replay.json
"""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.ingest_filter import load_config, evaluate, QuotaTracker  # noqa: E402
from utils.ops_noise import is_ops_noise  # noqa: E402

CLAUDE_MEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"

_SELECT = (
    "SELECT o.id, o.project, o.type, o.title, o.subtitle, o.facts, o.narrative, "
    "o.concepts, o.files_read, o.files_modified, o.created_at, o.content_hash, "
    "o.generated_by_model, o.relevance_count, s.platform_source "
    "FROM observations o "
    "LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id "
    "ORDER BY o.created_at_epoch DESC"
)


def fetch(limit: int) -> list[dict]:
    if not CLAUDE_MEM_DB.exists():
        raise SystemExit(f"claude-mem db not found at {CLAUDE_MEM_DB}")
    conn = sqlite3.connect(f"file:{CLAUDE_MEM_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = _SELECT + (f" LIMIT {int(limit)}" if limit else "")
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    conn.close()
    return rows


def _candidate(cfg: dict) -> dict:
    cand = copy.deepcopy(cfg)
    cand.setdefault("ops_noise", {})["enabled"] = True
    return cand


def run(limit: int) -> dict:
    cur_cfg = load_config()
    cand_cfg = _candidate(cur_cfg)
    obs_list = fetch(limit)

    qt_cur, qt_cand = QuotaTracker(), QuotaTracker()
    delta = []          # cur accept -> cand reject（武装净增拦截）
    newly_accepted = [] # 反向：cur reject -> cand accept（应为空）
    for obs in obs_list:
        d_cur = evaluate(obs, cur_cfg, qt_cur)
        d_cand = evaluate(obs, cand_cfg, qt_cand)
        if d_cur.would_accept and not d_cand.would_accept:
            delta.append(obs)
        elif (not d_cur.would_accept) and d_cand.would_accept:
            newly_accepted.append(obs)

    # 拆解 delta
    ops_hits = [o for o in delta if is_ops_noise(o, cand_cfg)]
    non_ops = [o for o in delta if not is_ops_noise(o, cand_cfg)]
    type_dist = Counter((o.get("type") or "?") for o in delta)

    # 硬断言：任何「非运维自指」被武装拦下都是违规（受保护类型 decision/security_*
    # 因 type gate 必不是 ops_noise，故必然落在 non_ops 里，一并被抓）。
    violations = [
        {"id": o.get("id"), "type": o.get("type"),
         "title": (o.get("title") or "")[:70]}
        for o in non_ops
    ]

    return {
        "n_replayed": len(obs_list),
        "delta_newly_rejected": len(delta),
        "delta_ops_noise": len(ops_hits),
        "delta_non_ops": len(non_ops),
        "delta_ops_share_pct": round(100.0 * len(ops_hits) / max(len(delta), 1), 1),
        "delta_type_dist": dict(type_dist),
        "newly_accepted_by_arming": len(newly_accepted),  # 期望 0：武装只减不增
        "violations": violations,                          # 期望空：受保护类型/非运维被误拦
        "samples": [
            {"id": o.get("id"), "type": o.get("type"),
             "is_ops_noise": is_ops_noise(o, cand_cfg),
             "title": (o.get("title") or "")[:72]}
            for o in delta[:12]
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="WS-3 ops_noise 武装回放（现行 vs 候选 enabled=true 的新增拦截）")
    ap.add_argument("--last", type=int, default=800, help="回放最近 N 条 obs（0=全部）")
    ap.add_argument("--report", metavar="PATH", help="把 JSON 报告写到文件")
    args = ap.parse_args()

    rep = run(args.last)

    print(f"回放 {rep['n_replayed']} 条 obs（候选仅 flip ops_noise.enabled=true）")
    print(f"  武装净增拦截 delta      : {rep['delta_newly_rejected']}")
    print(f"    其中运维自指(ops_noise): {rep['delta_ops_noise']}  ({rep['delta_ops_share_pct']}%)")
    print(f"    非运维(应为 0)         : {rep['delta_non_ops']}")
    print(f"  delta 的 type 分布       : {rep['delta_type_dist']}")
    print(f"  武装反而放行(应为 0)     : {rep['newly_accepted_by_arming']}")
    ok = (not rep["violations"]) and rep["delta_non_ops"] == 0 and rep["newly_accepted_by_arming"] == 0
    if rep["violations"]:
        print(f"  ❌ 违规(受保护类型/非运维被误拦): {rep['violations']}")
    print("  样本(前 12 条 delta):")
    for s in rep["samples"]:
        print(f"    [{'ops' if s['is_ops_noise'] else 'NON'}] {s['type']:<12} {s['title']}")
    print(f"\n验收：{'✅ PASS（新增拦截全是运维自指，受保护类型零误杀）' if ok else '❌ FAIL（见上）'}")

    if args.report:
        Path(args.report).write_text(json.dumps(rep, ensure_ascii=False, indent=2))
        print(f"[report] 已写 {args.report}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
