"""
Shared Graphiti client factory.

Extracted from spike-graphiti/spike.py so ingesters reuse identical wiring:
- LLM: logical ``kg_hub.entity_extract`` route via the NAS model gateway
- Embedder: fastembed，模型由 KG_HUB_EMBED_MODEL 决定（utils/embedding_profile；缺省 BAAI/bge-small-en-v1.5，384 维，本地）
- Cross encoder: noop pass-through (we don't use reranking yet)
- Graph: FalkorDB (Redis-protocol, runs in Docker container `kg-hub-falkordb`)

Sets EMBEDDING_DIM from the embedding profile before any graphiti_core import.

Migrated 2026-05-15 from KuzuDriver → FalkorDriver to escape Kuzu's single-writer
lock (Phase 1 → Phase 2 requires concurrent ingest + MCP read).
"""

import asyncio
from contextlib import nullcontext
import logging
import os
import threading
import typing
from pathlib import Path

# MUST be set before graphiti_core imports (EMBEDDING_DIM is a frozen pydantic field).
# 维度取自向量模型档案（utils/embedding_profile，T-0339）：模型由 KG_HUB_EMBED_MODEL 决定，
# 显式给了 EMBEDDING_DIM 却与模型维度不符时当场报错，不让错维度的向量写进图。
from utils.embedding_profile import active_profile as _active_embed_profile, check_dim as _check_embed_dim  # noqa: E402
_EMBED_PROFILE = _active_embed_profile()
_check_embed_dim(_EMBED_PROFILE)
os.environ.setdefault("EMBEDDING_DIM", str(_EMBED_PROFILE.dim))

# Force fully-sequential LLM calls inside Graphiti (per-episode entity/edge
# extraction otherwise fans out up to SEMAPHORE_LIMIT=20 concurrent paid calls).
# Set before any
# graphiti_core import so helpers.SEMAPHORE_LIMIT picks it up.
os.environ.setdefault("SEMAPHORE_LIMIT", "1")

from kg_hub_env import load_kg_hub_env

load_kg_hub_env(override=True)

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client import LLMConfig
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.client import LLMClient, ModelSize
from graphiti_core.prompts.models import Message
from model_gateway_client import (
    capture_model_result, create_gateway_client, gateway_model, gateway_token,
    model_stage, reject_model_result,
)
from utils.graphiti_stage_names import graphiti_model_stage
from pydantic import BaseModel, ValidationError

# --- Perf fix (task #7): make EDGE dedup vector-only ---------------------------
# resolve_extracted_edges() runs EDGE_HYBRID_SEARCH_RRF (bm25 fulltext + cosine
# vector) TWICE per extracted edge — once unfiltered over ALL edges. On FalkorDB
# the bm25/fulltext leg costs seconds and scales with graph size (the 30s-timeout
# / pegged-CPU root cause). Node dedup is already vector-only and fast. Rebind the
# recipe object that edge_operations imported to cosine-only, so BOTH dedup
# searches drop the slow fulltext leg. User-facing search imports the recipe from
# the recipes module directly and is unaffected.
import copy as _copy  # noqa: E402
import graphiti_core.utils.maintenance.edge_operations as _edge_ops  # noqa: E402
import graphiti_core.graphiti as _graphiti_pipeline  # noqa: E402
from utils.batched_node_attributes import extract_attributes_from_nodes as _batch_attributes  # noqa: E402
from utils.batched_edge_timestamps import install as _install_timestamp_batch  # noqa: E402
from utils.batched_edge_dedupe import install as _install_dedupe_batch  # noqa: E402
from utils.indexed_edge_search import install as _install_indexed_edge_search  # noqa: E402
from utils.edge_invalidation_shadow import install as _install_invalidation_shadow  # noqa: E402
from utils.falkordb_episode_window import install as _install_episode_window  # noqa: E402

# 抽取上下文「前 10 条 episode」按 reference_time 过滤（T-0285）。
_install_episode_window()
# graphiti 的矛盾判定默认只记日志、不让边失效（T-0285）。必须先于两个合批补丁安装：
# 它们会替换下层拿到的模型客户端，装在它们外面就看不到去重答案。
_install_invalidation_shadow(os.environ.get("KG_HUB_EDGE_INVALIDATION", "shadow"))
_install_timestamp_batch(int(os.environ.get("KG_HUB_EDGE_TIMESTAMP_BATCH_PERCENT", "0")))
# Installed second so its widened per-edge gather also lets the timestamp batch
# see every edge of an episode; both use the same episode hash buckets.
_install_dedupe_batch(int(os.environ.get("KG_HUB_EDGE_DEDUPE_BATCH_PERCENT", "0")),
                      int(os.environ.get("KG_HUB_EDGE_DEDUPE_BATCH_MAX", "12")))
