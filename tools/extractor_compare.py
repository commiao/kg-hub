"""tools/extractor_compare.py — 按抽取来源（模型）并排对比主图里的内容质量。

分组键：episode 的 ``extractor_model``（会话自抽取路径写入）；没有该字段的旧内容按
``created_at`` 月份归为 ``legacy:<YYYY-MM>``（对应网关时代的模型：06–08 qwen3.6-plus、
09 混合、10 deepseek，见 docs/gateway-model-quality-20261017.md）。
网关单次抽取（KG_HUB_INGEST_MODE=single_pass）的 episode/节点/边带 ``extraction_mode='gateway_single'``，
它们的 extractor_model 与 graphiti 模式归一后打的是同一个路由标签，所以单独成组：
``gateway_single:<extractor_model>``，这样同一路由模型的两种链路能并排比。

指标与报告第 3 节、tools/digest_retro.py 同口径：实体/条、边/条、零边占比、边名合规率
（schema 15 个名，大小写不敏感）、不同边名数、重复三元组、实体有类型、脏命名、摘要均长。
边和实体按各自的 ``extractor_model`` 分组（旧内容同样按月份）。

只读。在 kg-hub-server 容器内跑：
  python3 -m tools.extractor_compare            # markdown 表
  python3 -m tools.extractor_compare --json     # 机器可读
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.digest_retro import schema_names, DIRTY_PREFIXES  # noqa: E402

GATEWAY_SINGLE_PREFIX = "gateway_single:"


def _src(var: str) -> str:
    """分组键表达式：gateway_single 单列，其余按 extractor_model，没有就按月份归 legacy。"""
    return (f"CASE WHEN {var}.extraction_mode = 'gateway_single' "
            f"THEN '{GATEWAY_SINGLE_PREFIX}' + coalesce({var}.extractor_model, '?') "
            f"ELSE coalesce({var}.extractor_model, 'legacy:' + substring(toString({var}.created_at),0,7)) END")


_EP = ("MATCH (e:Episodic) WHERE e.group_id = $g "
       "OPTIONAL MATCH (e)-[:MENTIONS]->(n) WITH e, count(n) AS k "
       f"RETURN {_src('e')} AS src, "
       "count(e) AS eps, sum(k) AS mentions, sum(size(coalesce(e.entity_edges, []))) AS edges, "
       "sum(CASE WHEN k = 0 THEN 1 ELSE 0 END) AS zero_entities")
_EDGES = ("MATCH (a)-[r:RELATES_TO]->(b) WHERE r.group_id = $g "
          f"WITH {_src('r')} AS src, "
          "a.uuid+'|'+toLower(r.name)+'|'+b.uuid AS k, toLower(r.name) AS n, count(r) AS c "
          "RETURN src, n AS name, sum(c) AS count, sum(CASE WHEN c>1 THEN c-1 ELSE 0 END) AS dups")
_ENTS = ("MATCH (n:Entity) WHERE n.group_id = $g "
         f"RETURN {_src('n')} AS src, "
         "n.name AS name, size(coalesce(n.summary,'')) AS summary_len, labels(n) AS l")


def aggregate(ep_rows, edge_rows, ent_rows) -> dict[str, dict]:
    edge_names, entity_types = schema_names()
    out: dict[str, dict] = collections.defaultdict(dict)
    for r in ep_rows:
        eps = r["eps"] or 0
        out[r["src"]].update({
            "episodes": eps,
            "entities_per_episode": round((r["mentions"] or 0) / eps, 2) if eps else None,
            "edges_per_episode": round((r["edges"] or 0) / eps, 2) if eps else None,
            "zero_entity_share": round((r["zero_entities"] or 0) / eps, 4) if eps else None,
        })
    acc = collections.defaultdict(lambda: [0, 0, 0, set()])
    for r in edge_rows:
        a = acc[r["src"]]
        a[0] += r["count"]; a[1] += r["count"] if (r["name"] or "") in edge_names else 0
        a[2] += r["dups"]; a[3].add(r["name"])
    for src, a in acc.items():
        out[src].update({"edges": a[0], "edge_in_schema": round(a[1] / a[0], 4) if a[0] else None,
                         "dup_triple_share": round(a[2] / a[0], 4) if a[0] else None,
                         "edge_distinct_names": len(a[3])})
    ent = collections.defaultdict(lambda: [0, 0, 0, 0])
    for r in ent_rows:
        e = ent[r["src"]]
        e[0] += 1
        e[1] += 1 if any(x in entity_types for x in (r["l"] or [])) else 0
        e[2] += 1 if (r["name"] or "").startswith(DIRTY_PREFIXES) else 0
        e[3] += r["summary_len"] or 0
    for src, e in ent.items():
        out[src].update({"entities": e[0], "untyped_share": round((e[0] - e[1]) / e[0], 4) if e[0] else None,
                         "dirty_name_share": round(e[2] / e[0], 4) if e[0] else None,
                         "summary_len_avg": round(e[3] / e[0], 1) if e[0] else None})
    return dict(out)


_COLS = [("episodes", "episodes"), ("entities_per_episode", "实体/条"), ("edges_per_episode", "边/条"),
         ("zero_entity_share", "零实体"), ("edge_in_schema", "边名合规"), ("edge_distinct_names", "边名种类"),
         ("dup_triple_share", "重复三元组"), ("untyped_share", "无类型实体"), ("dirty_name_share", "脏命名"),
         ("summary_len_avg", "摘要均长")]


def render(table: dict[str, dict]) -> str:
    def f(k, v):
        if v is None:
            return "—"
        return f"{v:.1%}" if k.endswith("_share") or k == "edge_in_schema" else f"{v}"
    lines = ["| 来源 | " + " | ".join(l for _, l in _COLS) + " |", "|---|" + "---|" * len(_COLS)]
    for src in sorted(table, key=lambda s: (not s.startswith("legacy:"), s)):
        m = table[src]
        lines.append(f"| {src} | " + " | ".join(f(k, m.get(k)) for k, _ in _COLS) + " |")
    return "\n".join(lines) + "\n"


async def collect(driver, group_id: str = "kg_hub") -> dict[str, dict]:
    async def q(cy):
        rows, _, _ = await driver.execute_query(cy, g=group_id)
        return rows
    return aggregate(await q(_EP), await q(_EDGES), await q(_ENTS))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--group", default="kg_hub")
    a = ap.parse_args(argv)
    from graphiti_client import build_graphiti  # 容器内

    async def run():
        g = await build_graphiti()
        try:
            return await collect(g.driver, a.group)
        finally:
            await g.driver.close()
    table = asyncio.run(run())
    sys.stdout.write(json.dumps(table, ensure_ascii=False, indent=2) if a.json else render(table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
