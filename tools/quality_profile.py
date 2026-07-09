"""全图质量画像 —— 数据治理 step 1（只读）。

量化 kg-hub 图内 claude-mem obs 的信噪构成，用真实分布验证/证伪「大多数低质」，
给后续「设进闸 / 清存量」定量口径。见 docs/LANDING-PLAN-cognitive-asset.md。

做法：取图内 claude-mem-obs-{id} 的 id 集合，回 claude-mem.db 拉这些 id 的完整结构化
字段（type/narrative/facts/…），用多个镜头聚合：
  - type_dist：类型分布（decision/discovery 偏知识；change/bugfix/feature 偏操作日志）
  - would_reject_current：按**现行** ingest_filter（quotas=None，纯质量判定、不含限流）
    这些**已在图内**的 obs 有多少会被今天的闸挡下 = 「按当前标准属低质」的占比
  - would_reject_if_armed：候选（ops_noise.enabled=true）下的额外拦截
  - ops_noise：kg-hub 自身运维自指 bugfix
  - thin_content：narrative<120 且 facts+files<=1（低信噪代理）
  - knowledge_vs_ops：知识型 vs 操作型 type 粗分组
  - dup_clusters：同 project + 相同标题行的近重复簇
纯读，不写图/库/线上。NAS 一次性容器跑（需挂 claude-mem.db + 覆盖挂 utils/config）。
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.ingest_filter import load_config, evaluate  # noqa: E402
from utils.ops_noise import is_ops_noise  # noqa: E402

CM_DB = os.environ.get("CM_DB", "/data/cm.db")
CANON = "kg-hub-canonical"

KNOWLEDGE_TYPES = {"decision", "security_note", "security_alert"}
OPERATIONAL_TYPES = {"bugfix", "change", "feature", "refactor"}
MIXED_TYPES = {"discovery"}

_SELECT = (
    "SELECT o.id, o.project, o.type, o.title, o.subtitle, o.facts, o.narrative, "
    "o.concepts, o.files_read, o.files_modified, o.created_at, "
    "o.generated_by_model, o.relevance_count, s.platform_source "
    "FROM observations o "
    "LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id"
)


def _jlen(v) -> int:
    if not v:
        return 0
    try:
        x = json.loads(v) if isinstance(v, str) else v
        return len(x) if isinstance(x, list) else 0
    except Exception:
        return 0


async def _graph_ids(driver) -> set[int]:
    import re
    rows, _, _ = await driver.execute_query(
        "MATCH (n:Episodic) WHERE n.name STARTS WITH 'claude-mem-obs-' RETURN n.name AS name"
    )
    ids = set()
    for r in rows:
        m = re.search(r"claude-mem-obs-(\d+)", r.get("name") or "")
        if m:
            ids.add(int(m.group(1)))
    return ids


async def collect() -> dict:
    from graphiti_core.driver.falkordb_driver import FalkorDriver  # type: ignore
    driver = FalkorDriver(
        host=os.environ.get("KG_HUB_FALKORDB_HOST", "falkordb"), port=6379,
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
        database=os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"),
    )
    gids = await _graph_ids(driver)

    conn = sqlite3.connect(f"file:{CM_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(_SELECT).fetchall()]
    conn.close()
    obs = [r for r in rows if r["id"] in gids]  # 只画图内的

    cur = load_config()
    armed = copy.deepcopy(cur)
    armed.setdefault("ops_noise", {})["enabled"] = True

    type_dist = Counter()
    kvo = Counter()             # knowledge / operational / mixed / other
    reject_cur = reject_armed = ops = thin = 0
    reject_by_type = Counter()
    dup_map: Counter = Counter()
    nlens = []

    for o in obs:
        t = o.get("type") or "?"
        type_dist[t] += 1
        kvo["knowledge" if t in KNOWLEDGE_TYPES else
            "operational" if t in OPERATIONAL_TYPES else
            "mixed" if t in MIXED_TYPES else "other"] += 1

        nlen = len(o.get("narrative") or "")
        nlens.append(nlen)
        facts_files = _jlen(o.get("facts")) + _jlen(o.get("files_modified"))
        if nlen < 120 and facts_files <= 1:
            thin += 1

        # 纯质量判定：quotas=None → 跳过限流，只看 hard gate + 分数阈值
        d_cur = evaluate(o, cur, None)
        if not d_cur.would_accept:
            reject_cur += 1
            reject_by_type[t] += 1
        d_armed = evaluate(o, armed, None)
        if not d_armed.would_accept:
            reject_armed += 1
        if is_ops_noise(o, armed):
            ops += 1

        title = (o.get("title") or "").strip()
        if title:
            dup_map[(o.get("project") or "?", title)] += 1

    n = len(obs)
    dup_extra = sum(c - 1 for c in dup_map.values() if c > 1)  # 超出1份的重复份数
    dup_clusters = sum(1 for c in dup_map.values() if c > 1)

    def pct(a):
        return round(100.0 * a / n, 1) if n else 0.0

    return {
        "graph_obs_profiled": n,
        "type_dist": dict(type_dist.most_common()),
        "knowledge_vs_operational": {
            "knowledge": kvo["knowledge"], "knowledge_pct": pct(kvo["knowledge"]),
            "operational": kvo["operational"], "operational_pct": pct(kvo["operational"]),
            "mixed_discovery": kvo["mixed"], "mixed_pct": pct(kvo["mixed"]),
            "other": kvo["other"],
        },
        "would_reject_by_current_filter": reject_cur,
        "would_reject_by_current_filter_pct": pct(reject_cur),
        "reject_by_type": dict(reject_by_type.most_common()),
        "would_reject_if_ops_armed": reject_armed,
        "ops_noise": ops,
        "thin_content": thin, "thin_content_pct": pct(thin),
        "dup_clusters": dup_clusters, "dup_extra_copies": dup_extra,
        "narrative_len": {
            "median": int(statistics.median(nlens)) if nlens else 0,
            "p25": int(statistics.quantiles(nlens, n=4)[0]) if len(nlens) > 3 else 0,
            "p75": int(statistics.quantiles(nlens, n=4)[2]) if len(nlens) > 3 else 0,
        },
    }


def main() -> int:
    import asyncio
    m = asyncio.run(collect())
    print(json.dumps(m, ensure_ascii=False, indent=2))
    n = m["graph_obs_profiled"]
    kv = m["knowledge_vs_operational"]
    print("\n—— 画像小结 ——", file=sys.stderr)
    print(f"图内 obs {n} 条；知识型 {kv['knowledge_pct']}% / 操作型 {kv['operational_pct']}%"
          f" / discovery {kv['mixed_pct']}%", file=sys.stderr)
    print(f"当前闸会拒 {m['would_reject_by_current_filter_pct']}%；薄内容 {m['thin_content_pct']}%；"
          f"运维自指 {m['ops_noise']}；重复多余份 {m['dup_extra_copies']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
