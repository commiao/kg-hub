"""tools/eval_recall.py — 召回基线评测器(北极星③"先度量再建设")。

用 docs/golden-queries.json 的黄金查询集,对某个检索端点算召回:
  gold(相关集) = episode.content(toLower)子串含 gold_kw 的 Episodic
  某 gold episode 被"召回" = 该端点返回的 fact 里,有一条属于它(edge.episodes 含其 uuid)
  recall@k = 命中的 gold / gold 总数;hit = recall>0

按 mode(literal / nl)分组汇总——两组差值 = 语义检索要补的空间。
可对比多个端点(默认 /api/search 子串;--endpoint /api/search_semantic 试向量)。

用法:
  python -m tools.eval_recall                      # 评 /api/search,k=10
  python -m tools.eval_recall --k 20 --endpoint /api/search_semantic
只读,不改图。
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

GOLDEN = Path(__file__).resolve().parent.parent / "docs" / "golden-queries.json"


def _graph():
    from falkordb import FalkorDB
    db = FalkorDB(
        host=os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1"),
        port=int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379")),
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
    )
    return db.select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))


def gold_uuids(g, kw: str, cap: int = 60) -> set[str]:
    rows = g.query(
        "MATCH (n:Episodic) WHERE toLower(coalesce(n.content,'')) CONTAINS toLower($k) "
        "RETURN n.uuid LIMIT $cap", params={"k": kw, "cap": cap}).result_set
    return {r[0] for r in rows}


def recalled_gold(g, returned_facts: list[str], gold: set[str]) -> set[str]:
    """哪些 gold episode 在返回的 fact 里露面(edge.episodes 含其 uuid)。"""
    if not returned_facts or not gold:
        return set()
    rows = g.query(
        "MATCH ()-[e:RELATES_TO]->() WHERE e.fact IN $facts AND e.episodes IS NOT NULL "
        "RETURN e.episodes", params={"facts": returned_facts}).result_set
    hit = set()
    for (eps,) in rows:
        for u in (eps or []):
            if str(u) in gold:
                hit.add(str(u))
    return hit


def search(base: str, tok: str, endpoint: str, q: str, k: int) -> list[str]:
    # bump=0:评估跑分不计入 usage 统计(检索即 bump 是给真实取用的)
    url = f"{base}{endpoint}?" + urllib.parse.urlencode({"q": q, "num_results": k, "bump": 0})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"} if tok else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        return [x.get("fact") for x in d.get("results", []) if x.get("fact")]
    except Exception as e:
        print(f"    [search-fail] {type(e).__name__}: {e}")
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--endpoint", default="/api/search")
    ap.add_argument("--base", default=os.environ.get("KG_HUB_URL", "http://100.123.208.32:17171"))
    args = ap.parse_args()
    tok = os.environ.get("KG_HUB_TOKEN") or os.environ.get("KG_HUB_API_TOKEN") or ""

    g = _graph()
    queries = json.loads(GOLDEN.read_text())["queries"]
    print(f"评测 {args.endpoint} k={args.k} · {len(queries)} 条黄金查询\n")
    by_mode: dict[str, list[float]] = {}
    for q in queries:
        gold = gold_uuids(g, q["gold_kw"])
        facts = search(args.base, tok, args.endpoint, q["query"], args.k)
        hit = recalled_gold(g, facts, gold)
        recall = len(hit) / len(gold) if gold else 0.0
        by_mode.setdefault(q["mode"], []).append(recall)
        flag = "" if recall > 0 else "  ← 0 召回"
        print(f"  [{q['mode']:7}] {q['id']:22} gold={len(gold):3} 返回facts={len(facts):3} "
              f"recall@{args.k}={recall:.0%}{flag}")

    print("\n=== 汇总 ===")
    for mode, rs in sorted(by_mode.items()):
        answered = sum(1 for r in rs if r > 0)
        print(f"  {mode:7}: 命中率 {answered}/{len(rs)} · 平均 recall@{args.k}={sum(rs)/len(rs):.0%}")
    allr = [r for rs in by_mode.values() for r in rs]
    ans = sum(1 for r in allr if r > 0)
    print(f"  全部   : 命中率 {ans}/{len(allr)} · 平均 recall@{args.k}={sum(allr)/len(allr):.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
