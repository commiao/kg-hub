"""预抽取入库：调用方自带实体/关系，服务端只做嵌入、消解、写图，不调模型。

为什么要有这条路（2026-10-17 用户决定）：对比「旧模型经网关抽取的存量」与「Claude 会话自行
抽取的新内容」，期间 ``kg_hub.entity_extract`` 断路器保持断开，所以抽取不能再经
``graphiti.add_episode``（那条路的每一步都要打模型）。这条路由 Claude Code 会话把观测抽成
schema v0.2 的实体/关系 JSON，POST 过来；服务端按 graphiti 0.29 同样的图结构落图，
并在 episode / 新实体 / 新边上打 ``extractor_model`` 来源标记，使新旧内容可按模型分组对比。

与 /api/ingest 共享的治理件（备份、IngestedKey 幂等、task_executions 记账、写锁、
origin/kind 打标）都在 kg_hub_server 里复用；本模块只负责**纯逻辑**（校验/归一）和**写图**。

图结构与 graphiti_core 0.29.0 的 save 语句逐字段一致（节点 ``SET n = $entity_data`` +
``vecf32`` 向量；边 ``MERGE (a)-[e:RELATES_TO {uuid}]->(b)``；``MENTIONS`` 边），所以
kg_search / episode_search / normalize_edge_names / quality 画像都把它当普通数据。

消解策略（不调模型，只用本地 bge-small 向量）：
  实体  先按同 group 内名字大小写不敏感精确匹配；再按 name_embedding 余弦 ≥ NODE_MATCH_MIN
        取最相似一个；都没有才建新节点。复用的旧节点**不改**其 summary/labels。
  边    同 (源, 目标, 边名) 已有 RELATES_TO → 只把本 episode 追加进 e.episodes；否则建新边。
纪律度量在归一阶段就记下来（schema 外边名、未知实体类型、悬空边、重复三元组），
随响应返回，供每 500 条复盘用；schema 外边名映射为 relates_to 并把原名保留在 raw_name。
"""
from __future__ import annotations

import re
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel, Field

NODE_MATCH_MIN = 0.92        # bge-small 384 维上同名异写（大小写/空格）通常 ≥0.95
MAX_ENTITIES = 60
MAX_EDGES = 120
MAX_NAME_LEN = 200
MAX_SUMMARY_LEN = 2000
MAX_FACT_LEN = 1000
EXTRACTOR_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}\Z")
# 作为独立实体没有意义的名字：commit 哈希、绝对路径、任务号（见报告第 3 节「脏命名」）。
DIRTY_NAME_PATTERNS = (
    re.compile(r"^(commit\s+)?[0-9a-f]{7,40}$", re.I),
    re.compile(r"^/(Users|volume|home|tmp|private)/"),
    re.compile(r"^T-\d{3,5}$"),
)


class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    type: str = Field(min_length=1, max_length=40)
    summary: str = Field("", max_length=MAX_SUMMARY_LEN)


class ExtractedEdge(BaseModel):
    source: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    target: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    name: str = Field(min_length=1, max_length=80)
    fact: str = Field(min_length=1, max_length=MAX_FACT_LEN)


@dataclass
class NormalizedEntity:
    key: str            # 小写归一后的名字，payload 内去重用
    name: str
    type: str
    summary: str
    raw_type: str


@dataclass
class NormalizedEdge:
    source_key: str
    target_key: str
    name: str
    fact: str
    raw_name: str


@dataclass
class NormalizedExtraction:
    entities: list[NormalizedEntity]
    edges: list[NormalizedEdge]
    stats: dict = field(default_factory=dict)


def _norm_key(name: str) -> str:
    return " ".join(name.split()).lower()


