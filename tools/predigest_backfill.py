"""tools/predigest_backfill.py — 存量长文档补拆(Phase B' 回填)。

新入图的长文档由 kg_hub_server 的预拆分流处理;本工具处理**已在图里**的
openclaw-capsule-*/openclaw-kb-* 长文档:为它们生成原子 observation 子片段
(好 fact 的来源),父文档标 predigested=true。父文档既有的烂 fact **不删**
(图手术风险大;检索侧同源限席由治理会话缓解)。catalog 类只补 kind=registry 标。

与 kg_hub_server._predigest_extract 镜像同一套 utils/predigest prompt/解析
(先例:tools/backfill_schema.py 与 server 共用 KIND_PROMPT)。

在 NAS 容器内跑(需要 ANTHROPIC_* env + falkordb localhost):
  docker exec kg-hub-server python -m tools.predigest_backfill --dry-run
  docker exec kg-hub-server python -m tools.predigest_backfill --limit 5
  docker exec kg-hub-server python -m tools.predigest_backfill --name openclaw-capsule-CAPSULE-2026-07-22-001
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graphiti_core.nodes import EpisodeType  # noqa: E402

from graphiti_client import build_graphiti  # noqa: E402
from schema import ENTITY_TYPES, EDGE_TYPES, EDGE_TYPE_MAP  # noqa: E402
from utils.predigest import (  # noqa: E402
    predigest_route, PREDIGEST_PROMPT, parse_observations, obs_to_episode_body, MAX_OBS,
)
from utils.writer_lock import async_writer_lock, WriterLockBusy  # noqa: E402

GROUP_ID = "kg_hub"


async def llm_complete(prompt: str, max_tokens: int = 3200) -> str:
    """镜像 kg_hub_server._llm_complete 的配置(百炼代理 + thinking 关)。
    max_retries=5 与 graphiti 抽取侧对齐(百炼限频抖动常见)。"""
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(
        auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN"),
        base_url=os.environ.get("ANTHROPIC_BASE_URL"),
        max_retries=5, timeout=90.0,
    )
    msg = await client.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "qwen3.6-plus"),
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"thinking": {"type": "disabled"}},
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


async def backfill_one(g, name: str, content: str, prov: str, dry: bool) -> tuple[int, int]:
    """拆一篇 → 建子片段。返回 (成功子片段数, 计划子片段数)。
    LLM 失败返回 (-1, 0)(调用方**不标** predigested,下轮重试;区别于拆空)。"""
    try:
        prompt = PREDIGEST_PROMPT.format(max_obs=MAX_OBS, body=content[:16000])
        obs_list = parse_observations(await llm_complete(prompt))
    except Exception as exc:  # noqa: BLE001 — 单篇 LLM 失败不炸整轮
        print(f"  [llm-fail] {name}: {type(exc).__name__}: {exc}(不标记,下轮重试)")
        return -1, 0
    if not obs_list:
        print(f"  [empty] {name}: LLM 拆不出(调用方标 predigest_empty,不再重试)")
        return 0, 0
    if dry:
        for i, o in enumerate(obs_list, 1):
            print(f"  [dry] {name}--obs-{i:02d}: {o['title']} ({len(o['facts'])} facts)")
        return 0, len(obs_list)
    ok = 0
    ref = datetime.now(tz=timezone.utc)
    for i, obs in enumerate(obs_list, 1):
        child = f"{name}--obs-{i:02d}"
        # 幂等:崩溃重跑按名跳过已入图子片段(add_episode 不按名去重,会双份)
        rows, _, _ = await g.driver.execute_query(
            "MATCH (e:Episodic {name: $n}) RETURN count(e) AS c", n=child)
        if rows and int(rows[0].get("c") or 0):
            ok += 1
            print(f"  [dup-skip] {child}: 已在图(上次中断的续跑)")
            continue
        try:
            async with async_writer_lock(owner=f"predigest_backfill({child})",
                                         timeout_seconds=180.0):
                result = await g.add_episode(
                    name=child, episode_body=obs_to_episode_body(obs, name),
                    source=EpisodeType.text,
                    source_description=f"predigest-backfill: {name} type={obs['type']}",
                    reference_time=ref, group_id=GROUP_ID,
                    entity_types=ENTITY_TYPES, edge_types=EDGE_TYPES,
                    edge_type_map=EDGE_TYPE_MAP)
        except (WriterLockBusy, Exception) as exc:  # noqa: BLE001
            print(f"  [fail] {child}: {type(exc).__name__}: {exc}(继续)")
            continue
        ok += 1
        try:
            cu = str(result.episode.uuid)  # type: ignore[attr-defined]
            cy = ("MATCH (e:Episodic {uuid: $u}) SET e.derived_from = $p, "
                  "e.predigest = true, e.provenance = $prov")
            if prov.startswith("external"):
                cy += ", e.verified = coalesce(e.verified, false)"
            await g.driver.execute_query(cy, u=cu, p=name, prov=prov)
        except Exception as exc:  # noqa: BLE001
            print(f"  [tag-fail] {child}: {exc}(non-fatal)")
        print(f"  [ok] {child}: {obs['title']} nodes={len(result.nodes)} edges={len(result.edges)}")
    return ok, len(obs_list)


async def amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=3, help="本轮最多补拆几篇(LLM 预算)")
    ap.add_argument("--name", default="", help="只补拆指定 episode name")
    args = ap.parse_args()

    g = await build_graphiti(fresh=False)
    where = ("WHERE (n.name STARTS WITH 'openclaw-capsule-' OR n.name STARTS WITH 'openclaw-kb-') "
             "AND NOT coalesce(n.archived, false) AND n.predigested IS NULL "
             "AND NOT n.name CONTAINS '--obs-' ")
    if args.name:
        where += "AND n.name = $name "
    rows, _, _ = await g.driver.execute_query(
        f"MATCH (n:Episodic) {where}"
        "RETURN n.name AS name, n.content AS content, coalesce(n.provenance,'firsthand') AS prov "
        "ORDER BY n.created_at DESC LIMIT $lim",
        name=args.name, lim=max(args.limit, 1) if not args.name else 1)
    print(f"[backfill] 候选 {len(rows)} 篇" + (" (dry-run)" if args.dry_run else ""))

    done = 0
    for r in rows:
        name, content, prov = r.get("name"), r.get("content") or "", r.get("prov")
        route = predigest_route(name, content)
        if route == "catalog":
            print(f"[catalog] {name} → 标 kind=registry(不拆,烂 fact 保留待检索侧限席)")
            if not args.dry_run:
                # 不覆写既有人工/LLM kind——只补空缺(审查:无条件 SET 会覆写正确分类)
                await g.driver.execute_query(
                    "MATCH (n:Episodic {name: $n}) SET n.predigested = true, "
                    "n.kind = CASE WHEN n.kind IS NULL OR n.kind = 'unclassified' "
                    "THEN 'registry' ELSE n.kind END", n=name)
            continue
        if route != "split":
            print(f"[skip] {name}: 长度不足预拆阈值")
            if not args.dry_run:
                await g.driver.execute_query(
                    "MATCH (n:Episodic {name: $n}) SET n.predigested = true", n=name)
            continue
        print(f"[split] {name} ({len(content)} chars, prov={prov})")
        ok, planned = await backfill_one(g, name, content, prov, args.dry_run)
        if not args.dry_run:
            if ok == -1:
                pass  # LLM 失败:不标记,下轮重试
            elif planned == 0:
                # 拆空:标记且注记,避免每轮占满 limit 名额烧 LLM 的队头死循环
                await g.driver.execute_query(
                    "MATCH (n:Episodic {name: $n}) "
                    "SET n.predigested = true, n.predigest_empty = true", n=name)
            elif ok == planned:
                await g.driver.execute_query(
                    "MATCH (n:Episodic {name: $n}) SET n.predigested = true", n=name)
                done += 1
            else:
                # 部分成功:不标,下轮 dup-skip 续跑剩余片段
                print(f"  [partial] {name}: {ok}/{planned},未标记,下轮续跑")
        print(f"  [summary] {name}: {ok}/{planned} 子片段入图")
    print(f"[done] 本轮补拆 {done} 篇")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
