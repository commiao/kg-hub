"""网关路径（graphiti.add_episode）入图后的硬约束归一：让弱模型的输出也守 schema。

出处（docs/GATEWAY-EXTRACTION-OPTIMIZATION.md 第 1 节）：存量里 deepseek 时代 64% 的边名不在 schema、
16% 实体无类型、4.6% 实体名是 commit 哈希 / 绝对路径 / 任务号——这些都不该靠模型自觉，入图后一次
cypher 就能纠正。会话自抽取路径（utils/extracted_ingest）在归一阶段做同样的事，三轮实测合规率 100%。

做四件事，全部确定性、零模型调用、失败不影响入图本身：
  1. 边名：大小写归到 schema 名；不在 schema 的改为 relates_to，原名存 raw_name（信息不丢）
  2. 实体类型兜底：没有任何 schema 标签的实体按名字规则补标签（路径/文件名 → File，配置键/配置文件 → Config，其余 → Concept）
  3. 脏命名降级：本次新建、且名字是 commit 哈希 / 绝对路径 / 纯任务号的实体，若只被本 episode 提及则连边删除
     （边 uuid 从 episode.entity_edges 摘掉）；被别的 episode 提及的只打 dirty_name 标记，不动
  4. 来源标记：episode 打 extraction_mode='gateway'、extractor_model=<路由标签>，让 extractor_compare 能按模型分组
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from utils.extracted_ingest import DIRTY_NAME_PATTERNS

_FILE_LIKE = re.compile(r"(^|/)[\w.\-]+\.(py|md|sh|json|ya?ml|ts|js|tsx|jsx|txt|toml|cfg|ini|plist|sql|go|java|rs|html|css|log)$", re.I)
_CONFIG_LIKE = re.compile(r"^(KG_HUB_|CLAUDE_MEM_|ANTHROPIC_|FASTEMBED_)[A-Z0-9_]+$|(^|/)\.env(\.|$)|\.(json|ya?ml|toml|plist|cfg|ini)$", re.I)


def canonical_edge_name(name: str, edge_types: set[str], fallback: str = "relates_to") -> tuple[str, bool]:
    """返回 (归一后的名字, 是否在 schema 内)。"""
    lower = (name or "").strip().lower()
    for canon in edge_types:
        if canon.lower() == lower:
            return canon, True
    return fallback, False


def plan_edge_renames(edges: list[dict], edge_types: set[str]) -> list[dict]:
    """edges: {uuid, name}。只返回需要改的。"""
    out = []
    for e in edges:
        canon, in_schema = canonical_edge_name(e.get("name") or "", edge_types)
        if canon != (e.get("name") or ""):
            out.append({"uuid": e["uuid"], "name": canon, "raw": e.get("name") or "", "in_schema": in_schema})
    return out


def fallback_type(name: str) -> str:
    n = (name or "").strip()
    if _CONFIG_LIKE.search(n):      # 配置键与配置文件优先于一般文件（docker-compose.yml 是 Config）
        return "Config"
    if _FILE_LIKE.search(n):
        return "File"
    return "Concept"


def is_dirty_name(name: str) -> bool:
    return any(p.search((name or "").strip()) for p in DIRTY_NAME_PATTERNS)


@dataclass
class NodePlan:
    type_fallback: dict[str, list[str]] = field(default_factory=dict)   # label -> [uuid]
    dirty_new: list[str] = field(default_factory=list)                   # 本次新建的脏命名实体
    dirty_existing: list[str] = field(default_factory=list)              # 早已存在的脏命名实体（只打标）


def plan_node_fixes(nodes: list[dict], entity_types: set[str], since: datetime) -> NodePlan:
    """nodes: {uuid, name, labels, created_at(datetime|str)}。"""
    plan = NodePlan()
    for n in nodes:
        labels = set(n.get("labels") or [])
        if not (labels & entity_types):
            plan.type_fallback.setdefault(fallback_type(n.get("name") or ""), []).append(n["uuid"])
        if is_dirty_name(n.get("name") or ""):
            created = n.get("created_at")
            if isinstance(created, str):
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=since.tzinfo)
            (plan.dirty_new if created is not None and created >= since else plan.dirty_existing).append(n["uuid"])
    return plan


_RENAME_EDGES = ("UNWIND $items AS it MATCH ()-[r:RELATES_TO {uuid: it.uuid}]->() "
                 "SET r.raw_name = it.raw, r.name = it.name RETURN count(r) AS c")
_TAG_EPISODE = ("MATCH (e:Episodic {uuid: $u}) SET e.extraction_mode = 'gateway', e.extractor_model = $label")
_MARK_DIRTY = "MATCH (n:Entity) WHERE n.uuid IN $ids SET n.dirty_name = true RETURN count(n) AS c"
_OTHER_MENTIONS = ("MATCH (n:Entity {uuid: $u}) OPTIONAL MATCH (n)<-[m:MENTIONS]-(x:Episodic) "
                   "WHERE x.uuid <> $ep RETURN count(m) AS others")
_INCIDENT_EDGES = "MATCH (n:Entity {uuid: $u})-[r:RELATES_TO]-() RETURN r.uuid AS uuid"
_DELETE_NODE = "MATCH (n:Entity {uuid: $u}) DETACH DELETE n RETURN count(*) AS c"
_UNLINK_EDGES = ("MATCH (e:Episodic {uuid: $ep}) "
                 "SET e.entity_edges = [x IN coalesce(e.entity_edges, []) WHERE NOT x IN $gone]")


async def post_ingest_normalize(driver, *, episode_uuid: str, edges: list[dict], nodes: list[dict],
                                edge_types: set[str], entity_types: set[str], since: datetime,
                                extractor_label: str) -> dict:
    """对刚入图的一条 episode 执行四项归一，返回计数。driver 只需 execute_query。"""
    stats = {"edges_renamed": 0, "edges_offschema": 0, "nodes_typed": 0, "dirty_removed": 0,
             "dirty_marked": 0, "edges_unlinked": 0}
    renames = plan_edge_renames(edges, edge_types)
    if renames:
        await driver.execute_query(_RENAME_EDGES, items=[{k: r[k] for k in ("uuid", "name", "raw")} for r in renames])
        stats["edges_renamed"] = len(renames)
        stats["edges_offschema"] = sum(1 for r in renames if not r["in_schema"])
    plan = plan_node_fixes(nodes, entity_types, since)
    for label, ids in plan.type_fallback.items():
        if label not in entity_types:
            continue
        await driver.execute_query(f"MATCH (n:Entity) WHERE n.uuid IN $ids SET n:{label} RETURN count(n) AS c", ids=ids)
        stats["nodes_typed"] += len(ids)
    gone: list[str] = []
    for u in plan.dirty_new:
        rows, _, _ = await driver.execute_query(_OTHER_MENTIONS, u=u, ep=episode_uuid)
        if rows and int(rows[0].get("others") or 0) > 0:
            plan.dirty_existing.append(u)
            continue
        erows, _, _ = await driver.execute_query(_INCIDENT_EDGES, u=u)
        gone.extend(r["uuid"] for r in erows if r.get("uuid"))
        await driver.execute_query(_DELETE_NODE, u=u)
        stats["dirty_removed"] += 1
    if gone:
        await driver.execute_query(_UNLINK_EDGES, ep=episode_uuid, gone=gone)
        stats["edges_unlinked"] = len(gone)
    if plan.dirty_existing:
        await driver.execute_query(_MARK_DIRTY, ids=plan.dirty_existing)
        stats["dirty_marked"] = len(plan.dirty_existing)
    await driver.execute_query(_TAG_EPISODE, u=episode_uuid, label=extractor_label)
    return stats
