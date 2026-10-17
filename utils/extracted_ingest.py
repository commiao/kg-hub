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

# 2026-10-17 冒烟实测：bge-small-en 对中文短名的余弦普遍偏高，0.92 把「实际额度来源修正发布」
# 并到了「反思改进项落实专项」、把「…本机测试 FileNotFoundError」并到了「docs/effective-quota」。
# 所以向量只负责召回候选，复用还要过词面校验（_lexically_same）。
NODE_MATCH_MIN = 0.95
# 第 2 轮（opus）复盘：同类型近名仍误并 17 对——只差一个字符/数字/日期（test_phase1m→1p、准则 19–22→26–28）、
# 跨仓库同名文件（kg-hub 与 credvault 的 release.sh）、描述性名字被泛名吸进（max_tokens 路由上限裁剪→max_tokens）。
# 所以：代码类名字（标识符/路径）只认完全相等；只在数字上不同的一律不是同一对象；包含规则占比提到 0.8；
# difflib 阈值提到 0.85。
LEXICAL_MIN_RATIO = 0.85
CONTAIN_MIN_SHARE = 0.8
# 标识符/路径一类的名字：ASCII 字母数字加 . / _ - : ( ) 组成，没有空格和非 ASCII
_CODE_LIKE = re.compile(r"^[A-Za-z0-9_./:\-()~@]+$")
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


_LEX_STRIP = re.compile(r"[\s\-_./\\:：（）()\[\]【】,，;；'\"`·]+")


def _lex(name: str) -> str:
    return _LEX_STRIP.sub("", (name or "").lower())


def _lexically_same(a: str, b: str, entity_type: str | None = None) -> bool:
    """向量召回的候选必须在词面上也站得住。

    - 去分隔符后完全相等 → 同一对象（大小写/空格/标点差异）。
    - 只在数字上不同（phase1m/phase1p 不算数字，但 19–22 / 26–28、相邻日期算）→ 不是同一对象。
    - 代码类名字（标识符、路径）或 File/Config 类型 → 只认完全相等。
    - 其余：一方包含另一方且短名占长名 ≥ CONTAIN_MIN_SHARE，或 difflib ≥ LEXICAL_MIN_RATIO。
    """
    import difflib
    if entity_type in ("File", "Config") or (_CODE_LIKE.match(a.strip()) and _CODE_LIKE.match(b.strip())):
        # 代码类：分隔符有意义（deploy/nas/release.sh ≠ deploy/nas-release.sh），只认大小写无关的完全相等，
        # 唯一例外是同一文件的「全路径」与「文件名/子路径」（在 / 边界上是后缀）。
        # 第 3 轮复盘：`:` 不能算边界——规则 16 用 `仓库:路径` 区分跨仓库同名文件，
        # `credvault:tests/x.py` 若在 `:` 上放行就会并回 kg-hub 的 `tests/x.py`。
        la, lb = " ".join(a.split()).lower(), " ".join(b.split()).lower()
        if la == lb:
            return True
        short, long_ = (la, lb) if len(la) <= len(lb) else (lb, la)
        return long_.endswith("/" + short)
    x, y = _lex(a), _lex(b)
    if not x or not y:
        return False
    if x == y:
        return True
    if re.sub(r"\d+", "#", x) == re.sub(r"\d+", "#", y):
        return False
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    if len(short) >= 4 and short in long_ and len(short) / len(long_) >= CONTAIN_MIN_SHARE:
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= LEXICAL_MIN_RATIO


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
    # 第 3 轮复盘：候选顺序要确定，否则 dry-run 与正式提交会并到不同节点；同分按 uuid 定序，
    # 取前 5 个再过词面/类型校验，而不是只看最相似的那一个。
    "WHERE score > $min RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels, score "
    "ORDER BY score DESC, n.uuid ASC LIMIT 5")
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
                           node_match_min: float = NODE_MATCH_MIN,
                           type_guard: bool = True, dry_run: bool = False) -> WriteResult:
    """把归一后的抽取写进图。driver 只需 execute_query；embedder 只需 create_batch。

    dry_run=True：只做嵌入与消解，不写任何东西（第 2 轮复盘：让抽取者在写入前看到会并到哪个旧实体）。"""
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
            # 第 1 轮（haiku）复盘：按名字近似合并不看类型，把 Issue 并进了 Config、Tool 并进了 Issue、
            # 文件并进了同名不同路径的另一个文件。向量候选还要带抽出的类型标签才复用。
            rows = [r for r in rows
                    if _lexically_same(ent.name, r.get("name") or "", ent.type)
                    and (not type_guard or ent.type in (r.get("labels") or []))]
        if rows:
            resolved[ent.key] = {"uuid": rows[0]["uuid"], "reused": True,
                                 "matched_name": rows[0].get("name")}
            nodes_reused += 1
            continue
        new_uuid = str(uuidlib.uuid4())
        if dry_run:
            resolved[ent.key] = {"uuid": None, "reused": False, "matched_name": None}
            nodes_new += 1
            continue
        entity_data = {
            "uuid": new_uuid, "name": ent.name, "name_embedding": [float(x) for x in vec],
            "group_id": group_id, "summary": ent.summary, "created_at": now_iso,
            "extractor_model": extractor_model,
        }
        await driver.execute_query(_SAVE_NODE.replace("{labels}", f"{ent.type}:Entity"),
                                   entity_data=entity_data)
        resolved[ent.key] = {"uuid": new_uuid, "reused": False, "matched_name": None}
        nodes_new += 1

    if dry_run:
        return WriteResult(episode_uuid="", nodes_new=nodes_new, nodes_reused=nodes_reused,
                           edges_new=0, edges_reused=0, resolved=resolved)
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


_CHECK_NAMES = ("MATCH (k:IngestedKey) WHERE k.name IN $names "
                "RETURN k.name AS name, k.status AS status")


async def check_names(driver, names: list[str]) -> dict[str, str]:
    """按 episode 名批量查 IngestedKey 状态。refinery 用它把别的路径（会话自抽取）已入库的
    观测记为完成——否则断路器断开期间它一条都不提交，水位永远不知道这些观测已经进图。"""
    if not names:
        return {}
    rows, _, _ = await driver.execute_query(_CHECK_NAMES, names=list(names))
    return {r["name"]: r.get("status") for r in rows if r.get("name")}


async def check_keys(driver, items: list[dict]) -> dict[tuple[str, str], dict]:
    """批量查 IngestedKey 状态，供执行者跳过已入库的观测。"""
    if not items:
        return {}
    rows, _, _ = await driver.execute_query(
        _CHECK_KEYS, items=[{"sd": i["source_description"], "sid": i["source_obs_id"]} for i in items])
    return {(r["sd"], r["sid"]): {"status": r.get("status"), "episode_uuid": r.get("episode_uuid")}
            for r in rows}
