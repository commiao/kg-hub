"""
Shared Graphiti client factory.

Extracted from spike-graphiti/spike.py so ingesters reuse identical wiring:
- LLM: qwen3.6-plus via 百炼 Anthropic adapter (with thinking forced off)
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
import time
from pathlib import Path

# MUST be set before graphiti_core imports (EMBEDDING_DIM is a frozen pydantic field).
os.environ.setdefault("EMBEDDING_DIM", "384")

# Force fully-sequential LLM calls inside Graphiti (per-episode entity/edge
# extraction otherwise fans out up to SEMAPHORE_LIMIT=20 concurrent calls, which
# trips 百炼's "concurrency allocated quota exceeded" 429s). Set before any
# graphiti_core import so helpers.SEMAPHORE_LIMIT picks it up.
os.environ.setdefault("SEMAPHORE_LIMIT", "1")

from dotenv import load_dotenv

load_dotenv(Path.home() / ".claude-mem" / ".env", override=True)

from anthropic import AsyncAnthropic
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client import LLMConfig
from graphiti_core.llm_client.anthropic_client import AnthropicClient

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
# ~/.claude-mem/.env (loaded above) so we never hardcode the password.
#
# FalkorDB multi-tenancy: graphiti routes writes to a graph named after the
# `group_id` parameter on add_episode(). We deliberately align the driver-level
# database to the same name "kg_hub" so reads via execute_query() hit the same
# graph the ingesters wrote into. If we later partition data across multiple
# group_ids, we'll need to either union queries or pick one as the default.
FALKORDB_HOST = os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1")
FALKORDB_PORT = int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379"))
FALKORDB_DATABASE = os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub")


def build_llm() -> AnthropicClient:
    auth_token = os.environ["ANTHROPIC_AUTH_TOKEN"]
    base_url = os.environ["ANTHROPIC_BASE_URL"]
    model = os.environ.get("ANTHROPIC_MODEL", "qwen3.6-plus")
    cfg = LLMConfig(api_key=auth_token, model=model, max_tokens=4096)
    # 百炼 coding plan has concurrent-request quota; bump retries so transient
    # 429s ride out within the SDK rather than failing whole episodes.
    # timeout: without it, a half-open socket (e.g. after the Mac sleeps and the
    # connection is silently dropped) wedges a request forever — the whole ingest
    # hangs with 0 progress. A per-request timeout makes it fail fast and retry.
    async_client = AsyncAnthropic(
        auth_token=auth_token, base_url=base_url, max_retries=5, timeout=120.0
    )

    # 百炼 qwen3.6-plus runs in thinking mode by default, which forbids
    # forced tool_choice. Inject thinking={"type":"disabled"} on every call.
    orig_create = async_client.messages.create

    # Rate limit: 百炼 plan allows ~6000 calls / 5h (≈20/min). Enforce a minimum
    # gap between call STARTS so we stay under quota and leave headroom for other
    # apps. Default 4s ≈ 15/min ≈ 4500/5h (~75% of quota). Tune via
    # KG_HUB_LLM_MIN_INTERVAL_SEC. Combined with SEMAPHORE_LIMIT=1, calls are fully
    # sequential — the lock is held across the sleep so starts are strictly spaced.
    min_interval = float(os.environ.get("KG_HUB_LLM_MIN_INTERVAL_SEC", "4.0"))
    throttle_lock = asyncio.Lock()
    last_call = {"t": 0.0}

    async def create_with_thinking_off(*args, **kwargs):
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body.setdefault("thinking", {"type": "disabled"})
        kwargs["extra_body"] = extra_body
        if min_interval > 0:
            async with throttle_lock:
                wait = min_interval - (time.monotonic() - last_call["t"])
                if wait > 0:
                    await asyncio.sleep(wait)
                last_call["t"] = time.monotonic()
        return await orig_create(*args, **kwargs)

    async_client.messages.create = create_with_thinking_off  # type: ignore[assignment]
    return AnthropicClient(config=cfg, client=async_client)


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
        # 百炼 coding plan throttles on concurrent LLM calls. Serialize.
        max_coroutines=1,
    )
    await g.build_indices_and_constraints()
    return g