_install_indexed_edge_search()

# Graphiti imports this helper into its pipeline module. Bind that actual call
# site; rebinding node_operations alone leaves add_episode on the old helper.
_graphiti_pipeline.extract_attributes_from_nodes = _batch_attributes
# Durable stage recovery calls the maintenance entry point directly. Keep its
# original and restored executions on the same batch implementation too.
from graphiti_core.utils.maintenance import node_operations as _node_ops  # noqa: E402
_node_ops.extract_attributes_from_nodes = _batch_attributes

_vec_only = _copy.deepcopy(_edge_ops.EDGE_HYBRID_SEARCH_RRF)
_cosine_methods = [
    m for m in _vec_only.edge_config.search_methods
    if "cosine" in str(getattr(m, "value", m)).lower()
]
if _cosine_methods:
    _vec_only.edge_config.search_methods = _cosine_methods
    _edge_ops.EDGE_HYBRID_SEARCH_RRF = _vec_only

# --- 面向用户的语义边检索：唯一定义 ------------------------------------------------
# MCP kg_search（/api/search_semantic）、/api/search 的语义腿、tools/kg_eval 都走这里。
# Graphiti.search() 默认是 bm25 全文 + cosine：bm25 腿在 FalkorDB 上随边数变慢，
# 约 16 万条 RELATES_TO 时每次都撞 30s 查询超时，整次检索抛异常（T-0273：kg_eval
# 因此长期 edges=0）。从 recipes 模块取原件，与上面 dedup 补丁互不影响。
from graphiti_core.search.search_config_recipes import (  # noqa: E402
    EDGE_HYBRID_SEARCH_RRF as _EDGE_RRF_RECIPE,
)
from graphiti_core.driver.driver import GraphProvider  # noqa: E402
from graphiti_core.edges import get_entity_edge_from_record  # noqa: E402
from graphiti_core.models.edges.edge_db_queries import get_entity_edge_return_query  # noqa: E402
from utils.graphiti_parallel import _vector_read_lock  # noqa: E402

EDGE_VEC_ONLY_SEARCH = _copy.deepcopy(_EDGE_RRF_RECIPE)
EDGE_VEC_ONLY_SEARCH.edge_config.search_methods = [
    m for m in EDGE_VEC_ONLY_SEARCH.edge_config.search_methods
    if "cosine" in str(getattr(m, "value", m)).lower()
] or EDGE_VEC_ONLY_SEARCH.edge_config.search_methods


# 治理退休（T-0281）：背书 episode 全部 archived 的边不再召回，与 /api/search 的 agg()
# 同一规则（查不到背书的边保留）。只认 archived，不按 expired_at/invalid_at 过滤：
# 2026-10-14 实测 22% 的边被 graphiti 判为失效，抽样多数仍成立；硬过滤会把 FalkorDB
# 这类正确答案挤出 top-10。成因与入图侧止血见 utils/edge_invalidation_shadow.py。
# 失效与否由 /api/search_semantic 原样带出 invalid_at/expired_at，交给使用方判断。
RETIRED_HEADROOM = 10   # 多取几条抵扣被退休的，常见情况下仍只扫一次
_RETIRED_ROUNDS = 3     # 不够再翻倍补取，最多扫三次

_search_log = logging.getLogger("kg_hub.search")


async def _drop_retired(driver, edges: list) -> list:
    eps = sorted({str(u) for e in edges for u in (getattr(e, "episodes", None) or [])})
    if not eps:
        return edges
    try:
        rows, _, _ = await driver.execute_query(
            "MATCH (ep:Episodic) WHERE ep.uuid IN $u "
            "RETURN ep.uuid AS u, coalesce(ep.archived, false) AS arch", u=eps)
    except Exception as exc:  # noqa: BLE001 — 与 /api/search 一致：查不到就不过滤
        _search_log.warning("[search] archived lookup failed, not filtering: %s", exc)
        return edges
    archived = {str(r["u"]): bool(r.get("arch")) for r in rows if r.get("u") is not None}

    def retired(e) -> bool:
        backing = [archived[str(u)] for u in (getattr(e, "episodes", None) or [])
                   if str(u) in archived]
        return bool(backing) and all(backing)

    return [e for e in edges if not retired(e)]


