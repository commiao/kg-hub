"""交付分层离线 replay —— step 5 设计验证（只读，不改线上排序）。

见 docs/LANDING-PLAN-cognitive-asset.md（0.4 三层定义 · 交付优先级）。

问题：当前「出」侧对图内 Episodic 的排序是纯相关性（fulltext score desc，见
kg_hub_server canonical_context pass-2 / episode_search）。0.4 层③指出：操作型记录
（bugfix/change/feature/refactor）非垃圾，但默认不该与 decision/security 争注入位。

本工具**离线**对比：对一批真实查询，
  - baseline：当前排序 = fulltext score desc
  - tiered  ：score × type 权重（软加权），并保留**探索地板**（top_n 至少留 1 个
              非知识型槽，防操作型被完全饿死）
验证：decision/security 是否上浮、操作型是否降权但仍保留地板。**纯读、不写、不改线上。**

设计（可调，都在下方常量）：
  TIER_WEIGHT：知识型>1、操作型<1、discovery=1；ops_noise 额外压到很低（但非 0）。
  EXPLORE_FLOOR_SLOTS：top_n 中强制保留给最高分操作型的槽位数（探索地板）。

NAS 一次性容器跑（覆盖挂 ops_noise/config）：
  compose run --rm --no-deps -v ...ops_noise.py -v ...config ingester \
    python -m tools.delivery_replay
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.ops_noise import is_ops_noise  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "ingest_filter.json"

# —— 分层设计（软加权，绝不置 0）——
TIER_WEIGHT = {
    "decision": 1.6, "security_note": 1.6, "security_alert": 1.6,   # 知识型上浮
    "discovery": 1.0,                                               # 中性
    "feature": 0.7, "refactor": 0.7,                               # 操作型降权
    "bugfix": 0.6, "change": 0.6,
}
DEFAULT_WEIGHT = 1.0
OPS_NOISE_WEIGHT = 0.2          # ops_noise 额外压低（仍 >0：软加权铁律）
EXPLORE_FLOOR_SLOTS = 1         # top_n 中保留给最高分操作型的探索地板槽

KNOWLEDGE = {"decision", "security_note", "security_alert"}
OPERATIONAL = {"bugfix", "change", "feature", "refactor"}

_SD_TYPE = re.compile(r"type=(\S+)")

DEFAULT_QUERIES = [
    "ingest filter", "capsule ranking", "falkordb", "docker deploy",
    "forward route", "architecture decision", "security", "dashboard",
]


def _obs_of(name, content, sd, cfg):
    t = _SD_TYPE.search(sd or "")
    typ = t.group(1) if t else "?"
    obs = {"type": typ, "narrative": content or "", "title": "", "facts": ""}
    return typ, obs


def _weight(typ, obs, cfg):
    if is_ops_noise(obs, cfg):
        return OPS_NOISE_WEIGHT
    return TIER_WEIGHT.get(typ, DEFAULT_WEIGHT)


def _tiered_topn(cands, top_n):
    """cands: [{name,typ,score,w,tscore}] 已按 tscore desc。探索地板：保证 top_n 里至少
    EXPLORE_FLOOR_SLOTS 个操作型（前提是有操作型候选）。置换规则——**只牺牲最低分的
    「非知识型」项（优先 discovery/其他），绝不牺牲知识型**；若 top_n 全是知识型则
    尊重知识主导、不强插操作型。"""
    picked = cands[:top_n]
    ops_in = [c for c in picked if c["typ"] in OPERATIONAL]
    if len(ops_in) >= EXPLORE_FLOOR_SLOTS:
        return picked
    rest_ops = [c for c in cands[top_n:] if c["typ"] in OPERATIONAL]
    if not rest_ops:
        return picked
    non_know = [c for c in picked if c["typ"] not in KNOWLEDGE]  # 可牺牲的：非知识型
    if not non_know:
        return picked  # 全知识型 → 尊重知识主导，不强插
    drop = min(non_know, key=lambda c: c["tscore"])
    add = rest_ops[0]
    return [c for c in picked if c is not drop] + [add]


async def run(queries, top_n) -> dict:
    from graphiti_core.driver.falkordb_driver import FalkorDriver  # type: ignore
    driver = FalkorDriver(
        host=os.environ.get("KG_HUB_FALKORDB_HOST", "falkordb"), port=6379,
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
        database=os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"),
    )
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {"ops_noise": {}}

    per_query = []
    agg = {"knowledge_rose": 0, "knowledge_preserved": 0, "floor_violations": 0, "queries": 0}
    for q in queries:
        rows, _, _ = await driver.execute_query(
            "CALL db.idx.fulltext.queryNodes('Episodic', $q) YIELD node, score "
            "WHERE NOT node.name STARTS WITH 'kg-hub-canonical' "
            "RETURN node.name AS name, node.content AS content, "
            "       node.source_description AS sd, score AS score "
            "ORDER BY score DESC LIMIT 40",
            q=q,
        )
        cands = []
        for r in rows:
            typ, obs = _obs_of(r.get("name") or "", r.get("content") or "", r.get("sd") or "", cfg)
            score = float(r.get("score") or 0)
            w = _weight(typ, obs, cfg)
            cands.append({"name": r.get("name"), "typ": typ, "score": round(score, 3),
                          "w": w, "tscore": round(score * w, 3)})
        if not cands:
            continue
        baseline = sorted(cands, key=lambda c: -c["score"])[:top_n]
        tiered_sorted = sorted(cands, key=lambda c: -c["tscore"])
        tiered = _tiered_topn(tiered_sorted, top_n)

        def summ(lst):
            return {"knowledge": sum(1 for c in lst if c["typ"] in KNOWLEDGE),
                    "operational": sum(1 for c in lst if c["typ"] in OPERATIONAL),
                    "discovery": sum(1 for c in lst if c["typ"] == "discovery")}
        sb, st = summ(baseline), summ(tiered)
        agg["queries"] += 1
        if st["knowledge"] > sb["knowledge"]:
            agg["knowledge_rose"] += 1
        if st["knowledge"] >= sb["knowledge"]:            # 不变量：知识型永不下降
            agg["knowledge_preserved"] += 1
        ops_cands = any(c["typ"] in OPERATIONAL for c in cands)
        all_know = all(c["typ"] in KNOWLEDGE for c in tiered)
        if ops_cands and not all_know and st["operational"] < EXPLORE_FLOOR_SLOTS:
            agg["floor_violations"] += 1                  # 应为 0
        per_query.append({
            "q": q, "baseline_mix": sb, "tiered_mix": st,
            "baseline_top": [f"{c['typ']}:{c['score']}" for c in baseline],
            "tiered_top": [f"{c['typ']}:{c['tscore']}(w{c['w']})" for c in tiered],
        })
    return {"top_n": top_n, "aggregate": agg, "per_query": per_query}


def main() -> int:
    import asyncio
    ap = argparse.ArgumentParser(description="交付分层离线 replay（只读）")
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES)
    ap.add_argument("--report", metavar="PATH")
    args = ap.parse_args()
    rep = asyncio.run(run(args.queries, args.top_n))

    a = rep["aggregate"]
    print(f"交付分层 replay（top_n={rep['top_n']}，{a['queries']} 条查询）")
    print(f"  知识型上浮的查询数            : {a['knowledge_rose']}/{a['queries']}")
    print(f"  知识型未被挤掉(不变量,应=全部): {a['knowledge_preserved']}/{a['queries']}")
    print(f"  探索地板违规(应=0)           : {a['floor_violations']}")
    for pq in rep["per_query"]:
        print(f"\n  q='{pq['q']}'  mix {pq['baseline_mix']} → {pq['tiered_mix']}")
        print(f"    baseline: {pq['baseline_top']}")
        print(f"    tiered  : {pq['tiered_top']}")
    if args.report:
        Path(args.report).write_text(json.dumps(rep, ensure_ascii=False, indent=2))
        print(f"\n[report] 已写 {args.report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
