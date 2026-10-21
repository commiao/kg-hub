"""tools/vector_index_check.py — RELATES_TO.fact_embedding 向量索引验收（T-0345）。

对 docs/golden-queries.json 的前 N 条查询，串行比较两种取法的前 50 条边：
  索引：db.idx.vector.queryRelationships 取 2048 个候选，再按 graphiti 同一公式精确重排
  全扫：graphiti 原来的做法，MATCH 全部 RELATES_TO 逐条算 vec.cosineDistance
索引结果不重复、且没漏掉全扫前 30 名才算该条通过（31–50 名只报告）。另报索引的 numDocuments / maxDocId：maxDocId 远大于
numDocuments 说明向量被大量重写过（换模型重算），2026-10-22 实测这种索引会漏边，要重建。

全扫一次约 1 秒且让 NAS 升温，所以默认只跑 8 条、每条之间停 --pause 秒；别在高峰跑。
用法：python -m tools.vector_index_check [--n 8] [--pause 5]
只读：只发 ro_query。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GOLDEN = Path(__file__).resolve().parent.parent / "docs" / "golden-queries.json"
TOP = 50
CANDIDATES = 2048            # 与 graphiti_client.SEMANTIC_CANDIDATES 一致（测试对账）
TIE = 1e-6
# 新建的索引偶尔漏第 40 名之后的边（HNSW 是近似的），语义腿去重后只取 40 条、
# 235 条评测查询输出与全扫逐条相同；被重写过的索引会漏到前几名。所以门槛只管前 30 名。
GATE = 30
_SCORE = "(2 - vec.cosineDistance(e.fact_embedding, vecf32($v)))/2"
INDEXED = ("CALL db.idx.vector.queryRelationships('RELATES_TO', 'fact_embedding', $k, vecf32($v)) "
           "YIELD relationship AS e WITH count(e) AS hits, collect(DISTINCT e) AS uniq UNWIND uniq AS e "
           f"WITH hits, size(uniq) AS distinct_hits, e, {_SCORE} AS score WHERE score > 0.6 "
           "RETURN e.uuid AS u, score, hits, distinct_hits ORDER BY score DESC LIMIT $top")
SCAN = (f"MATCH ()-[e:RELATES_TO]->() WITH e, {_SCORE} AS score WHERE score > 0.6 "
        "RETURN e.uuid AS u, score ORDER BY score DESC LIMIT $top")


def compare(indexed_rows, scan_rows) -> dict:
    """一条查询的结论。indexed_rows: [(uuid, score, hits, distinct_hits)]；scan_rows: [(uuid, score)]。

    第 TOP 名附近常有并列分（同一句 fact 落在多条边上，向量相同），两种取法截断到不同的边
    不算漏；只有分数高于索引结果最低分的边没取到才算。"""
    got = {r[0] for r in indexed_rows}
    floor = min((r[1] for r in indexed_rows), default=float("inf"))
    dup = bool(indexed_rows) and indexed_rows[0][2] != indexed_rows[0][3]
    missed = [i + 1 for i, (u, score) in enumerate(scan_rows)
              if u not in got and (score > floor + TIE or len(indexed_rows) < len(scan_rows))]
    return {"ok": not dup and not any(r <= GATE for r in missed), "duplicates": dup,
            "missed_ranks": missed}


def index_stats(g) -> dict:
    for row in g.ro_query("CALL db.indexes()").result_set:
        if row[0] == "RELATES_TO":
            info = row[-1] if isinstance(row[-1], dict) else {}
            return {k: info.get(k) for k in ("numDocuments", "maxDocId")}
    return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=8, help="跑几条黄金查询（默认 8）")
    ap.add_argument("--pause", type=float, default=5.0, help="每条之间停几秒，给 NAS 降温")
    args = ap.parse_args(argv)

    from fastembed import TextEmbedding
    from tools.reembed import graph
    from utils.embedding_profile import active_profile

    kwargs = {"model_name": active_profile().model}
    if os.environ.get("FASTEMBED_CACHE_PATH"):
        kwargs["cache_dir"] = os.environ["FASTEMBED_CACHE_PATH"]
    model = TextEmbedding(**kwargs)
    g = graph()
    queries = [q["query"] for q in json.load(open(GOLDEN))["queries"]][:args.n]
    print("索引:", index_stats(g), flush=True)
    bad = 0
    for q in queries:
        v = [float(x) for x in next(iter(model.embed([q.replace("\n", " ")])))]
        t = time.time()
        rows = g.ro_query(INDEXED, {"v": v, "k": CANDIDATES, "top": TOP}).result_set
        t_idx = time.time() - t
        t = time.time()
        ref = g.ro_query(SCAN, {"v": v, "top": TOP}).result_set
        t_scan = time.time() - t
        res = compare(rows, ref)
        bad += not res["ok"]
        print(f"{'通过' if res['ok'] else '不符'}  索引 {t_idx * 1000:.0f}ms 全扫 {t_scan * 1000:.0f}ms "
              f"重复={res['duplicates']} 漏掉全扫名次={res['missed_ranks']}  {q}", flush=True)
        time.sleep(args.pause)
    print(f"{len(queries) - bad}/{len(queries)} 条一致", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