# 语义腿走 RELATES_TO.fact_embedding 的 HNSW 索引（T-0345）。graphiti 的 cosine 腿是
# MATCH 全部边逐条算 vec.cosineDistance：17 万条边每次 0.6–1.1 s，NAS 升温约 10°C。
# 索引取候选、再用 graphiti 同一公式与 min_score 精确重排，线上单次十几毫秒。
# 2026-10-22 本机同版本 FalkorDB 灌同一份向量：235 条评测查询在 limit 10/30/40 下
# 去重后的 fact 列表与全扫逐条相同；候选 512 时有 18 条不同，所以下限取 1024。
# FalkorDB 4.18 两条向量查询并发时会返回重复的边、挤掉正确候选（实测 1000 次并发：
# 坏的 715 次全部带重复，无重复的 285 次全部正确；与非向量查询并发不受影响）。
# 进程内与 ingest 共用一把锁串行；跨进程撞上时按「候选有重复」判坏，重试仍坏退回全扫。
# 默认关（KG_HUB_SEARCH_VECTOR_INDEX=1 才走索引）：线上索引在 10-21 换模型重写全部向量后
# 退化，抽样 24 条有 4 条漏掉全扫的前 50 名（含第 2、3 名），加大候选也找不回；
# 重建索引并按 docs/falkor-vector-index-rollout.md 验收后再开。
SEMANTIC_CANDIDATES = 1024
_INDEX_TRIES = 3
_INDEXED_EDGE_QUERY = (
    "CALL db.idx.vector.queryRelationships('RELATES_TO', 'fact_embedding', "
    "$candidates, vecf32($search_vector)) YIELD relationship AS e "
    "WITH count(e) AS hits, collect(DISTINCT e) AS uniq "
    "UNWIND uniq AS e "
    "WITH hits, size(uniq) AS distinct_hits, e, startNode(e) AS n, endNode(e) AS m, "
    "(2 - vec.cosineDistance(e.fact_embedding, vecf32($search_vector)))/2 AS score "
    "WHERE score > $min_score RETURN hits, distinct_hits, "
    + get_entity_edge_return_query(GraphProvider.FALKORDB)
    + " ORDER BY score DESC LIMIT $limit"
)


async def _indexed_edges(driver, vector: list, limit: int) -> list | None:
    """索引取候选、精确重排后的前 limit 条；索引不可用或结果一直损坏时返回 None。"""
    for attempt in range(1, _INDEX_TRIES + 1):
        try:
            async with _vector_read_lock():
                rows, _, _ = await driver.execute_query(
                    _INDEXED_EDGE_QUERY, search_vector=vector, limit=limit,
                    candidates=max(SEMANTIC_CANDIDATES, 20 * limit),
                    min_score=EDGE_VEC_ONLY_SEARCH.edge_config.sim_min_score, routing_="r")
        except Exception as exc:  # noqa: BLE001
            # 与 ingest 侧一致：只有索引本身的问题才退回全扫，其他图错误照常抛出。
            if not any(word in str(exc).lower() for word in ("index", "vector")):
                raise
            _search_log.warning("[search] vector index query failed, exact scan: %s", exc)
            return None
        if not rows or rows[0]["hits"] == rows[0]["distinct_hits"]:
            return [get_entity_edge_from_record(r, GraphProvider.FALKORDB) for r in rows]
        _search_log.warning("[search] vector index returned duplicates (%s of %s distinct), try %d/%d",
                            rows[0]["distinct_hits"], rows[0]["hits"], attempt, _INDEX_TRIES)
    return None


async def search_edges_semantic(g: "Graphiti", query: str, limit: int) -> list:
    """仅 cosine 的语义边检索，返回 EntityEdge 列表（顺序即相关度排名）。

    已退休（背书 episode 全部 archived）的边被剔除，并补取到 limit 条。
    FalkorDB 上走向量索引，索引不可用时退回 graphiti 的全扫。
    """
    indexed = (os.environ.get("KG_HUB_SEARCH_VECTOR_INDEX", "0") == "1"
               and getattr(g.driver, "provider", None) == GraphProvider.FALKORDB)
    vector = None
    fetch = limit + RETIRED_HEADROOM
    for _ in range(_RETIRED_ROUNDS):
        edges = None
        if indexed:
            if vector is None:
                vector = await g.embedder.create(input_data=[query.replace("\n", " ")])
            edges = await _indexed_edges(g.driver, vector, fetch)
            indexed = edges is not None
        if edges is None:
            cfg = _copy.deepcopy(EDGE_VEC_ONLY_SEARCH)
            cfg.limit = fetch
            edges = (await g._search(query=query, config=cfg)).edges
        live = await _drop_retired(g.driver, edges)
        if len(live) >= limit or len(edges) < fetch:
            break
        fetch *= 2
    return live[:limit]


