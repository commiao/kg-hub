"""
Shared Graphiti client factory.

Extracted from spike-graphiti/spike.py so ingesters reuse identical wiring:
- LLM: logical ``kg_hub.entity_extract`` route via the NAS model gateway
- Embedder: fastembed BAAI/bge-small-en-v1.5 (384-dim, local)
- Cross encoder: noop pass-through (we don't use reranking yet)
- Graph: FalkorDB (Redis-protocol, runs in Docker container `kg-hub-falkordb`)

Sets EMBEDDING_DIM=384 before any graphiti_core import.

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
os.environ.setdefault("EMBEDDING_DIM", "384")

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


async def search_edges_semantic(g: "Graphiti", query: str, limit: int) -> list:
    """仅 cosine 的语义边检索，返回 EntityEdge 列表（顺序即相关度排名）。

    已退休（背书 episode 全部 archived）的边被剔除，并补取到 limit 条。
    """
    fetch = limit + RETIRED_HEADROOM
    for _ in range(_RETIRED_ROUNDS):
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
    """Local 384-dim embeddings, loaded lazily so status endpoints stay responsive."""

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
                kwargs = {"model_name": "BAAI/bge-small-en-v1.5"}
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
