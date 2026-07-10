"""`?` 型来源画像 —— G5 flip 前置（只读）。

G6-lite baseline 暴露：episode_search 交付 ~87% 是 `?` 型（source_description 无 type=）。
type 加权对 `?` 无杠杆（默认 1.0）。flip 前须把 `?` 拆开决定权重策略，而非拍脑袋。

本工具读图内**活跃**（未归档）Episodic，按 source_description 是否含 `type=` 分 typed/untyped，
再把 untyped 按 name 前缀/来源拆成 4 类：
  - canonical capsule (kg-hub-canonical-*)      —— 知识/文档资产，权重不该低
  - openclaw capsule  (openclaw-capsule-* 等)   —— 知识/文档资产
  - claude-mem obs 缺 type (claude-mem-obs-*)   —— 本应有 type，缺失属异常，需查
  - misc / 其它                                  —— 临时导入/杂项 snapshot，保持 1.0 或轻降
纯读，NAS 上跑。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter


def _bucket(name: str) -> str:
    n = (name or "").lower()
    if n.startswith("kg-hub-canonical"):
        return "canonical_capsule"
    if n.startswith("openclaw") or "capsule" in n:
        return "openclaw_capsule"
    if n.startswith("claude-mem-obs"):
        return "claudemem_obs_missing_type"
    return "misc"


async def run() -> dict:
    from graphiti_core.driver.falkordb_driver import FalkorDriver  # type: ignore
    d = FalkorDriver(
        host=os.environ.get("KG_HUB_FALKORDB_HOST", "falkordb"), port=6379,
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
        database=os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"),
    )

    async def q(cy, **p):
        r, _, _ = await d.execute_query(cy, **p)
        return r

    # 活跃 Episodic 的 typed / untyped
    r = await q(
        "MATCH (n:Episodic) WHERE NOT coalesce(n.archived,false) "
        "RETURN sum(CASE WHEN coalesce(n.source_description,'') CONTAINS 'type=' THEN 1 ELSE 0 END) AS typed, "
        "       sum(CASE WHEN NOT coalesce(n.source_description,'') CONTAINS 'type=' THEN 1 ELSE 0 END) AS untyped, "
        "       count(n) AS total"
    )
    typed = int(r[0].get("typed") or 0)
    untyped = int(r[0].get("untyped") or 0)
    total = int(r[0].get("total") or 0)

    # untyped 拆类 + 每类抽样 source
    rows = await q(
        "MATCH (n:Episodic) WHERE NOT coalesce(n.archived,false) "
        "AND NOT coalesce(n.source_description,'') CONTAINS 'type=' "
        "RETURN n.name AS name, n.source_description AS sd"
    )
    buckets = Counter()
    samples: dict[str, list] = {}
    for row in rows:
        b = _bucket(row.get("name") or "")
        buckets[b] += 1
        samples.setdefault(b, [])
        if len(samples[b]) < 3:
            samples[b].append({"name": (row.get("name") or "")[:60],
                               "sd": (row.get("sd") or "")[:80]})

    return {
        "active_total": total, "typed": typed, "untyped": untyped,
        "untyped_pct": round(100 * untyped / total, 1) if total else 0.0,
        "untyped_buckets": dict(buckets.most_common()),
        "samples": samples,
    }


def main() -> int:
    rep = asyncio.run(run())
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print("\n—— 小结 ——", file=sys.stderr)
    print(f"活跃 {rep['active_total']}：typed {rep['typed']} / untyped {rep['untyped']} "
          f"({rep['untyped_pct']}%)", file=sys.stderr)
    print(f"untyped 拆类：{rep['untyped_buckets']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