# FalkorDB connection (Docker container `kg-hub-falkordb`). Reads from
# kg-hub's project .env (loaded above) so we never hardcode the password.
#
# FalkorDB multi-tenancy: graphiti routes writes to a graph named after the
# `group_id` parameter on add_episode(). We deliberately align the driver-level
# database to the same name "kg_hub" so reads via execute_query() hit the same
# graph the ingesters wrote into. If we later partition data across multiple
# group_ids, we'll need to either union queries or pick one as the default.
FALKORDB_HOST = os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1")
FALKORDB_PORT = int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379"))
FALKORDB_DATABASE = os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub")


class SingleAttemptAnthropicClient(AnthropicClient):
    """Graphiti 0.29.0 adapter with no semantic retry loop.

    The upstream ``AnthropicClient.generate_response`` retries parsing and
    Pydantic validation failures twice, turning one Graphiti operation into as
    many as three paid model requests.  Transport retries are already disabled
    in :mod:`model_gateway_client`; this override also makes the semantic layer
    exactly one attempt.  Callers may retry only as a new, explicit business
    operation after observing the failure.
    """

    async def generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
        group_id: str | None = None,
        prompt_name: str | None = None,
    ) -> dict[str, typing.Any]:
        del group_id  # retained for graphiti-core's public method contract
        if max_tokens is None:
            max_tokens = self.max_tokens
        stage = graphiti_model_stage(prompt_name)
        stage_scope = model_stage(stage) if stage else nullcontext()
        with stage_scope, capture_model_result() as source:
            response, input_tokens, output_tokens = await self._generate_response(
                messages, response_model, max_tokens, model_size
            )
        self.token_tracker.record(prompt_name, input_tokens, output_tokens)
        if response_model is not None:
            # Validation errors intentionally propagate.  In particular, never
            # append a corrective prompt and make another paid request.
            try:
                return response_model(**response).model_dump()
            except ValidationError:
                # This exact answer can never pass: stop the journal replaying
                # it, so the task's next (separate) retry asks the model again.
                if await reject_model_result(source):
                    logging.getLogger("kg_hub.gateway").warning(
                        "[model_result:rejected] stage=%s model=%s; the next retry re-asks",
                        stage, response_model.__name__)
                raise
        return response


    async def generate_text(
        self,
        messages: list[Message],
        max_tokens: int | None = None,
        prompt_name: str | None = None,
    ) -> tuple[str, dict]:
        """一次调用、**不挂工具**，返回模型文本块拼接与账本捕获 sink。

        2026-10-19 单次抽取首批 11 条实测：经网关的 deepseek 在强制工具调用下 11 次都不写
        ``edges`` 键，唯一一次写了的，边的 JSON 从工具入参里溢出成了文本块（供应商侧的工具调用
        解析不可靠）。让模型直接输出 JSON 文本、由调用方解析，绕开那一层。调用方解析失败时
        用返回的 sink 调 ``reject_model_result``，账本里这份答案才不会在重试时被原样回放。
        """
        if max_tokens is None:
            max_tokens = self.max_tokens
        stage = graphiti_model_stage(prompt_name)
        stage_scope = model_stage(stage) if stage else nullcontext()
        system_message = messages[0]
        user_messages = [{"role": m.role, "content": m.content} for m in messages[1:]]
        with stage_scope, capture_model_result() as source:
            result = await self.client.messages.create(
                system=system_message.content,
                max_tokens=max_tokens,
                temperature=self.temperature,
                messages=user_messages,
                model=self.model,
            )
        usage = getattr(result, "usage", None)
        input_tokens = (getattr(usage, "input_tokens", 0) or 0) if usage else 0
        output_tokens = (getattr(usage, "output_tokens", 0) or 0) if usage else 0
        self.token_tracker.record(prompt_name, input_tokens, output_tokens)
        text = "".join(getattr(b, "text", "") or "" for b in (result.content or [])
                       if getattr(b, "type", "") == "text")
        return text, source


