"""G6-lite 读数器 —— 把 /backup/delivery-hits.jsonl 聚合成「按 endpoint 的 type-mix 表」。

见 docs/LANDING-PLAN-cognitive-asset.md（G5 flip gate 第 2/3 步：flip 前固化 baseline 表、
flip 后跑同口径对比）。纯读，不写。

用法（NAS 一次性容器，挂 ingest-backup 卷即含 /backup/delivery-hits.jsonl）：
  compose run --rm --no-deps -v .../ingest-backup:/backup:ro \
    -v .../tools/delivery_stats.py:/app/tools/delivery_stats.py \
    ingester python -m tools.delivery_stats
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

KNOWLEDGE = {"decision", "security_note", "security_alert"}
OPERATIONAL = {"bugfix", "change", "feature", "refactor"}


def _bucket(t: str) -> str:
    if t in KNOWLEDGE:
        return "knowledge"
    if t == "discovery":
        return "discovery"
    if t in OPERATIONAL:
        return "operational"
    if t == "?":
        return "canonical/?"   # canonical 胶囊无 type= —— tiering 不作用其上
    return "other"


def load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def summarize(rows: list[dict]) -> dict:
    by_ep = defaultdict(lambda: {"hits": 0, "picks": 0, "buckets": Counter(), "tiering": Counter()})
    for r in rows:
        ep = r.get("endpoint") or "?"
        s = by_ep[ep]
        s["hits"] += 1
        s["tiering"][bool(r.get("tiering"))] += 1
        for p in r.get("picked", []):
            s["picks"] += 1
            s["buckets"][_bucket(p.get("type") or "?")] += 1
    # 转可读
    report = {"total_hits": len(rows), "by_endpoint": {}}
    for ep, s in by_ep.items():
        picks = s["picks"] or 1
        report["by_endpoint"][ep] = {
            "hits": s["hits"], "picks": s["picks"],
            "tiering_on_hits": s["tiering"].get(True, 0),
            "tiering_off_hits": s["tiering"].get(False, 0),
            "mix_pct": {k: round(100 * v / picks, 1) for k, v in s["buckets"].most_common()},
            "mix_count": dict(s["buckets"].most_common()),
        }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="G6-lite delivery-hits 聚合（只读）")
    ap.add_argument("--log", default=os.environ.get("KG_HUB_DELIVERY_LOG", "/backup/delivery-hits.jsonl"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load(args.log)
    rep = summarize(rows)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"G6-lite 交付命中聚合（{args.log}）")
    print(f"总命中(调用)数: {rep['total_hits']}")
    if not rep["total_hits"]:
        print("（暂无数据——G6-lite 需真实注入/搜索触发后才有记录）")
        return 0
    for ep, s in rep["by_endpoint"].items():
        print(f"\n[{ep}]  hits={s['hits']}  picks={s['picks']}  "
              f"(tiering on/off = {s['tiering_on_hits']}/{s['tiering_off_hits']})")
        print(f"   type-mix: {s['mix_pct']}")
    print("\n注：canonical 胶囊记为 canonical/? （无 type=，tiering 不作用其上）；"
          "ops_noise 已被 G3 归档过滤，交付中应≈0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
