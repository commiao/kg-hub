"""tools/eval_kind.py — kind 分类器准确率评测(北极星④托底)。

拿 docs/kind-calibration.json 的人工 gold_kind,对比图里各节点**当前存储的 kind**
(即分类器最近一次跑的结果),算准确率 + 列错分。纯读,不发 LLM——改 prompt/阈值后
先在容器重跑分类(backfill --retry-llm --force),再跑本工具看准确率是否回升。

用法: python -m tools.eval_kind
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

CALIB = Path(__file__).resolve().parent.parent / "docs" / "kind-calibration.json"


def _graph():
    from falkordb import FalkorDB
    db = FalkorDB(
        host=os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1"),
        port=int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379")),
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
    )
    return db.select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))


def main() -> int:
    labels = json.loads(CALIB.read_text())["labels"]
    g = _graph()
    correct = 0
    miss = []
    for lab in labels:
        rows = g.query("MATCH (n:Episodic {name:$n}) RETURN coalesce(n.kind,'(未标)'), "
                       "coalesce(n.kind_confidence,0.0)", params={"n": lab["name"]}).result_set
        got = rows[0][0] if rows else "(不存在)"
        conf = rows[0][1] if rows else 0.0
        ok = got == lab["gold_kind"]
        correct += 1 if ok else 0
        mark = "✓" if ok else "✗"
        print(f"  {mark} {lab['name'].replace('openclaw-capsule-','')[:36]:36} "
              f"gold={lab['gold_kind']:6} got={got:8}({conf:.2f})")
        if not ok:
            miss.append((lab["name"], lab["gold_kind"], got, lab.get("note", "")))
    n = len(labels)
    print(f"\n准确率: {correct}/{n} = {correct/n:.0%}")
    if miss:
        print("错分(分类器需改进的方向):")
        for name, gold, got, note in miss:
            print(f"  · 应 {gold} 判成 {got}  [{note}]")
        conf = Counter((gold, got) for _, gold, got, _ in miss)
        print("混淆(gold→got):", dict(conf))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