def normalize_extraction(entities: list[ExtractedEntity], edges: list[ExtractedEdge],
                         entity_types: set[str], edge_types: set[str],
                         fallback_type: str = "Concept",
                         fallback_edge: str = "relates_to") -> NormalizedExtraction:
    """纯函数：去重、校验、schema 映射，并把纪律指标写进 stats。不读图不写图。"""
    if len(entities) > MAX_ENTITIES:
        raise ValueError(f"entities 超过 {MAX_ENTITIES} 条")
    if len(edges) > MAX_EDGES:
        raise ValueError(f"edges 超过 {MAX_EDGES} 条")
    edge_types_lower = {e.lower() for e in edge_types}
    stats = {"entities_in": len(entities), "edges_in": len(edges),
             "unknown_type": 0, "offschema_edge": 0, "dangling_edge": 0,
             "dup_entity": 0, "dup_edge": 0, "dirty_name": 0, "self_edge": 0}
    out_entities: dict[str, NormalizedEntity] = {}
    for e in entities:
        name = " ".join(e.name.split())
        key = _norm_key(name)
        if not key:
            continue
        if key in out_entities:
            stats["dup_entity"] += 1
            continue
        raw_type = e.type.strip()
        typ = raw_type if raw_type in entity_types else None
        if typ is None:
            # 容忍大小写：tool → Tool
            match = [t for t in entity_types if t.lower() == raw_type.lower()]
            typ = match[0] if match else fallback_type
            if not match:
                stats["unknown_type"] += 1
        if any(p.search(name) for p in DIRTY_NAME_PATTERNS):
            stats["dirty_name"] += 1
        out_entities[key] = NormalizedEntity(key=key, name=name, type=typ,
                                             summary=e.summary.strip(), raw_type=raw_type)
    out_edges: list[NormalizedEdge] = []
    seen_triples: set[tuple[str, str, str]] = set()
    for ed in edges:
        sk, tk = _norm_key(ed.source), _norm_key(ed.target)
        if sk not in out_entities or tk not in out_entities:
            stats["dangling_edge"] += 1
            continue
        if sk == tk:
            stats["self_edge"] += 1
            continue
        raw_name = ed.name.strip()
        lname = raw_name.lower()
        name = lname if lname in edge_types_lower else fallback_edge
        if lname not in edge_types_lower:
            stats["offschema_edge"] += 1
        triple = (sk, name, tk)
        if triple in seen_triples:
            stats["dup_edge"] += 1
            continue
        seen_triples.add(triple)
        out_edges.append(NormalizedEdge(source_key=sk, target_key=tk, name=name,
                                        fact=" ".join(ed.fact.split()), raw_name=raw_name))
    stats["entities_out"] = len(out_entities)
    stats["edges_out"] = len(out_edges)
    return NormalizedExtraction(entities=list(out_entities.values()), edges=out_edges, stats=stats)


# ---------- 写图（与 graphiti_core 0.29.0 FalkorDB save 语句一致） ----------

_FIND_EXACT = (
    "MATCH (n:Entity) WHERE n.group_id = $g AND toLower(n.name) = $lname "
    "RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels LIMIT 1")
_FIND_SIMILAR = (
    "MATCH (n:Entity) WHERE n.group_id = $g AND n.name_embedding IS NOT NULL "
    "WITH n, (2 - vec.cosineDistance(n.name_embedding, vecf32($v)))/2 AS score "
    "WHERE score > $min RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels, score "
    "ORDER BY score DESC LIMIT 1")
_SAVE_NODE = (
    "MERGE (n:Entity {uuid: $entity_data.uuid}) SET n:{labels} SET n = $entity_data "
    "SET n.name_embedding = vecf32($entity_data.name_embedding) RETURN n.uuid AS uuid")
_SAVE_EPISODE = (
    "MERGE (e:Episodic {uuid: $uuid}) SET e = {uuid: $uuid, name: $name, group_id: $group_id, "
    "source_description: $source_description, source: $source, content: $content, "
    "entity_edges: $entity_edges, created_at: $created_at, valid_at: $valid_at, "
    "extractor_model: $extractor_model, extraction_mode: 'agent', "
    "extractor_session: $extractor_session, prompt_version: $prompt_version} "
    "RETURN e.uuid AS uuid")
_SAVE_MENTIONS = (
    "MATCH (episode:Episodic {uuid: $episode_uuid}) MATCH (node:Entity {uuid: $entity_uuid}) "
    "MERGE (episode)-[e:MENTIONS {uuid: $uuid}]->(node) "
    "SET e.group_id = $group_id, e.created_at = $created_at RETURN e.uuid AS uuid")
_FIND_EDGE = (
    "MATCH (a:Entity {uuid: $src})-[e:RELATES_TO {name: $name}]->(b:Entity {uuid: $dst}) "
    "WHERE e.group_id = $g RETURN e.uuid AS uuid, e.episodes AS episodes LIMIT 1")
_APPEND_EPISODE = (
    "MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() "
    "SET e.episodes = CASE WHEN $ep IN coalesce(e.episodes, []) THEN e.episodes "
    "ELSE coalesce(e.episodes, []) + [$ep] END RETURN e.uuid AS uuid")
_SAVE_EDGE = (
    "MATCH (source:Entity {uuid: $edge_data.source_uuid}) "
    "MATCH (target:Entity {uuid: $edge_data.target_uuid}) "
    "MERGE (source)-[e:RELATES_TO {uuid: $edge_data.uuid}]->(target) SET e = $edge_data "
    "SET e.fact_embedding = vecf32($edge_data.fact_embedding) RETURN e.uuid AS uuid")
_LINK_EPISODE_EDGES = (
    "MATCH (e:Episodic {uuid: $uuid}) SET e.entity_edges = $entity_edges")


@dataclass
class WriteResult:
    episode_uuid: str
    nodes_new: int
    nodes_reused: int
    edges_new: int
    edges_reused: int
    resolved: dict  # key -> {"uuid", "reused": bool, "matched_name"}


