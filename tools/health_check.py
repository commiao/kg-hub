"""图谱可用性体检器（WS-4）—— kg-hub「用起来了吗」的尺子，也是本改造的总验收工具。

见 docs/LANDING-PLAN-cognitive-asset.md。

**只读**，直连 NAS-local FalkorDB（graph "kg_hub"）。本地 Mac 连不上 FalkorDB，
必须在 NAS 容器里跑：
    docker compose -p kg-hub exec ingester python -m tools.health_check --json

指标（全部来自 group "kg_hub"）：
  total_episodes        Episodic 节点总数
  injected_ever_rate    usage_count>0 占比 = 曾被 canonical_context 注入过的占比
                        ⚠️ 非「被使用过」：usage_count 只在注入路径 bump，kg_search/
                           dashboard 读取都不 bump（见 plan 的插桩盲区）。仅作趋势。
  capsule_dormant_rate  canonical 胶囊中 usage_count=0 占比（可信沉睡信号）
  ops_noise_share       运维自指 bugfix 占比（复用 utils.ops_noise.is_ops_noise，
                        从 source_description 的 type=/project= + content 重建 obs）
  orphan_rate           零边 Entity 占比
  dup_clusters          近重复 episode 簇数（**廉价代理**：同 project + 相同标题行；
                        非语义级，仅作膨胀趋势观测）

用法：
  python -m tools.health_check                     # 人读
  python -m tools.health_check --json              # 机读
  python -m tools.health_check --baseline out.json # 存基线
  python -m tools.health_check --compare base.json # 对比基线出 diff
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.ops_noise import is_ops_noise  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "ingest_filter.json"
CANONICAL_PREFIX = "kg-hub-canonical"

_SD_TYPE = re.compile(r"type=(\S+)")
_SD_PROJECT = re.compile(r"project=(.+?)\s+type=")  # project 值可能含 '/'，取到 ' type=' 前


def _load_ops_cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 读 {CONFIG_PATH} 失败，ops_noise_share 将为 0: {exc}", file=sys.stderr)
        return {"ops_noise": {}}


def _obs_from_episodic(name: str, content: str, source_description: str) -> dict:
    """从 Episodic 节点重建 is_ops_noise 需要的 obs 形状。"""
    t = _SD_TYPE.search(source_description or "")
    p = _SD_PROJECT.search(source_description or "")
    return {
        "type": (t.group(1) if t else ""),
        "project": (p.group(1) if p else ""),
        "narrative": content or "",   # 整个 body 作可搜索文本（含 narrative/facts/Project 行）
        "facts": "",
    }


def _build_driver():
    """轻量 FalkorDriver（不加载 embedder），镜像 kg_hub_server.get_status_driver。"""
    from graphiti_core.driver.falkordb_driver import FalkorDriver  # type: ignore

    host = os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1")
    port = int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379"))
    database = os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub")
    password = os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None
    return FalkorDriver(host=host, port=port, password=password, database=database)


async def _q(driver, cypher: str, **params):
    rows, _, _ = await driver.execute_query(cypher, **params)
    return rows


async def collect() -> dict:
    driver = _build_driver()
    ops_cfg = _load_ops_cfg()

    # --- episode 总数 + 注入覆盖 ---
    r = await _q(
        driver,
        "MATCH (n:Episodic) RETURN count(n) AS total, "
        "sum(CASE WHEN coalesce(n.usage_count,0) > 0 THEN 1 ELSE 0 END) AS injected",
    )
    total = int(r[0].get("total") or 0)
    injected = int(r[0].get("injected") or 0)

    # --- canonical 胶囊沉睡 ---
    rc = await _q(
        driver,
        "MATCH (n:Episodic) WHERE n.name STARTS WITH $pfx "
        "RETURN count(n) AS total, "
        "sum(CASE WHEN coalesce(n.usage_count,0) = 0 THEN 1 ELSE 0 END) AS starved",
        pfx=CANONICAL_PREFIX,
    )
    cap_total = int(rc[0].get("total") or 0)
    cap_starved = int(rc[0].get("starved") or 0)

    # --- ops_noise：拉全量 Episodic 重建 obs 后过分类器 ---
    rows = await _q(
        driver,
        "MATCH (n:Episodic) WHERE NOT n.name STARTS WITH $pfx "
        "RETURN n.name AS name, n.content AS content, "
        "       n.source_description AS sd",
        pfx=CANONICAL_PREFIX,
    )
    ops_noise = 0
    dup_map: dict[tuple[str, str], int] = {}
    for row in rows:
        obs = _obs_from_episodic(row.get("name") or "", row.get("content") or "",
                                 row.get("sd") or "")
        if is_ops_noise(obs, ops_cfg):
            ops_noise += 1
        # 廉价 dup 代理：同 project + 相同标题行（content 首行 = "[TYPE] title"）
        title = (row.get("content") or "").splitlines()[0].strip() if row.get("content") else ""
        key = (obs["project"], title)
        if title:
            dup_map[key] = dup_map.get(key, 0) + 1
    dup_clusters = sum(1 for _k, c in dup_map.items() if c > 1)

    # --- orphan 实体 ---
    ro = await _q(
        driver,
        "MATCH (n:Entity) OPTIONAL MATCH (n)-[e]-() "
        "WITH n, count(e) AS deg "
        "RETURN count(n) AS total, sum(CASE WHEN deg = 0 THEN 1 ELSE 0 END) AS orphans",
    )
    ent_total = int(ro[0].get("total") or 0)
    orphans = int(ro[0].get("orphans") or 0)

    def rate(a: int, b: int) -> float:
        return round(100.0 * a / b, 2) if b else 0.0

    return {
        "total_episodes": total,
        "injected_ever_count": injected,
        "injected_ever_rate_pct": rate(injected, total),  # ⚠️ 注入覆盖，非使用率
        "capsule_total": cap_total,
        "capsule_dormant_count": cap_starved,
        "capsule_dormant_rate_pct": rate(cap_starved, cap_total),
        "ops_noise_count": ops_noise,
        "ops_noise_share_pct": rate(ops_noise, total),
        "entity_total": ent_total,
        "orphan_count": orphans,
        "orphan_rate_pct": rate(orphans, ent_total),
        "dup_clusters_approx": dup_clusters,
    }


def _fmt_human(m: dict) -> str:
    return (
        "kg-hub 可用性体检\n"
        f"  episodes 总数        : {m['total_episodes']}\n"
        f"  注入覆盖(非使用率!)  : {m['injected_ever_rate_pct']}%  ({m['injected_ever_count']})\n"
        f"  胶囊沉睡率           : {m['capsule_dormant_rate_pct']}%  "
        f"({m['capsule_dormant_count']}/{m['capsule_total']})\n"
        f"  运维自指 bugfix 占比 : {m['ops_noise_share_pct']}%  ({m['ops_noise_count']})\n"
        f"  孤儿实体率           : {m['orphan_rate_pct']}%  ({m['orphan_count']}/{m['entity_total']})\n"
        f"  近重复簇(廉价代理)   : {m['dup_clusters_approx']}\n"
    )


def _diff(base: dict, cur: dict) -> str:
    keys = [k for k in cur if k.endswith(("_pct", "_count", "_episodes", "_clusters_approx", "_total"))]
    lines = ["指标对比（基线 → 当前）:"]
    for k in keys:
        b, c = base.get(k), cur.get(k)
        if b is None:
            lines.append(f"  {k}: (基线无) → {c}")
        elif b != c:
            arrow = "↑" if isinstance(c, (int, float)) and c > b else "↓"
            lines.append(f"  {k}: {b} → {c}  {arrow}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="kg-hub 图谱可用性体检（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--baseline", metavar="PATH", help="把结果写为基线文件")
    ap.add_argument("--compare", metavar="PATH", help="与基线文件对比出 diff")
    args = ap.parse_args()

    metrics = asyncio.run(collect())

    if args.compare:
        base = json.loads(Path(args.compare).read_text())
        print(_diff(base.get("metrics", base), metrics))
    if args.baseline:
        Path(args.baseline).write_text(
            json.dumps(
                {"generated_at": datetime.now(timezone.utc).isoformat(), "metrics": metrics},
                ensure_ascii=False, indent=2,
            )
        )
        print(f"[baseline] 已写 {args.baseline}", file=sys.stderr)
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    elif not args.compare:
        print(_fmt_human(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