def build_llm() -> AnthropicClient:
    # LLMConfig insists on a non-empty key although the separately constructed
    # SDK client owns transport. Reuse the gateway caller token, never a direct
    # provider or legacy ANTHROPIC_AUTH_TOKEN.
    auth_token = gateway_token()
    model = gateway_model()
    # 2026-10-17 存量画像：零实体/零边 episode（deepseek 时代 1.8%）主要是长观测在 4096 下被截断，而路由允许 8192。
    cfg = LLMConfig(api_key=auth_token, model=model,
                    max_tokens=int(os.environ.get("KG_HUB_LLM_MAX_TOKENS", "8192")))
    # Gateway owns provider choice and cost ceilings. SDK transport retries are
    # disabled centrally; every logical call gets one stable Idempotency-Key.
    min_interval = float(os.environ.get("KG_HUB_LLM_MIN_INTERVAL_SEC", "4.0"))
    # 超时由工厂按「必须晚于网关路由 timeout」的下限统一决定,这里不再各自定值
    async_client = create_gateway_client(min_interval=min_interval)
    return SingleAttemptAnthropicClient(config=cfg, client=async_client)


class FastembedEmbedder(EmbedderClient):
    """Local embeddings (model from utils.embedding_profile), loaded lazily so status endpoints stay responsive."""

    def __init__(self):
        self.model = None
        self._model_lock = threading.Lock()

    def _ensure_model(self):
        if self.model is not None:
            return self.model
        with self._model_lock:
            if self.model is None:
                from fastembed import TextEmbedding
                cache_dir = os.environ.get("FASTEMBED_CACHE_PATH")
                local_only = os.environ.get("FASTEMBED_LOCAL_FILES_ONLY", "").lower() == "true"
                kwargs = {"model_name": _EMBED_PROFILE.model}
                if cache_dir:
                    kwargs["cache_dir"] = cache_dir
                if local_only:
                    kwargs["local_files_only"] = True
                self.model = TextEmbedding(**kwargs)
        return self.model

    def _embed_sync(self, texts):
        model = self._ensure_model()
        return [e.tolist() for e in model.embed(texts)]

    async def create(self, input_data):
        if isinstance(input_data, str):
            texts = [input_data]
        else:
            texts = list(input_data)
        emb = await asyncio.to_thread(self._embed_sync, texts)
        return emb[0]

    async def create_batch(self, input_data_list):
        return await asyncio.to_thread(self._embed_sync, list(input_data_list))


class SearchOnlyLLM(LLMClient):
    """只读用途（如 tools/kg_eval 的检索评测）用的占位模型客户端。

    检索路径（Graphiti.search / 全文）不调模型，但 build_llm() 需要网关凭据。只读工具不该
    为了「构造得出来」就拿到能发起付费调用的凭据（T-0269：周报容器不接网关）。任何一次
    生成都直接报错，保证零调用。
    """

    def __init__(self) -> None:
        super().__init__(config=None, cache=False)

    async def _generate_response(self, *args, **kwargs):
        raise RuntimeError("SearchOnlyLLM: 只读 Graphiti 不允许调用模型")


class NoOpCrossEncoder(CrossEncoderClient):
    """Pass-through reranker; replace with BGERerankerClient when search quality matters."""

    async def rank(self, query, passages):
        return [(p, 1.0) for p in passages]


def _drop_falkordb_graph(database: str) -> None:
    """Delete the named graph in FalkorDB (used by fresh=True)."""
    from falkordb import FalkorDB

    password = os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None
    db = FalkorDB(
        host=FALKORDB_HOST,
        port=FALKORDB_PORT,
        password=password,
    )
    existing = set(db.list_graphs())
    if database in existing:
        db.select_graph(database).delete()


async def build_graphiti(
    fresh: bool = False,
    database: str = FALKORDB_DATABASE,
    llm_client: LLMClient | None = None,
) -> Graphiti:
    """
    Build a Graphiti instance backed by FalkorDB.

    If fresh=True, drops the named graph in FalkorDB first.
    FalkorDriver auto-creates fulltext / range indices on construction —
    unlike Kuzu we don't need to manually run INSTALL fts; or CREATE_FTS_INDEX.
    """
    if fresh:
        _drop_falkordb_graph(database)

    password = os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None
    driver = FalkorDriver(
        host=FALKORDB_HOST,
        port=FALKORDB_PORT,
        password=password,
        database=database,
    )
    # 查询向量与库向量必须同源：配置的模型与图里向量的来源模型不一致就拒绝建客户端（T-0339）。
    from utils.embedding_profile import assert_graph_profile
    await assert_graph_profile(driver)
    g = Graphiti(
        graph_driver=driver,
        llm_client=llm_client if llm_client is not None else build_llm(),
        embedder=FastembedEmbedder(),
        cross_encoder=NoOpCrossEncoder(),
        # Keep business extraction serialized; gateway applies the signed ceiling.
        max_coroutines=1,
    )
    await g.build_indices_and_constraints()
    return g