async def write_extraction(driver, embedder, extraction: NormalizedExtraction, *,
                           name: str, episode_body: str, source_description: str,
                           reference_time: datetime, extractor_model: str,
                           group_id: str, extractor_session: str | None = None,
                           prompt_version: str | None = None,
                           episode_uuid: str | None = None,
                           node_match_min: float = NODE_MATCH_MIN) -> WriteResult:
    """把归一后的抽取写进图。driver 只需 execute_query；embedder 只需 create_batch。"""
    now = datetime.now(tz=timezone.utc)
    now_iso = now.isoformat()
    ep_uuid = episode_uuid or str(uuidlib.uuid4())
    names = [e.name for e in extraction.entities]
    name_vecs = await embedder.create_batch(names) if names else []
    resolved: dict[str, dict] = {}
    nodes_new = nodes_reused = 0
    for ent, vec in zip(extraction.entities, name_vecs):
        rows, _, _ = await driver.execute_query(_FIND_EXACT, g=group_id, lname=ent.key)
        if not rows:
            rows, _, _ = await driver.execute_query(
                _FIND_SIMILAR, g=group_id, v=[float(x) for x in vec], min=node_match_min)
        if rows:
            resolved[ent.key] = {"uuid": rows[0]["uuid"], "reused": True,
                                 "matched_name": rows[0].get("name")}
            nodes_reused += 1
            continue
        new_uuid = str(uuidlib.uuid4())
        entity_data = {
            "uuid": new_uuid, "name": ent.name, "name_embedding": [float(x) for x in vec],
            "group_id": group_id, "summary": ent.summary, "created_at": now_iso,
            "extractor_model": extractor_model,
        }
        await driver.execute_query(_SAVE_NODE.replace("{labels}", f"{ent.type}:Entity"),
                                   entity_data=entity_data)
        resolved[ent.key] = {"uuid": new_uuid, "reused": False, "matched_name": None}
        nodes_new += 1

    await driver.execute_query(
        _SAVE_EPISODE, uuid=ep_uuid, name=name, group_id=group_id,
        source_description=source_description, source="text", content=episode_body,
        entity_edges=[], created_at=now_iso, valid_at=reference_time.isoformat(),
        extractor_model=extractor_model, extractor_session=extractor_session,
        prompt_version=prompt_version)
    for ent in extraction.entities:
        await driver.execute_query(
            _SAVE_MENTIONS, episode_uuid=ep_uuid, entity_uuid=resolved[ent.key]["uuid"],
            uuid=str(uuidlib.uuid4()), group_id=group_id, created_at=now_iso)

    facts = [e.fact for e in extraction.edges]
    fact_vecs = await embedder.create_batch(facts) if facts else []
    edge_uuids: list[str] = []
    edges_new = edges_reused = 0
    for ed, vec in zip(extraction.edges, fact_vecs):
        src = resolved[ed.source_key]["uuid"]
        dst = resolved[ed.target_key]["uuid"]
        rows, _, _ = await driver.execute_query(_FIND_EDGE, src=src, dst=dst, name=ed.name, g=group_id)
        if rows:
            await driver.execute_query(_APPEND_EPISODE, uuid=rows[0]["uuid"], ep=ep_uuid)
            edge_uuids.append(rows[0]["uuid"])
            edges_reused += 1
            continue
        edge_uuid = str(uuidlib.uuid4())
        edge_data = {
            "source_uuid": src, "target_uuid": dst, "uuid": edge_uuid, "name": ed.name,
            "group_id": group_id, "fact": ed.fact, "fact_embedding": [float(x) for x in vec],
            "episodes": [ep_uuid], "created_at": now_iso, "expired_at": None,
            "valid_at": reference_time.isoformat(), "invalid_at": None,
            "reference_time": reference_time.isoformat(),
            "extractor_model": extractor_model, "raw_name": ed.raw_name,
        }
        await driver.execute_query(_SAVE_EDGE, edge_data=edge_data)
        edge_uuids.append(edge_uuid)
        edges_new += 1
    if edge_uuids:
        await driver.execute_query(_LINK_EPISODE_EDGES, uuid=ep_uuid, entity_edges=edge_uuids)
    return WriteResult(episode_uuid=ep_uuid, nodes_new=nodes_new, nodes_reused=nodes_reused,
                       edges_new=edges_new, edges_reused=edges_reused, resolved=resolved)


_CHECK_KEYS = (
    "UNWIND $items AS it MATCH (k:IngestedKey {source_description: it.sd, source_obs_id: it.sid}) "
    "RETURN it.sd AS sd, it.sid AS sid, k.status AS status, k.episode_uuid AS episode_uuid")


async def check_keys(driver, items: list[dict]) -> dict[tuple[str, str], dict]:
    """批量查 IngestedKey 状态，供执行者跳过已入库的观测。"""
    if not items:
        return {}
    rows, _, _ = await driver.execute_query(
        _CHECK_KEYS, items=[{"sd": i["source_description"], "sid": i["source_obs_id"]} for i in items])
    return {(r["sd"], r["sid"]): {"status": r.get("status"), "episode_uuid": r.get("episode_uuid")}
            for r in rows}
