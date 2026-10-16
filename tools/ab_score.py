"""tools/ab_score.py — 实体抽取模型 A/B 的并排评分（只读）。

给定若干条路由（每条路由一个独立 group_id，如 kg_hub_ab_deepseek / kg_hub_ab_qwen /
kg_hub_ab_claude），从 FalkorDB 按同一口径算指标，输出 markdown 并排对比表。指标函数直接
复用 tools.digest_retro.graph_metrics，口径与 docs/gateway-model-quality-20261017.md 第 3 节、
每 500 条复盘报告一致：边名合规率（schema 15 个名，大小写不敏感）、不同边名数、重复三元组、
无类型实体、脏命名（commit/绝对路径/任务号）、摘要均长、零边 episode 占比；再加覆盖率
（已入图 episode / 样本数）与账本侧的每条调用数、wall time、阶段分布。

只读：不写图、不发模型调用、不动断路器。FalkorDB 不支持 =~ 正则，查询全部用等值/IN。

用法（在 kg-hub-server 容器内跑才能拿到图库；账本可选）：
  python -m tools.ab_score \
      --route deepseek=kg_hub_ab_deepseek --route qwen=kg_hub_ab_qwen --route claude=kg_hub_ab_claude \
      [--database kg_hub_ab]                                  # 各路由所在的 FalkorDB 图名，默认主图
      [--expected 50]                                         # 样本条数，算覆盖率
      [--journal /backup/model-attempts.sqlite3 --sample docs/ab-sample-20261017.json \
       --business-key deepseek=kg_hub.entity_extract --business-key claude=kg_hub.entity_extract_ab]
      [--json ab-score.json] [--top-offschema 8]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.digest_retro import graph_metrics, schema_names, _q  # noqa: E402

# (指标键, 表头, 方向)。方向：higher/lower = 越高/越低越好，None = 只展示不评优。
ROWS: list[tuple[str, str, str | None]] = [
    ("coverage", "覆盖率（入图/样本）", "higher"),
    ("entities_per_episode", "实体/条", None),
    ("edges_per_episode", "边/条", None),
    ("zero_edge_share", "零边 episode", "lower"),
    ("edge_in_schema", "边名合规率", "higher"),
    ("edge_distinct_names", "边名种类", "lower"),
    ("dup_triple_share", "重复三元组", "lower"),
    ("untyped_share", "无类型实体", "lower"),
    ("dirty_name_share", "脏命名", "lower"),
    ("summary_len_avg", "摘要均长", None),
    ("calls_mean", "调用/条", "lower"),
    ("wall_med_s", "每条中位 s", "lower"),
    ("wall_p90_s", "每条 p90 s", "lower"),
]
SHARE_KEYS = {"coverage", "zero_edge_share", "edge_in_schema", "dup_triple_share",
              "untyped_share", "dirty_name_share"}


# ---------- 解析 ----------

def parse_pairs(items: list[str], what: str) -> dict[str, str]:
    """label=value 列表 → dict；重复 label 或缺 '=' 直接报错。"""
    out: dict[str, str] = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"--{what} 需要 label=value：{it}")
        k, v = (x.strip() for x in it.split("=", 1))
        if not k or not v:
            raise ValueError(f"--{what} 的 label 与 value 都不能为空：{it}")
        if k in out:
            raise ValueError(f"--{what} 重复的 label：{k}")
        out[k] = v
    return out


# ---------- 图库（只读） ----------

async def load_group_graph(driver, group_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    """按 group_id 取 episodes / 按小写名聚合的边 / 实体，形状与 digest_retro.load_graph 相同。"""
    _, entity_types = schema_names()

    async def q(cy, **p):
        rows, _, _ = await driver.execute_query(cy, **p)
        return rows

    eps = await q(
        "MATCH (e:Episodic) WHERE e.group_id = $g "
        "OPTIONAL MATCH (e)-[:MENTIONS]->(n) WITH e, count(n) AS k "
        "OPTIONAL MATCH ()-[r:RELATES_TO]->() WHERE r.group_id = $g AND e.uuid IN r.episodes "
        "RETURN e.uuid AS u, k AS entities, count(r) AS edges", g=group_id)
    edges = await q(
        "MATCH (a)-[r:RELATES_TO]->(b) WHERE r.group_id = $g "
        "WITH a.uuid+'|'+toLower(r.name)+'|'+b.uuid AS k, toLower(r.name) AS n, count(r) AS c "
        "RETURN n AS name, sum(c) AS count, sum(CASE WHEN c>1 THEN c-1 ELSE 0 END) AS dups", g=group_id)
    ents = await q(
        "MATCH (n:Entity) WHERE n.group_id = $g "
        "RETURN n.name AS name, size(coalesce(n.summary,'')) AS summary_len, labels(n) AS l", g=group_id)
    return (
        [{"entities": r["entities"], "edges": r["edges"]} for r in eps],
        [{"name": r["name"], "count": r["count"], "dups": r["dups"]} for r in edges],
        [{"name": r["name"], "summary_len": r["summary_len"],
          "typed": any(x in entity_types for x in (r["l"] or []))} for r in ents],
    )


def offschema_edges(edges: list[dict], top: int = 8) -> list[tuple[str, int]]:
    """schema 外的边名按出现次数倒序，供复盘写「自创边名」证据。"""
    names, _ = schema_names()
    xs = [(e["name"] or "", int(e["count"])) for e in edges if (e["name"] or "").lower() not in names]
    return sorted(xs, key=lambda x: (-x[1], x[0]))[:top]


# ---------- 账本（只读，走 (source_description, source_obs_id) 索引逐条取） ----------

def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def journal_route_metrics(journal_path: str | Path, business_key: str,
                          sample_items: list[dict]) -> dict:
    """样本每条观测在该业务 key 下的 completed 调用数、wall（首条 created_at → 末条 updated_at）、阶段分布。"""
    conn = sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=1")
    calls, walls, stages, with_calls = [], [], {}, 0
    for it in sample_items:
        rows = conn.execute(
            "SELECT stage, created_at, updated_at FROM model_attempts "
            "WHERE source_description=? AND source_obs_id=? AND business_key=? AND phase='completed'",
            (it["source_description"], it["source_obs_id"], business_key)).fetchall()
        if not rows:
            continue
        with_calls += 1
        calls.append(len(rows))
        for st, _, _ in rows:
            stages[st or "unknown"] = stages.get(st or "unknown", 0) + 1
        starts = [t for t in (_parse_ts(r[1]) for r in rows) if t]
        ends = [t for t in (_parse_ts(r[2]) for r in rows) if t]
        if starts and ends:
            walls.append((max(ends) - min(starts)).total_seconds())
    conn.close()
    total = sum(stages.values())
    return {
        "obs_with_calls": with_calls,
        "calls_mean": round(sum(calls) / len(calls), 2) if calls else None,
        "calls_med": statistics.median(calls) if calls else None,
        "wall_med_s": round(statistics.median(walls), 1) if walls else None,
        "wall_p90_s": round(_q(walls, 0.9), 1) if walls else None,
        "stage_share": {k: round(v / total, 3) for k, v in sorted(stages.items())} if total else {},
    }


# ---------- 汇总与渲染 ----------

def score_route(eps: list[dict], edges: list[dict], ents: list[dict], *,
                expected: int | None = None, journal: dict | None = None, top: int = 8) -> dict:
    m = graph_metrics(eps, edges, ents)
    m["coverage"] = round(m["episodes"] / expected, 4) if expected else None
    m["offschema_top"] = offschema_edges(edges, top)
    if journal:
        m.update(journal)
    return m


def _fmt(k: str, v) -> str:
    if v is None:
        return "—"
    if k in SHARE_KEYS:
        return f"{v:.1%}"
    return f"{v}"


def best_label(row_key: str, direction: str | None, values: dict[str, object]) -> str | None:
    """一行里哪条路由最好；方向为 None、全缺、或并列时返回 None。"""
    if direction is None:
        return None
    xs = {k: v for k, v in values.items() if isinstance(v, (int, float))}
    if len(xs) < 2:
        return None
    pick = max(xs.items(), key=lambda kv: kv[1]) if direction == "higher" else min(xs.items(), key=lambda kv: kv[1])
    if sum(1 for v in xs.values() if v == pick[1]) > 1:
        return None
    return pick[0]


def render_table(routes: dict[str, dict], title: str = "实体抽取模型 A/B 并排对比") -> str:
    labels = list(routes)
    lines = [f"# {title}", "", "| 指标 | " + " | ".join(labels) + " | 最优 |",
             "|---|" + "---|" * (len(labels) + 1)]
    for key, head, direction in ROWS:
        vals = {l: routes[l].get(key) for l in labels}
        best = best_label(key, direction, vals)
        lines.append(f"| {head} | " + " | ".join(_fmt(key, vals[l]) for l in labels)
                     + f" | {best or '—'} |")
    lines += ["", "读法：覆盖率低于 100% 的路由，其余行只代表已入图的那部分；最优列按方向判定，并列或方向无意义的行留空。", ""]
    for l in labels:
        m = routes[l]
        lines += [f"## {l}", "",
                  f"- episodes {m.get('episodes')}，entities {m.get('entities')}，edges {m.get('edges')}"]
        if m.get("stage_share"):
            lines.append("- 调用阶段分布：" + ", ".join(f"{k} {v:.0%}" for k, v in m["stage_share"].items()))
        top = m.get("offschema_top") or []
        lines.append("- schema 外边名 top：" + (", ".join(f"`{n}`×{c}" for n, c in top) if top else "无"))
        lines.append("")
    return "\n".join(lines)


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--route", action="append", default=[], required=True, help="label=group_id，可多次")
    ap.add_argument("--database", default=None, help="FalkorDB 图名（默认 KG_HUB_FALKORDB_DATABASE / kg_hub）")
    ap.add_argument("--expected", type=int, default=None, help="样本条数，用于覆盖率")
    ap.add_argument("--journal", help="model-attempts.sqlite3 路径（可选）")
    ap.add_argument("--sample", help="docs/ab-sample-20261017.json（与 --journal 搭配）")
    ap.add_argument("--business-key", action="append", default=[], help="label=business_key，可多次")
    ap.add_argument("--json", dest="json_out", help="把各路由指标写成 JSON")
    ap.add_argument("--top-offschema", type=int, default=8)
    a = ap.parse_args(argv)

    try:
        routes = parse_pairs(a.route, "route")
        keys = parse_pairs(a.business_key, "business-key")
    except ValueError as exc:
        raise SystemExit(str(exc))
    sample_items: list[dict] = []
    if a.journal:
        if not a.sample:
            raise SystemExit("--journal 需要同时给 --sample")
        sample_items = json.loads(Path(a.sample).read_text())["items"]
        if a.expected is None:
            a.expected = len(sample_items)

    from graphiti_client import build_graphiti  # 容器内
    kwargs = {"database": a.database} if a.database else {}
    g = asyncio.run(build_graphiti(**kwargs))
    out: dict[str, dict] = {}
    try:
        for label, gid in routes.items():
            eps, edges, ents = asyncio.run(load_group_graph(g.driver, gid))
            jm = journal_route_metrics(a.journal, keys[label], sample_items) \
                if (a.journal and label in keys) else None
            out[label] = score_route(eps, edges, ents, expected=a.expected, journal=jm, top=a.top_offschema)
            out[label]["group_id"] = gid
    finally:
        asyncio.run(g.driver.close())

    if a.json_out:
        Path(a.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=list))
    sys.stdout.write(render_table(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
