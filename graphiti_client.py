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
from graphiti_core.llm_client.client import ModelSize
from graphiti_core.prompts.models import Message
from model_gateway_client import create_gateway_client, gateway_model, gateway_token
from pydantic import BaseModel

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

_vec_only = _copy.deepcopy(_edge_ops.EDGE_HYBRID_SEARCH_RRF)
_cosine_methods = [
    m for m in _vec_only.edge_config.search_methods
    if "cosine" in str(getattr(m, "value", m)).lower()
]
if _cosine_methods:
    _vec_only.edge_config.search_methods = _cosine_methods
    _edge_ops.EDGE_HYBRID_SEARCH_RRF = _vec_only


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
        response, input_tokens, output_tokens = await self._generate_response(
            messages, response_model, max_tokens, model_size
        )
        self.token_tracker.record(prompt_name, input_tokens, output_tokens)
        if response_model is not None:
            # Validation errors intentionally propagate.  In particular, never
            # append a corrective prompt and make another paid request.
            return response_model(**response).model_dump()
        return response


def build_llm() -> AnthropicClient:
    # LLMConfig insists on a non-empty key although the separately constructed
    # SDK client owns transport. Reuse the gateway caller token, never a direct
    # provider or legacy ANTHROPIC_AUTH_TOKEN.
    auth_token = gateway_token()
    model = gateway_model()
    cfg = LLMConfig(api_key=auth_token, model=model, max_tokens=4096)
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
        llm_client=build_llm(),
        embedder=FastembedEmbedder(),
        cross_encoder=NoOpCrossEncoder(),
        # Keep business extraction serialized; gateway applies the signed ceiling.
        max_coroutines=1,
    )
    await g.build_indices_and_constraints()
    return g
