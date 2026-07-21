"""
kg_hub_server.py — HTTP API for kg-hub (Phase 3.A).

Routes:
  POST /api/ingest   add episode (idempotent, schema = DESIGN decision 14)
  GET  /api/search   semantic search over edge facts
  GET  /health       liveness probe (no auth)

Auth:
  Authorization: Bearer <KG_HUB_API_TOKEN> on every request except /health.
  Token persisted in ~/.claude-mem/.env (DESIGN decision 15).

Concurrency:
  Writes acquire utils.writer_lock (DESIGN decision 12) — serializes against
  ingester scripts that may also write. Reads bypass the lock.

Idempotency:
  Each ingest looks up (source_description, source_obs_id) in an
  IngestedKey node. Hit → 200 OK skip; miss → write + record.

Launch:
  /Users/mac/workspace_claudeCode/kg-hub/spike-graphiti/.venv/bin/python kg_hub_server.py
  # → listens on 0.0.0.0:8080
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(Path.home() / ".claude-mem" / ".env", override=True)

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route

from graphiti_core.driver.falkordb_driver import FalkorDriver  # type: ignore
from graphiti_core.nodes import EpisodeType  # type: ignore

from graphiti_client import (  # noqa: E402
    FALKORDB_DATABASE,
    FALKORDB_HOST,
    FALKORDB_PORT,
    build_graphiti,
)
from schema import ENTITY_TYPES, EDGE_TYPES, EDGE_TYPE_MAP  # noqa: E402
from utils.writer_lock import async_writer_lock, WriterLockBusy  # noqa: E402
from utils.wait_for_dependencies import wait_for_falkordb  # noqa: E402


# ---------- Config from env ----------
API_TOKEN = os.environ.get("KG_HUB_API_TOKEN")
if not API_TOKEN:
    raise RuntimeError(
        "KG_HUB_API_TOKEN missing from ~/.claude-mem/.env — generate with "
        "`python -c 'import secrets; print(secrets.token_urlsafe(32))'`"
    )
GROUP_ID = "kg_hub"
HOST = os.environ.get("KG_HUB_BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("KG_HUB_BIND_PORT", "8080"))

# Append every incoming episode (raw body + metadata) to a local jsonl BEFORE
# extraction, so content added via kg_add_episode / curl survives even if the
# FalkorDB graph is later lost. This is the durable source-of-truth backup for
# the otherwise-sourceless /api/ingest writes. Empty = disabled.
INGEST_BACKUP_PATH = os.environ.get("KG_HUB_INGEST_BACKUP_PATH", "").strip()

# Stuck job cleanup: any IngestedKey with status='pending' older than this many
# minutes is considered orphaned (worker died / server restarted mid-extract) and
# DELETED so the same source_obs_id can be retried fresh. 30 min = 2.7x worst-case
# observed (rich-content add_episode ~196s + lock wait ~180s + 429 retries ~150s
# ≈ 9 min worst). Configurable for tuning.
STUCK_THRESHOLD_MIN = int(os.environ.get("KG_HUB_STUCK_THRESHOLD_MIN", "30"))

# logger for ingest lifecycle events ([ingest:start] / [ingest:done] / [ingest:error])
logger = logging.getLogger("kg_hub.server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


# ---------- Graphiti lazy singleton ----------
_graphiti = None
_init_lock = asyncio.Lock()
_status_driver = None


async def get_graphiti():
    global _graphiti
    async with _init_lock:
        if _graphiti is None:
            _graphiti = await build_graphiti(fresh=False)
    return _graphiti


def get_status_driver():
    """Lightweight FalkorDB driver for status/queue reads that do not need embeddings."""
    global _status_driver
    if _status_driver is None:
        _status_driver = FalkorDriver(
            host=FALKORDB_HOST,
            port=FALKORDB_PORT,
            password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
            database=FALKORDB_DATABASE,
        )
    return _status_driver


# ---------- Auth middleware ----------
class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Public, read-only, tailnet-only surfaces: health + the report portal /
        # dashboards. They render server-side (data baked in), so no client token
        # is needed; 17171 is bound to NAS loopback + tailscale, never public LAN.
        if path == "/health" or path == "/" or path.startswith("/portal") or path.startswith("/dashboard"):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return JSONResponse(
                {"status": "error", "code": "unauthorized", "message": "missing Bearer token"},
                status_code=401,
            )
        token = header[len("Bearer "):].strip()
        if token != API_TOKEN:
            return JSONResponse(
                {"status": "error", "code": "unauthorized", "message": "invalid token"},
                status_code=401,
            )
        return await call_next(request)


# ---------- Idempotency + status helpers ----------
#
# IngestedKey schema:
#   source_description (str)     part of unique key
#   source_obs_id      (str)     part of unique key
#   episode_uuid       (str)     UUID assigned by THIS server (pre-extract)
#   status             (str)     'pending' | 'ok' | 'error'
#   created_at         (ISO)     when first MERGEd
#   updated_at         (ISO)     last status change
#   created_by_request (str)     request_id of the call that created this row
#                                  used to detect "did THIS call newly create it"
#   nodes / edges      (int?)    populated on success
#   error_message      (str?)    populated on error


async def cleanup_stuck_jobs(graphiti) -> int:
    """Delete IngestedKey rows stuck in 'pending' older than STUCK_THRESHOLD_MIN.
    Returns number deleted. Called at the top of every /api/ingest to avoid
    needing a separate cron — piggybacks on existing traffic."""
    threshold = (
        datetime.now(tz=timezone.utc) - timedelta(minutes=STUCK_THRESHOLD_MIN)
    ).isoformat()
    rows, _, _ = await graphiti.driver.execute_query(
        "MATCH (k:IngestedKey) "
        "WHERE k.status = 'pending' AND k.created_at < $threshold "
        "WITH k, k.source_description AS sd, k.source_obs_id AS sid, "
        "     k.episode_uuid AS uuid "
        "DELETE k "
        "RETURN count(*) AS cleaned, "
        "       collect({sd:sd, sid:sid, uuid:uuid}) AS removed",
        threshold=threshold,
    )
    if rows:
        cleaned = rows[0].get("cleaned", 0)
        if cleaned:
            removed = rows[0].get("removed", [])
            logger.warning(
                "[ingest:cleanup] removed %d stuck pending keys (older than %d min): %s",
                cleaned,
                STUCK_THRESHOLD_MIN,
                removed,
            )
        return int(cleaned)
    return 0


async def merge_or_get_ingested_key(
    graphiti,
    source_description: str,
    source_obs_id: str,
    request_id: str,
) -> dict:
    """
    Atomic check-and-insert: if (sd, sid) doesn't exist, create with 'pending'.
    If it exists, return existing state.

    Returns: {status, episode_uuid, newly_created, error_message}
      newly_created=True   we own this row; should launch extraction
      newly_created=False  another request already created it; check status

    Note: episode_uuid is NOT pre-assigned — graphiti generates it during
    add_episode. The IngestedKey row exists first as a "claim" tied to
    (sd, sid); episode_uuid is filled in after successful extraction.
    """
    now = datetime.now(tz=timezone.utc).isoformat()
    rows, _, _ = await graphiti.driver.execute_query(
        "MERGE (k:IngestedKey {source_description: $sd, source_obs_id: $sid}) "
        "ON CREATE SET "
        "  k.status = 'pending', "
        "  k.created_at = $now, "
        "  k.updated_at = $now, "
        "  k.created_by_request = $request_id "
        "RETURN k.status AS status, k.episode_uuid AS episode_uuid, "
        "       k.error_message AS error_message, "
        "       k.created_by_request = $request_id AS newly_created",
        sd=source_description,
        sid=source_obs_id,
        now=now,
        request_id=request_id,
    )
    if not rows:
        raise RuntimeError("MERGE returned no rows — should never happen")
    return {
        "status": rows[0].get("status"),
        "episode_uuid": rows[0].get("episode_uuid"),  # may be None for newly_created
        "error_message": rows[0].get("error_message"),
        "newly_created": bool(rows[0].get("newly_created")),
    }


async def update_ingested_key_status(
    graphiti,
    source_description: str,
    source_obs_id: str,
    status: str,
    episode_uuid: str | None = None,
    nodes: int = 0,
    edges: int = 0,
    error_message: str | None = None,
) -> None:
    """Update an existing IngestedKey row after extraction succeeds or fails."""
    now = datetime.now(tz=timezone.utc).isoformat()
    await graphiti.driver.execute_query(
        "MATCH (k:IngestedKey {source_description: $sd, source_obs_id: $sid}) "
        "SET k.status = $status, k.updated_at = $now, "
        "    k.episode_uuid = $episode_uuid, "
        "    k.nodes = $nodes, k.edges = $edges, "
        "    k.error_message = $error_message",
        sd=source_description,
        sid=source_obs_id,
        status=status,
        now=now,
        episode_uuid=episode_uuid,
        nodes=int(nodes),
        edges=int(edges),
        error_message=error_message,
    )


async def lookup_status_by_uuid(graphiti, episode_uuid: str) -> dict | None:
    """Look up an IngestedKey by episode_uuid (for the status endpoint)."""
    driver = graphiti if hasattr(graphiti, "execute_query") else graphiti.driver
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey {episode_uuid: $uuid}) "
        "RETURN k.status AS status, k.episode_uuid AS episode_uuid, "
        "       k.source_description AS source_description, "
        "       k.source_obs_id AS source_obs_id, "
        "       k.created_at AS created_at, k.updated_at AS updated_at, "
        "       k.nodes AS nodes, k.edges AS edges, "
        "       k.error_message AS error_message "
        "LIMIT 1",
        uuid=episode_uuid,
    )
    return rows[0] if rows else None


# ---------- Request body ----------
class IngestBody(BaseModel):
    name: str
    episode_body: str
    source_description: str
    reference_time: str
    source_obs_id: str
    sync: bool = False  # default async; old callers can request sync=true to keep blocking behavior


# ---------- Route handlers ----------
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "kg_hub_server"})


def _backup_episode(body: "IngestBody", ref_time: datetime) -> None:
    """Append the raw episode to KG_HUB_INGEST_BACKUP_PATH (jsonl) before extraction.

    Best-effort and never raises — a backup failure must not break ingestion.
    Captures the otherwise-unrecoverable content of sourceless /api/ingest writes.
    """
    if not INGEST_BACKUP_PATH:
        return
    try:
        rec = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "source_description": body.source_description,
            "source_obs_id": body.source_obs_id,
            "name": body.name,
            "reference_time": ref_time.isoformat(),
            "episode_body": body.episode_body,
        }
        p = Path(INGEST_BACKUP_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("[ingest:backup] backup write failed (non-fatal)", exc_info=True)


# Writer-lock contention handling (see 2026-06-13 incident): instead of dropping
# an ingest to 'error' the first time it can't grab the writer.lock, retry with
# linear backoff. Concurrent batch ingests then serialize cleanly behind whichever
# writer currently holds the lock, instead of all timing out and silently erroring.
INGEST_LOCK_TIMEOUT_SEC = float(os.environ.get("KG_HUB_INGEST_LOCK_TIMEOUT_SEC", "180.0"))
INGEST_LOCK_RETRIES = int(os.environ.get("KG_HUB_INGEST_LOCK_RETRIES", "5"))
INGEST_LOCK_BACKOFF_SEC = float(os.environ.get("KG_HUB_INGEST_LOCK_BACKOFF_SEC", "5.0"))


async def do_extract(
    graphiti,
    body: IngestBody,
    ref_time: datetime,
) -> None:
    """
    The actual heavy work: acquire writer.lock, call graphiti.add_episode,
    record final status (including graphiti-assigned episode_uuid) onto
    IngestedKey. Called either inline (sync path) or via asyncio.create_task
    (async path). Never raises — all errors are captured and written to
    IngestedKey.status='error' for caller polling.

    Tracking key is (source_description, source_obs_id); episode_uuid is
    populated post-extraction from graphiti's assigned UUID.
    """
    sd = body.source_description
    sid = body.source_obs_id
    started = datetime.now(tz=timezone.utc)
    # Durable backup BEFORE extraction — survives even if extraction or the graph fails.
    _backup_episode(body, ref_time)
    logger.info(
        "[ingest:start] source=%s sobsid=%s body_len=%d",
        sd, sid, len(body.episode_body),
    )
    try:
        result = None
        attempt = 0
        while True:
            try:
                async with async_writer_lock(
                    owner=f"api_ingest({sd})", timeout_seconds=INGEST_LOCK_TIMEOUT_SEC
                ):
                    lock_acquired = datetime.now(tz=timezone.utc)
                    logger.info(
                        "[ingest:lock_acquired] sd=%s sid=%s waited=%.1fs attempt=%d",
                        sd, sid, (lock_acquired - started).total_seconds(), attempt + 1,
                    )
                    result = await graphiti.add_episode(
                        name=body.name,
                        episode_body=body.episode_body,
                        source=EpisodeType.text,
                        source_description=sd,
                        reference_time=ref_time,
                        group_id=GROUP_ID,
                        entity_types=ENTITY_TYPES,
                        edge_types=EDGE_TYPES,
                        edge_type_map=EDGE_TYPE_MAP,
                    )
                break  # lock acquired + extraction completed
            except WriterLockBusy:
                attempt += 1
                if attempt > INGEST_LOCK_RETRIES:
                    await update_ingested_key_status(
                        graphiti, sd, sid, "error",
                        error_message=(
                            f"writer.lock busy after {INGEST_LOCK_RETRIES} retries "
                            f"(~{int(INGEST_LOCK_RETRIES * INGEST_LOCK_TIMEOUT_SEC)}s) — "
                            "contention too high"
                        ),
                    )
                    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
                    logger.error(
                        "[ingest:error] sd=%s sid=%s elapsed=%.1fs "
                        "reason=lock_timeout_exhausted attempts=%d",
                        sd, sid, elapsed, attempt,
                    )
                    return
                backoff = INGEST_LOCK_BACKOFF_SEC * attempt  # linear backoff
                logger.warning(
                    "[ingest:lock_retry] sd=%s sid=%s attempt=%d/%d backoff=%.1fs "
                    "(another writer holds the lock; will retry, not dropping)",
                    sd, sid, attempt, INGEST_LOCK_RETRIES, backoff,
                )
                await asyncio.sleep(backoff)
        nodes = len(result.nodes)
        edges = len(result.edges)
        episode_uuid = None
        try:
            episode_uuid = str(result.episode.uuid)  # type: ignore[attr-defined]
        except Exception:
            pass
        await update_ingested_key_status(
            graphiti, sd, sid, "ok",
            episode_uuid=episode_uuid, nodes=nodes, edges=edges,
        )
        elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
        logger.info(
            "[ingest:done] sd=%s sid=%s uuid=%s elapsed=%.1fs nodes=%d edges=%d",
            sd, sid, episode_uuid, elapsed, nodes, edges,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            await update_ingested_key_status(
                graphiti, sd, sid, "error",
                error_message=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
        logger.exception("[ingest:error] sd=%s sid=%s elapsed=%.1fs", sd, sid, elapsed)


async def ingest(request: Request) -> JSONResponse:
    """
    POST /api/ingest — accepts an episode for the central KG.

    Default behavior is ASYNC: returns 202 immediately with the pre-assigned
    episode_uuid; background task does graphiti extraction (~10-200s).

    For backwards compat, pass {"sync": true} in the body to block until
    extraction completes (returns 200 with nodes/edges count).

    Idempotency: (source_description, source_obs_id) is a unique key.
    Repeated calls with the same pair return the existing episode_uuid
    (with status='skipped' if already done, 'in_progress' if still extracting).

    See DESIGN decision 14 (schema) + decision 16 (write-path policy).
    """
    try:
        raw = await request.json()
    except Exception as exc:
        return JSONResponse(
            {"status": "error", "code": "bad_json", "message": str(exc)},
            status_code=400,
        )

    try:
        body = IngestBody.model_validate(raw)
    except ValidationError as exc:
        return JSONResponse(
            {"status": "error", "code": "bad_schema", "message": exc.errors()},
            status_code=400,
        )

    # Validate reference_time eagerly
    try:
        ref_time = datetime.fromisoformat(body.reference_time.replace("Z", "+00:00"))
    except Exception:
        return JSONResponse(
            {"status": "error", "code": "bad_reference_time",
             "message": "reference_time must be ISO 8601 (e.g. '2026-05-18T12:34:56Z')"},
            status_code=400,
        )

    g = await get_graphiti()

    # 1. Piggyback cleanup of stuck-pending IngestedKey rows. Cheap when none stuck.
    try:
        await cleanup_stuck_jobs(g)
    except Exception:
        logger.exception("[ingest:cleanup_failed] continuing anyway")

    # 2. Atomic check-and-create IngestedKey (episode_uuid filled later by graphiti)
    request_id = str(uuidlib.uuid4())
    try:
        merge_result = await merge_or_get_ingested_key(
            g, body.source_description, body.source_obs_id, request_id,
        )
    except Exception as exc:
        return JSONResponse(
            {"status": "error", "code": "ingest_failed",
             "message": f"idempotency merge failed: {exc}"},
            status_code=500,
        )

    sd = body.source_description
    sid = body.source_obs_id
    # urlencode the query params for the poll_url; keep light by hand
    from urllib.parse import urlencode
    poll_url = f"/api/ingest/status?{urlencode({'source_description': sd, 'source_obs_id': sid})}"

    # 3. Route based on what MERGE returned
    if not merge_result["newly_created"]:
        existing_uuid = merge_result["episode_uuid"]
        existing_status = merge_result["status"]
        if existing_status == "ok":
            return JSONResponse(
                {"status": "skipped", "reason": "duplicate",
                 "episode_uuid": existing_uuid,
                 "source_description": sd, "source_obs_id": sid}
            )
        if existing_status == "pending":
            return JSONResponse(
                {"status": "in_progress",
                 "reason": "another request is currently extracting this episode",
                 "source_description": sd, "source_obs_id": sid,
                 "poll_url": poll_url},
                status_code=202,
            )
        # existing_status == 'error'
        return JSONResponse(
            {"status": "error", "code": "previous_attempt_failed",
             "message": merge_result.get("error_message")
                        or "previous attempt failed; delete the IngestedKey to retry",
             "source_description": sd, "source_obs_id": sid},
            status_code=409,
        )

    # 4. We own this row — do the extraction.
    if body.sync:
        # Old-callers path: block until done.
        await do_extract(g, body, ref_time)
        rows, _, _ = await g.driver.execute_query(
            "MATCH (k:IngestedKey {source_description: $sd, source_obs_id: $sid}) "
            "RETURN k.status AS status, k.episode_uuid AS episode_uuid, "
            "       k.nodes AS nodes, k.edges AS edges, "
            "       k.error_message AS error_message",
            sd=sd, sid=sid,
        )
        if not rows:
            return JSONResponse(
                {"status": "error", "code": "internal", "message": "IngestedKey vanished post-extract"},
                status_code=500,
            )
        r = rows[0]
        if r.get("status") == "ok":
            return JSONResponse(
                {"status": "ok", "episode_uuid": r.get("episode_uuid"),
                 "nodes": r.get("nodes"), "edges": r.get("edges"),
                 "source_description": sd, "source_obs_id": sid}
            )
        return JSONResponse(
            {"status": "error", "code": "ingest_failed",
             "message": r.get("error_message"),
             "source_description": sd, "source_obs_id": sid},
            status_code=500,
        )

    # Default async path: return 202 immediately, background does the work.
    asyncio.create_task(do_extract(g, body, ref_time))
    return JSONResponse(
        {"status": "accepted",
         "source_description": sd, "source_obs_id": sid,
         "poll_url": poll_url,
         "hint": "extraction running in background; check status via poll_url or just kg_search later. episode_uuid populated when extraction completes."},
        status_code=202,
    )


async def ingest_status(request: Request) -> JSONResponse:
    """
    GET /api/ingest/status — poll an ingest job's outcome.

    Two forms:
      ?source_description=X&source_obs_id=Y  (primary — works pre-completion)
      ?episode_uuid=Z                         (after completion, alternative)
    """
    sd = request.query_params.get("source_description", "").strip()
    sid = request.query_params.get("source_obs_id", "").strip()
    episode_uuid = request.query_params.get("episode_uuid", "").strip()
    driver = get_status_driver()
    if sd and sid:
        rows, _, _ = await driver.execute_query(
            "MATCH (k:IngestedKey {source_description: $sd, source_obs_id: $sid}) "
            "RETURN k.status AS status, k.episode_uuid AS episode_uuid, "
            "       k.source_description AS source_description, "
            "       k.source_obs_id AS source_obs_id, "
            "       k.created_at AS created_at, k.updated_at AS updated_at, "
            "       k.nodes AS nodes, k.edges AS edges, "
            "       k.error_message AS error_message "
            "LIMIT 1",
            sd=sd, sid=sid,
        )
        row = rows[0] if rows else None
    elif episode_uuid:
        row = await lookup_status_by_uuid(driver, episode_uuid)
    else:
        return JSONResponse(
            {"status": "error", "code": "bad_request",
             "message": "provide either source_description+source_obs_id or episode_uuid"},
            status_code=400,
        )

    if not row:
        return JSONResponse(
            {"status": "error", "code": "not_found",
             "message": "no IngestedKey matching the supplied keys"},
            status_code=404,
        )
    return JSONResponse({
        "status": row.get("status"),
        "episode_uuid": row.get("episode_uuid"),
        "source_description": row.get("source_description"),
        "source_obs_id": row.get("source_obs_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "nodes": row.get("nodes"),
        "edges": row.get("edges"),
        "error_message": row.get("error_message"),
    })


async def queue_stats(request: Request) -> JSONResponse:
    """
    GET /api/queue_stats — aggregate snapshot for monitoring.

    Counts pending/ok/error IngestedKey rows + identifies the oldest stuck
    pending. The watchdog polls this to decide whether to alert.
    """
    driver = get_status_driver()
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey) "
        "RETURN k.status AS status, k.created_at AS created_at, "
        "       k.updated_at AS updated_at, k.error_message AS error_message, "
        "       k.source_obs_id AS sid, k.source_description AS sd"
    )
    pending = ok = errored = 0
    oldest_pending: str | None = None
    last_hour = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    ok_last_1h = errored_last_1h = 0
    recent_error_samples: list[dict] = []  # surface WHY ingests failed (e.g. timeouts)
    for r in rows:
        s = r.get("status")
        created = r.get("created_at")
        updated = r.get("updated_at") or created
        if s == "pending":
            pending += 1
            if not oldest_pending or (created and created < oldest_pending):
                oldest_pending = created
        elif s == "ok":
            ok += 1
            try:
                if updated and datetime.fromisoformat(updated.replace("Z", "+00:00")) > last_hour:
                    ok_last_1h += 1
            except Exception:
                pass
        elif s == "error":
            errored += 1
            try:
                if updated and datetime.fromisoformat(updated.replace("Z", "+00:00")) > last_hour:
                    errored_last_1h += 1
                    em = (r.get("error_message") or "").strip()
                    recent_error_samples.append({
                        "sid": r.get("sid"),
                        "sd": r.get("sd"),
                        "error": em[:200],
                    })
            except Exception:
                pass

    oldest_pending_age_seconds: float | None = None
    if oldest_pending:
        try:
            oldest_dt = datetime.fromisoformat(oldest_pending.replace("Z", "+00:00"))
            oldest_pending_age_seconds = (datetime.now(tz=timezone.utc) - oldest_dt).total_seconds()
        except Exception:
            pass

    return JSONResponse({
        "status": "ok",
        "pending": pending,
        "ok_total": ok,
        "errored_total": errored,
        "ok_last_1h": ok_last_1h,
        "errored_last_1h": errored_last_1h,
        "oldest_pending_at": oldest_pending,
        "oldest_pending_age_seconds": oldest_pending_age_seconds,
        "stuck_threshold_minutes": STUCK_THRESHOLD_MIN,
        "recent_error_samples": recent_error_samples[:5],
    })


async def search(request: Request) -> JSONResponse:
    query = (request.query_params.get("q") or "").strip()
    if not query:
        return JSONResponse(
            {"status": "error", "code": "bad_request", "message": "missing query param 'q'"},
            status_code=400,
        )
    try:
        num_results = min(int(request.query_params.get("num_results", "10")), 30)
    except (TypeError, ValueError):
        num_results = 10

    # Use a direct FalkorDB text search for the HTTP API. Graphiti's hybrid
    # semantic search is still available to deeper callers, but it can take
    # tens of seconds on this local graph and should not make /api/search hang.
    driver = get_status_driver()
    rows, _, _ = await driver.execute_query(
        "MATCH (s)-[e]->(t) "
        "WHERE e.fact IS NOT NULL AND toLower(e.fact) CONTAINS $query "
        "RETURN e.fact AS fact, s.uuid AS source_node_uuid, "
        "       t.uuid AS target_node_uuid, e.valid_at AS valid_at, "
        "       e.created_at AS created_at "
        "LIMIT $limit",
        query=query.lower(),
        limit=num_results,
    )
    results = []
    for row in rows:
        results.append({
            "fact": row.get("fact"),
            "source_node_uuid": str(row.get("source_node_uuid")),
            "target_node_uuid": str(row.get("target_node_uuid")),
            "valid_at": row.get("valid_at"),
            "created_at": row.get("created_at"),
        })
    return JSONResponse({"status": "ok", "query": query, "mode": "falkordb_text", "results": results})


# ---------------------------------------------------------------------------
# Canonical capsule ranking config (DESIGN: PUSH-hook relevance, not coincidence)
#
# Scope is a SOFT routing prior, not a hard partition — the cross-tool/-device/
# -session commons stays a single pool. A capsule is eligible in a session if it
# is `global` (relevant everywhere), mentions the cwd keyword, or is explicitly
# scoped to that project. `global` capsules compete in EVERY session; that is
# what preserves公共性 (cross-* sharing). usage_count is used ONLY inverted, as
# a bounded exploration term, so popular capsules can't crowd out the long tail
# (anti rich-get-richer).
#
# CANONICAL_SCOPE is a built-in fallback so scoping works with ZERO DB migration
# (FalkorDB is NAS-localhost-only; no remote write path). A node's own `n.scope`
# property, once set by tools/ingest_canonical_docs.py, takes precedence.
DEFAULT_SCOPE = "global"
SCOPE_MATCH_BONUS = 0.5      # capsule scoped to the current project
SCOPE_OTHER_PENALTY = -0.3   # capsule scoped to a *different* project (soft, not excluded)
EXPLORE_C = 1.0              # UCB exploration coefficient (bounded to 1 reserved slot)
CANONICAL_SCOPE = {
    # kg-hub internal docs — only inject when actually working on kg-hub
    "kg-hub-canonical-DESIGN": "project:kg-hub",
    "kg-hub-canonical-ROADMAP": "project:kg-hub",
    "kg-hub-canonical-PHASE-3-REPORT": "project:kg-hub",
    "kg-hub-canonical-OBSERVATION-PHASE": "project:kg-hub",
    "kg-hub-canonical-INCIDENT-RETRO": "project:kg-hub",
    "kg-hub-canonical-NOTIFICATION": "project:kg-hub",
    # cross-tool / cross-device commons — relevant in any session, any tool
    "kg-hub-canonical-ONBOARDING": "global",
    "kg-hub-canonical-INTEGRATION-GUIDE": "global",
    "kg-hub-canonical-AGENT-TOOL-DISCOVERY": "global",
}


# ---------- G5 交付分层 + G6-lite 使用度量（0.4 层③；delivery_replay 已离线验证）----------
# 对「按 score 排 Episodic」的交付面（canonical_context pass-2 填位 / episode_search）做
# type 软加权 + 探索地板：知识型上浮、操作型降权但保留 1 席。config `delivery_tiering.enabled`
# 门控，默认 false=inert（行为与今日一致）。config baked，flip 需 rebuild。
_DT_TYPE_RE = re.compile(r"type=(\S+)")
_DT_KNOWLEDGE = {"decision", "security_note", "security_alert"}
_DT_OPERATIONAL = {"bugfix", "change", "feature", "refactor"}


def _load_delivery_tiering() -> dict:
    try:
        p = Path(__file__).resolve().parent / "config" / "ingest_filter.json"
        return json.loads(p.read_text()).get("delivery_tiering", {}) or {}
    except Exception:
        return {}


_DELIVERY = _load_delivery_tiering()  # cached at import（config baked，重启即重读）


def _ep_type(source_description: str) -> str:
    m = _DT_TYPE_RE.search(source_description or "")
    return m.group(1) if m else "?"


def _tier_weight(typ: str) -> float:
    return float((_DELIVERY.get("weights") or {}).get(typ, 1.0))


def _tiered_rerank(items: list, n: int) -> list:
    """items: dicts 含 'score' + 'source'(=source_description)。enabled=false 时纯按 score 取
    top-n（与今日一致）。enabled=true：score×type权重 重排 + 1 席操作型探索地板（只牺牲最低分
    非知识型，绝不牺牲知识型；全知识型不强插）。逻辑镜像 tools/delivery_replay.py。"""
    for it in items:
        it["_type"] = _ep_type(it.get("source") or "")
    if not _DELIVERY.get("enabled"):
        return sorted(items, key=lambda r: -(r.get("score") or 0))[:n]
    for it in items:
        it["_tscore"] = float(it.get("score") or 0) * _tier_weight(it["_type"])
    ranked = sorted(items, key=lambda x: -x["_tscore"])
    picked = ranked[:n]
    if n <= 0 or any(c["_type"] in _DT_OPERATIONAL for c in picked):
        return picked
    rest_ops = [c for c in ranked[n:] if c["_type"] in _DT_OPERATIONAL]
    non_know = [c for c in picked if c["_type"] not in _DT_KNOWLEDGE]
    if rest_ops and non_know:
        drop = min(non_know, key=lambda c: c["_tscore"])
        picked = [c for c in picked if c is not drop] + [rest_ops[0]]
    return picked


def _log_delivery(endpoint: str, kw: str, picked: list) -> None:
    """G6-lite：把每次注入/搜索的命中(name+type+是否tiered)追加到 /backup 卷。绝不因日志失败影响交付。"""
    try:
        rec = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "endpoint": endpoint, "kw": kw, "tiering": bool(_DELIVERY.get("enabled")),
            "picked": [{"name": p.get("name"),
                        "type": p.get("_type") or _ep_type(p.get("source") or "")}
                       for p in picked],
        }
        path = os.environ.get("KG_HUB_DELIVERY_LOG", "/backup/delivery-hits.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def canonical_context(request: Request) -> JSONResponse:
    """GET /api/canonical_context?kw=<keyword>&top_n=3&bump=1

    Server-side replacement for kg_push_hook.py's old direct-FalkorDB
    read+bump. Runs the two-pass canonical/fulltext retrieval, ranks
    (canonical first), and — when bump=1 — increments usage_count +
    last_used_at on the picked episodes, all on localhost FalkorDB.

    Why: after the NAS migration the SessionStart hook's cross-network
    FalkorDB write (1s fail-fast) was silently dropped, so usage_count
    stopped accumulating and the direct read drifted to ~3.6s (close to the
    5s hook timeout). Moving read+bump here makes the hook a single tolerant
    HTTP round-trip; the bump is now reliable because server↔falkordb is local.
    """
    kw = (request.query_params.get("kw") or "").strip()
    if not kw:
        return JSONResponse(
            {"status": "error", "code": "bad_request", "message": "missing query param 'kw'"},
            status_code=400,
        )
    try:
        top_n = min(max(int(request.query_params.get("top_n", "3")), 1), 10)
    except (TypeError, ValueError):
        top_n = 3
    bump = (request.query_params.get("bump", "1") or "").lower() not in ("0", "false", "no", "")
    tool = (request.query_params.get("tool") or "").strip()[:32]  # which tool pulled (claude/cursor/codex/…)
    EXCERPT_CAP = 2000  # hook only excerpts ~400 chars; cap payload size

    driver = get_status_driver()
    rows: list[dict] = []
    seen: set[str] = set()

    # Pass 1: rank the canonical capsule set by real relevance + scope prior,
    # with a single bounded exploration slot. Replaces the old flat score=100,
    # which made every canonical a tie broken by insertion order + top_n cut —
    # i.e. selection-by-coincidence (see capsule-usage-audit-2026-06-18).
    proj_scope = f"project:{kw}"
    kw_lc = kw.lower()
    cand: list[dict] = []
    try:
        r1, _, _ = await driver.execute_query(
            # G3: 排除归档胶囊（如 INCIDENT-RETRO），不再进 canonical 注入候选
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
            "AND NOT coalesce(n.archived, false) "
            "RETURN n.name AS name, n.content AS content, "
            "       n.source_description AS source, "
            "       coalesce(n.usage_count, 0) AS uc, n.scope AS scope",
        )
        for row in r1:
            nm = row.get("name")
            content = row.get("content") or ""
            scope = row.get("scope") or CANONICAL_SCOPE.get(nm, DEFAULT_SCOPE)
            hits = content.lower().count(kw_lc)
            # Eligibility: global (everywhere) OR mentions cwd OR this project.
            if not (scope == "global" or hits > 0 or scope == proj_scope):
                continue
            if scope == proj_scope:
                bonus = SCOPE_MATCH_BONUS
            elif scope.startswith("project:"):
                bonus = SCOPE_OTHER_PENALTY
            else:  # global
                bonus = 0.0
            cand.append({
                "name": nm,
                "content": content[:EXCERPT_CAP],
                "source": row.get("source") or "",
                "is_canonical": True,
                "uc": int(row.get("uc") or 0),
                "relscore": math.log1p(hits) + bonus,
            })
            seen.add(nm)
    except Exception as exc:
        logger.warning("[canonical_context] canonical pass failed for kw=%r: %s", kw, exc)

    # Select: relevance fills all but the last slot; the final slot is reserved
    # for the most under-exposed eligible capsule (deterministic UCB) so the
    # long tail and newly-added capsules can't be permanently starved.
    # Primary: relevance. Tie-break: lower usage first, so equally-relevant
    # capsules rotate through the relevance slots too (not just the explore
    # slot) — otherwise an incumbent with high usage keeps an arbitrary tie.
    cand.sort(key=lambda r: (-r["relscore"], r["uc"]))
    if len(cand) > top_n and top_n >= 2:
        picked_canon = cand[:top_n - 1]
        rest = cand[top_n - 1:]
        T = sum(c["uc"] for c in cand) + 1
        picked_canon.append(
            max(rest, key=lambda r: EXPLORE_C * math.sqrt(math.log(T + 1) / (r["uc"] + 1)))
        )
    else:
        picked_canon = cand[:top_n]
    for c in picked_canon:
        c["score"] = round(c["relscore"], 3)  # display/back-compat field
    rows.extend(picked_canon)

    # Pass 2: general fulltext over Episodic to fill remaining slots.
    if len(rows) < top_n:
        try:
            r2, _, _ = await driver.execute_query(
                "CALL db.idx.fulltext.queryNodes('Episodic', $q) YIELD node, score "
                "WHERE NOT node.name IN $exclude AND NOT coalesce(node.archived, false) "
                "RETURN node.name AS name, node.content AS content, "
                "       node.source_description AS source, score AS score "
                "ORDER BY score DESC LIMIT $lim",
                q=kw,
                exclude=list(seen),
                lim=(top_n - len(rows)) * 3,
            )
            for row in r2:
                nm = row.get("name")
                rows.append({
                    "name": nm,
                    "content": (row.get("content") or "")[:EXCERPT_CAP],
                    "source": row.get("source") or "",
                    "score": float(row.get("score") or 0),
                    "is_canonical": bool(nm and nm.startswith("kg-hub-canonical-")),
                })
        except Exception as exc:
            logger.warning("[canonical_context] pass2 failed for q=%r: %s", kw, exc)

    # rank_and_pick: canonical first (already relevance-ranked + exploration-
    # padded above, so keep that order), then fulltext others by score.
    canonical = [r for r in rows if r["is_canonical"]]
    others_all = [r for r in rows if not r["is_canonical"]]
    picked = canonical[:top_n]
    if len(picked) < top_n:
        # G5 交付分层：type-weighted 重排 others 填位（enabled=false 时等价原按 score 取）
        picked.extend(_tiered_rerank(others_all, top_n - len(picked)))
    _log_delivery("canonical_context", kw, picked)  # G6-lite：记录本次注入命中

    bumped = 0
    if bump and picked:
        try:
            br, _, _ = await driver.execute_query(
                "MATCH (n:Episodic) WHERE n.name IN $names "
                "SET n.usage_count = coalesce(n.usage_count, 0) + 1, "
                "    n.last_used_at = $now "
                "RETURN count(n) AS c",
                names=[p["name"] for p in picked],
                now=datetime.now(tz=timezone.utc).isoformat(),
            )
            if br:
                bumped = int(br[0].get("c") or 0)
        except Exception as exc:
            logger.warning("[canonical_context] usage bump failed: %s", exc)
        # Per-tool injection tally — which tool is actually pulling kg-hub.
        if tool:
            try:
                await driver.execute_query(
                    "MERGE (t:ToolStat {tool: $tool}) "
                    "SET t.injections = coalesce(t.injections, 0) + 1, t.last_at = $now",
                    tool=tool, now=datetime.now(tz=timezone.utc).isoformat())
            except Exception as exc:
                logger.warning("[canonical_context] tool stat failed: %s", exc)

    return JSONResponse({
        "status": "ok",
        "keyword": kw,
        "bumped": bumped,
        "picked": picked,
    })


async def usage_ranking(request: Request) -> JSONResponse:
    """GET /api/usage_ranking?top_n=10

    Server-side usage-count ranking (the Lindy / implicit-feedback signal the
    PUSH hook produces by bumping `usage_count` on injected canonical episodes).
    Returns three rankings + summary stats as JSON, computed on localhost
    FalkorDB. `tools/usage_ranking.py` fetches this over HTTP and renders the
    markdown report — replacing its old direct-FalkorDB connection, which the
    Mac can no longer reach after the NAS migration (falkordb binds localhost
    only). Same lesson as the push hook: reads/writes收敛到与图同机的 server。
    """
    try:
        top_n = min(max(int(request.query_params.get("top_n", "10")), 1), 50)
    except (TypeError, ValueError):
        top_n = 10
    driver = get_status_driver()

    async def q(cypher, **params):
        rows, _, _ = await driver.execute_query(cypher, **params)
        return rows

    try:
        r = await q(
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
            "AND coalesce(n.usage_count, 0) > 0 "
            "RETURN n.name AS name, coalesce(n.usage_count, 0) AS uc, n.last_used_at AS last "
            "ORDER BY uc DESC LIMIT $n", n=top_n)
        top_canonical = [{"name": x.get("name"), "usage_count": int(x.get("uc") or 0),
                          "last_used_at": x.get("last")} for x in r]

        r = await q(
            "MATCH (n:Episodic) WHERE NOT (n.name STARTS WITH 'kg-hub-canonical') "
            "AND coalesce(n.usage_count, 0) > 0 "
            "RETURN n.name AS name, coalesce(n.usage_count, 0) AS uc, "
            "substring(coalesce(n.content, ''), 0, 80) AS preview "
            "ORDER BY uc DESC LIMIT $n", n=top_n)
        promote = [{"name": x.get("name"), "usage_count": int(x.get("uc") or 0),
                    "preview": x.get("preview") or ""} for x in r]

        r = await q(
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
            "AND coalesce(n.usage_count, 0) = 0 "
            "RETURN n.name AS name, n.created_at AS created "
            "ORDER BY n.created_at LIMIT $n", n=top_n)
        demote = [{"name": x.get("name"), "created_at": x.get("created")} for x in r]

        r = await q(
            "MATCH (n:Episodic) RETURN count(n) AS total, "
            "sum(coalesce(n.usage_count, 0)) AS total_usage, "
            "sum(CASE WHEN coalesce(n.usage_count, 0) > 0 THEN 1 ELSE 0 END) AS used_count")
        row = r[0] if r else {}
        rc = await q(
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
            "RETURN count(n) AS total, sum(coalesce(n.usage_count, 0)) AS used")
        crow = rc[0] if rc else {}
        stats = {
            "total_episodes": int(row.get("total") or 0),
            "total_usage_events": int(row.get("total_usage") or 0),
            "episodes_with_usage": int(row.get("used_count") or 0),
            "canonical_total": int(crow.get("total") or 0),
            "canonical_total_usage": int(crow.get("used") or 0),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("[usage_ranking] query failed")
        return JSONResponse(
            {"status": "error", "message": f"{type(exc).__name__}: {exc}"}, status_code=500
        )

    return JSONResponse({
        "status": "ok",
        "stats": stats,
        "top_canonical": top_canonical,
        "promote": promote,
        "demote": demote,
    })


async def stats(request: Request) -> JSONResponse:
    """GET /api/stats — entity/edge/episode counts (NAS-local read for kg_stats)."""
    driver = get_status_driver()

    async def c(cypher):
        rows, _, _ = await driver.execute_query(cypher)
        return rows

    ent = await c("MATCH (n:Entity) RETURN count(n) AS c")
    edg = await c("MATCH (a:Entity)-[e:RELATES_TO]->(b:Entity) RETURN count(e) AS c")
    epi = await c("MATCH (n:Episodic) RETURN count(n) AS c")
    return JSONResponse({
        "status": "ok",
        "entities": int(ent[0]["c"]) if ent else 0,
        "edges": int(edg[0]["c"]) if edg else 0,
        "episodes": int(epi[0]["c"]) if epi else 0,
    })


async def episode_search(request: Request) -> JSONResponse:
    """GET /api/episode_search?q=&num_results= — fulltext over Episodic (+ substring fallback)."""
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return JSONResponse({"status": "error", "message": "missing q"}, status_code=400)
    try:
        lim = min(max(int(request.query_params.get("num_results", "5")), 1), 15)
    except (TypeError, ValueError):
        lim = 5
    driver = get_status_driver()
    # disabled 时 pool=lim（查询与今日字节一致，避免平局下返回不同子集）；enabled 时放大供重排
    pool = min(lim * 3, 45) if _DELIVERY.get("enabled") else lim
    try:
        rows, _, _ = await driver.execute_query(
            "CALL db.idx.fulltext.queryNodes('Episodic', $q) YIELD node, score "
            "WHERE NOT coalesce(node.archived, false) "
            "RETURN node.name AS name, node.content AS content, "
            "node.source_description AS source, score "
            f"ORDER BY score DESC LIMIT {pool}",
            q=q,
        )
    except Exception:
        rows, _, _ = await driver.execute_query(
            "MATCH (n:Episodic) WHERE (n.name CONTAINS $q OR n.content CONTAINS $q) "
            "AND NOT coalesce(n.archived, false) "
            "RETURN n.name AS name, n.content AS content, "
            f"n.source_description AS source, 0.0 AS score LIMIT {pool}",
            q=q,
        )
    items = [{"name": r.get("name"), "source": r.get("source"),
              "score": r.get("score"), "content": r.get("content")} for r in rows]
    items = _tiered_rerank(items, lim)          # G5：type-weighted（enabled=false 时等价原样）
    _log_delivery("episode_search", q, items)   # G6-lite
    results = [{
        "name": it.get("name"),
        "source": it.get("source"),
        "score": it.get("score"),
        "body_preview": (it.get("content") or "")[:600],
    } for it in items]
    return JSONResponse({"status": "ok", "results": results})


async def node_neighbors(request: Request) -> JSONResponse:
    """GET /api/node_neighbors?name=&limit= — fuzzy-match an Entity, return 1-hop neighbors."""
    name = (request.query_params.get("name") or "").strip()
    if not name:
        return JSONResponse({"status": "error", "message": "missing name"}, status_code=400)
    try:
        limit = min(max(int(request.query_params.get("limit", "20")), 1), 100)
    except (TypeError, ValueError):
        limit = 20
    driver = get_status_driver()
    m, _, _ = await driver.execute_query(
        "MATCH (n:Entity) WHERE n.name = $name OR n.name CONTAINS $name "
        "RETURN n.name AS name, labels(n) AS labels LIMIT 1",
        name=name,
    )
    if not m:
        return JSONResponse({"status": "ok", "matched_node": None, "labels": [], "neighbors": []})
    matched = m[0]["name"]
    labels = list(m[0].get("labels") or [])
    out_rows, _, _ = await driver.execute_query(
        "MATCH (a:Entity {name: $name})-[e:RELATES_TO]->(b:Entity) "
        "RETURN b.name AS name, e.name AS edge, e.fact AS fact LIMIT $lim",
        name=matched, lim=limit,
    )
    in_rows, _, _ = await driver.execute_query(
        "MATCH (a:Entity)-[e:RELATES_TO]->(b:Entity {name: $name}) "
        "RETURN a.name AS name, e.name AS edge, e.fact AS fact LIMIT $lim",
        name=matched, lim=limit,
    )
    neighbors = ([{**r, "direction": "out"} for r in out_rows]
                 + [{**r, "direction": "in"} for r in in_rows])
    return JSONResponse({"status": "ok", "matched_node": matched, "labels": labels,
                         "neighbors": neighbors})


async def path_between(request: Request) -> JSONResponse:
    """GET /api/path_between?source=&target=&max_hops= — up to 3 paths between two entities."""
    src = (request.query_params.get("source") or "").strip()
    tgt = (request.query_params.get("target") or "").strip()
    if not src or not tgt:
        return JSONResponse({"status": "error", "message": "missing source/target"}, status_code=400)
    try:
        hops = min(max(int(request.query_params.get("max_hops", "4")), 1), 6)
    except (TypeError, ValueError):
        hops = 4
    driver = get_status_driver()
    rows, _, _ = await driver.execute_query(
        f"MATCH path = (a:Entity)-[:RELATES_TO*1..{hops}]->(b:Entity) "
        "WHERE a.name CONTAINS $src AND b.name CONTAINS $tgt "
        "RETURN [n IN nodes(path) | n.name] AS names LIMIT 3",
        src=src, tgt=tgt,
    )
    return JSONResponse({"status": "ok", "paths": [r["names"] for r in rows if r.get("names")]})


# Vector-only edge search config for /api/search_semantic. graphiti's user-facing
# search defaults to bm25+cosine hybrid; the bm25 fulltext leg is the slow /
# CPU-pegging path (see graphiti_client dedup note). Keep cosine_similarity only:
# semantic recall without the slow fulltext leg.
import copy as _copy_search  # noqa: E402
from graphiti_core.search.search_config_recipes import (  # noqa: E402
    EDGE_HYBRID_SEARCH_RRF as _EDGE_RRF_SRC,
)

_EDGE_VEC_ONLY = _copy_search.deepcopy(_EDGE_RRF_SRC)
_cos_methods = [m for m in _EDGE_VEC_ONLY.edge_config.search_methods
                if "cosine" in str(getattr(m, "value", m)).lower()]
if _cos_methods:
    _EDGE_VEC_ONLY.edge_config.search_methods = _cos_methods


async def search_semantic(request: Request) -> JSONResponse:
    """GET /api/search_semantic?q=&num_results= — vector-only semantic edge search.

    The MCP kg_search tool calls this over one HTTP hop. Uses graphiti's edge
    search with cosine_similarity only (drops the slow bm25 fulltext leg), so it
    gives natural-language semantic recall without the CPU-pegging fulltext path.
    Runs on NAS-local FalkorDB. For literal keyword / liveness probes use /api/search.
    """
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return JSONResponse({"status": "error", "message": "missing q"}, status_code=400)
    try:
        num = min(max(int(request.query_params.get("num_results", "10")), 1), 30)
    except (TypeError, ValueError):
        num = 10
    try:
        g = await get_graphiti()
        cfg = _copy_search.deepcopy(_EDGE_VEC_ONLY)
        cfg.limit = num
        results = await g._search(query=q, config=cfg)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[search_semantic] failed for q=%r", q)
        return JSONResponse(
            {"status": "error", "message": f"{type(exc).__name__}: {exc}"}, status_code=500
        )
    out = [{
        "fact": e.fact,
        "source_node_uuid": str(e.source_node_uuid),
        "target_node_uuid": str(e.target_node_uuid),
        "valid_at": e.valid_at.isoformat() if e.valid_at else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    } for e in results.edges]
    return JSONResponse({"status": "ok", "query": q, "mode": "semantic_vector", "results": out})


# ---------- App ----------
# ---------- Report portal + dashboards (read-only, server-rendered) ----------
# 统一入口：所有看板/报表收拢到 /portal。新增报表 = 在 PORTAL_REPORTS 加一条
# {name,desc,url} + 写一个 /dashboard/* 处理器。数据服务端渲染进页面（免客户端
# 二次鉴权）；17171 仅绑 NAS loopback + tailscale。
PORTAL_REPORTS = [
    {"name": "知识胶囊看板", "desc": "canonical 胶囊曝光 + 各 cwd 下实时排序与注入",
     "url": "/dashboard/capsules", "icon": "📎", "ready": True},
    {"name": "使用排行", "desc": "胶囊累计注入排行 + 建议晋升 / 建议下线",
     "url": "/dashboard/usage", "icon": "📊", "ready": True},
    {"name": "知识库速览", "desc": "全图概览(Episode/实体/关系) + 最近知识",
     "url": "/dashboard/knowledge", "icon": "🧠", "ready": True},
    {"name": "知识使用率", "desc": "全图/胶囊利用率 + 被取用知识 + 沉睡长尾",
     "url": "/dashboard/utilization", "icon": "📈", "ready": True},
    {"name": "各工具与 kg-hub", "desc": "各工具贡献(写入) + 注入使用(读取)统计",
     "url": "/dashboard/tools", "icon": "🛠", "ready": True},
    {"name": "案例整理台", "desc": "给知识打标签(内部/方法/可公开)+验证,一键操作",
     "url": "/dashboard/curate", "icon": "🗂", "ready": True},
    {"name": "运营反馈", "desc": "录入文章阅读/点赞/涨粉,写回知识库(真实 outcome)",
     "url": "/dashboard/feedback", "icon": "📣", "ready": True},
    {"name": "反馈待办", "desc": "自动列出需你拍板的:待分层(AI已建议)+待补运营数据",
     "url": "/dashboard/inbox", "icon": "📥", "ready": True},
]

_PORTAL_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>kg-hub 报表门户</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
h1{font-size:20px;font-weight:500}.sub{color:GrayText;font-size:13px;margin-bottom:1.5rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
a.card{display:block;text-decoration:none;color:inherit;border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:12px;padding:1rem 1.1rem}
a.card:hover{border-color:color-mix(in srgb,CanvasText 45%,transparent)}
.t{font-size:15px;font-weight:500}.d{font-size:13px;color:GrayText;margin-top:4px}
.soon{opacity:.5;pointer-events:none}.foot{color:GrayText;font-size:12px;margin-top:2rem}</style></head><body>
<h1>kg-hub 报表门户</h1><div class=sub>所有看板/报表的统一入口 · 部署在常开 NAS · tailnet 内任意设备可访问</div>
<div class=grid id=grid></div>
<div class=foot>新增报表：kg_hub_server.PORTAL_REPORTS 加一条 + 写 /dashboard/* 处理器。</div>
<script>document.getElementById('grid').innerHTML=(__DATA__).map(function(r){
return '<a class="card'+(r.ready?'':' soon')+'" href="'+r.url+'"><div class=t>'+r.icon+' '+r.name+(r.ready?'':' · 即将上线')+'</div><div class=d>'+r.desc+'</div></a>';}).join('');</script></body></html>"""


async def portal(request: Request) -> HTMLResponse:
    return HTMLResponse(_PORTAL_HTML.replace("__DATA__", json.dumps(PORTAL_REPORTS, ensure_ascii=False)))


async def portal_manifest(request: Request) -> JSONResponse:
    """GET /portal_manifest — this source's report cards, for the standalone
    aggregator portal to fetch and merge. Auth-exempt (read-only card metadata
    only; covered by the `/portal*` allowlist). URLs are relative to this
    server's base; the aggregator prefixes them with this source's link base."""
    return JSONResponse({"source": "kg-hub", "reports": PORTAL_REPORTS})


_DASH_CAPSULES_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=60>
<title>kg-hub 知识胶囊看板</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:860px;margin:1.5rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
a.back{font-size:13px;color:GrayText;text-decoration:none}h1{font-size:20px;font-weight:500;margin:.3rem 0}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:1rem 0}
.mc{background:color-mix(in srgb,CanvasText 6%,transparent);border-radius:8px;padding:.7rem .9rem}
.mc .l{font-size:13px;color:GrayText}.mc .v{font-size:22px;font-weight:500}
.lbl{font-size:12px;color:GrayText;margin:1.3rem 0 .3rem}
.row{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent)}
.nm{flex:1;min-width:120px;font-family:ui-monospace,Menlo,monospace;font-size:13px}
.bar{width:110px;height:6px;border-radius:3px;overflow:hidden;background:color-mix(in srgb,CanvasText 10%,transparent)}
.bar>i{display:block;height:100%}.bdg{font-size:11px;padding:2px 8px;border-radius:8px}
.g{background:#E1F5EE;color:#085041}.p{background:#EEEDFE;color:#3C3489}
.inj{background:#E6F1FB;color:#185FA5;font-size:11px;padding:2px 6px;border-radius:8px}
button{font-family:ui-monospace,monospace;font-size:13px;padding:4px 10px;border-radius:8px;border:1px solid color-mix(in srgb,CanvasText 25%,transparent);background:transparent;color:inherit;cursor:pointer}
button[aria-pressed=true]{background:color-mix(in srgb,CanvasText 12%,transparent)}
.ts{color:GrayText;font-size:12px}
details{border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent)}details .row{border-bottom:none}
summary{list-style:none;cursor:pointer}summary::-webkit-details-marker{display:none}
summary:hover{background:color-mix(in srgb,CanvasText 5%,transparent)}
.chev{color:GrayText;transition:transform .15s;width:14px;text-align:center;flex:none}
details[open] .chev{transform:rotate(90deg)}
.body{padding:.4rem .2rem 1.1rem;font-size:14px;overflow-x:auto;border-left:2px solid color-mix(in srgb,CanvasText 12%,transparent);margin:.2rem 0 .6rem;padding-left:12px}
.body pre{white-space:pre-wrap;font-size:12px}.body code{font-family:ui-monospace,monospace;font-size:12px}
.body h1,.body h2,.body h3{font-size:15px;font-weight:500;margin:.7rem 0 .3rem}
.body table{border-collapse:collapse;font-size:12px}.body td,.body th{border:1px solid color-mix(in srgb,CanvasText 15%,transparent);padding:3px 6px}</style></head><body>
<a class=back href="/portal">← 报表门户</a><h1>知识胶囊看板</h1>
<div class=cards id=cards></div>
<div class=lbl>胶囊总览 · 按曝光排序</div><div id=caps></div>
<div class=lbl>实时排序 · 选 cwd 关键词（<span style="color:#185FA5">注入</span> = 进 top-3 会被钉进会话）</div>
<div id=kw style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:.5rem"></div><div id=rank></div>
<div class=ts style="margin-top:1.5rem">每 60s 自动刷新 · score = log1p(命中数)+scope加成 · 曝光=被注入次数(非贡献度)</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.3.0/marked.min.js"></script>
<script>var D=__DATA__;
function badge(s){var g=s==='global';return '<span class="bdg '+(g?'g':'p')+'">'+(g?'global':'kg-hub')+'</span>';}
var s=D.stats;document.getElementById('cards').innerHTML='<div class=mc><div class=l>胶囊总数</div><div class=v>'+s.canonical_total+'</div></div><div class=mc><div class=l>累计注入</div><div class=v>'+s.canonical_total_usage+'</div></div><div class=mc><div class=l>有曝光</div><div class=v>'+s.with_usage+' / '+s.canonical_total+'</div></div>';
var mu=Math.max.apply(null,D.caps.map(function(c){return c.usage}).concat([1]));
document.getElementById('caps').innerHTML=D.caps.map(function(c){return '<details><summary class=row><span class=nm>'+c.name+'</span>'+badge(c.scope)+'<div class=bar><i style="width:'+Math.round(c.usage/mu*100)+'%;background:#888780"></i></div><span style="width:32px;text-align:right;font-weight:500">'+c.usage+'</span><span class=ts style="width:80px;text-align:right">'+c.last+'</span><span class=chev>›</span></summary><div class=body></div></details>';}).join('');
Array.prototype.forEach.call(document.querySelectorAll('#caps details'),function(d,i){d.addEventListener('toggle',function(){if(d.open&&!d.dataset.done){var md=D.caps[i].content||'(无内容)';var b=d.querySelector('.body');try{b.innerHTML=marked.parse(md);}catch(e){var p=document.createElement('pre');p.textContent=md;b.innerHTML='';b.appendChild(p);}d.dataset.done='1';}});});
function rank(kw){var r=D.rankings[kw]||[];var mx=Math.max.apply(null,r.map(function(x){return x.score}).concat([0.001]));document.getElementById('rank').innerHTML=r.map(function(x,i){return '<div class=row><span class=ts style="width:16px">'+(i+1)+'</span><span class=nm>'+x.name+'</span>'+badge(x.scope)+'<div class=bar><i style="width:'+Math.round(x.score/mx*100)+'%;background:'+(x.injected?'#378ADD':'#B4B2A9')+'"></i></div><span style="width:38px;text-align:right">'+x.score.toFixed(2)+'</span><span style="width:46px;text-align:right">'+(x.injected?'<span class=inj>注入</span>':'')+'</span></div>';}).join('');}
var kws=Object.keys(D.rankings);document.getElementById('kw').innerHTML=kws.map(function(k,i){return '<button data-k="'+k+'" aria-pressed="'+(i===0)+'">'+k+'</button>';}).join('');
Array.prototype.forEach.call(document.querySelectorAll('#kw button'),function(b){b.onclick=function(){Array.prototype.forEach.call(document.querySelectorAll('#kw button'),function(x){x.setAttribute('aria-pressed','false')});b.setAttribute('aria-pressed','true');rank(b.dataset.k);};});
rank(kws[0]);</script></body></html>"""

_DASH_KWS = ["kg-hub", "workspace_claudeCode", "sd-server"]


async def dashboard_capsules(request: Request) -> HTMLResponse:
    driver = get_status_driver()
    try:
        rows, _, _ = await driver.execute_query(
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
            "AND NOT coalesce(n.archived, false) "
            "RETURN n.name AS name, n.content AS content, "
            "       coalesce(n.usage_count,0) AS uc, n.last_used_at AS last, n.scope AS scope")
    except Exception as exc:
        return HTMLResponse(f"<p>dashboard 取数失败: {exc}</p>", status_code=503)

    caps_raw = []
    for r in rows:
        nm = r.get("name")
        caps_raw.append({
            "name": nm, "content": r.get("content") or "",
            "scope": r.get("scope") or CANONICAL_SCOPE.get(nm, DEFAULT_SCOPE),
            "usage": int(r.get("uc") or 0), "last": (r.get("last") or "")[:10] or "—",
        })
    def _cap_view(c):
        body = c["content"]
        if len(body) > 8000:
            body = body[:8000] + "\n\n…（已截断，完整见源文档）"
        return {"name": c["name"].replace("kg-hub-canonical-", ""), "scope": c["scope"],
                "usage": c["usage"], "last": c["last"], "content": body}
    caps = sorted((_cap_view(c) for c in caps_raw), key=lambda c: -c["usage"])

    rankings = {}
    for kw in _DASH_KWS:
        proj, kl, scored = f"project:{kw}", kw.lower(), []
        for c in caps_raw:
            hits = c["content"].lower().count(kl)
            if not (c["scope"] == "global" or hits > 0 or c["scope"] == proj):
                continue
            bonus = (SCOPE_MATCH_BONUS if c["scope"] == proj
                     else SCOPE_OTHER_PENALTY if c["scope"].startswith("project:") else 0.0)
            scored.append({"name": c["name"].replace("kg-hub-canonical-", ""),
                           "scope": c["scope"], "score": round(math.log1p(hits) + bonus, 3)})
        scored.sort(key=lambda x: -x["score"])
        for i, x in enumerate(scored):
            x["injected"] = i < 3
        rankings[kw] = scored

    data = {"stats": {"canonical_total": len(caps_raw),
                      "canonical_total_usage": sum(c["usage"] for c in caps_raw),
                      "with_usage": sum(1 for c in caps_raw if c["usage"] > 0)},
            "caps": caps, "rankings": rankings}
    return HTMLResponse(_DASH_CAPSULES_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))


_DASH_USAGE_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=60>
<title>kg-hub 使用排行</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:860px;margin:1.5rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
a.back{font-size:13px;color:GrayText;text-decoration:none}h1{font-size:20px;font-weight:500;margin:.3rem 0}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:1rem 0}
.mc{background:color-mix(in srgb,CanvasText 6%,transparent);border-radius:8px;padding:.7rem .9rem}
.mc .l{font-size:13px;color:GrayText}.mc .v{font-size:22px;font-weight:500}
.lbl{font-size:12px;color:GrayText;margin:1.3rem 0 .3rem}
.row{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent)}
.nm{flex:1;min-width:120px;font-family:ui-monospace,Menlo,monospace;font-size:13px;word-break:break-all}
.bar{width:110px;height:6px;border-radius:3px;overflow:hidden;background:color-mix(in srgb,CanvasText 10%,transparent)}
.bar>i{display:block;height:100%}.ts{color:GrayText;font-size:12px}
.empty{color:GrayText;font-size:13px;padding:6px 0}</style></head><body>
<a class=back href="/portal">← 报表门户</a><h1>使用排行</h1>
<div class=cards id=cards></div>
<div class=lbl>胶囊累计注入排行 · canonical 被 PUSH 钩子注入的次数（Lindy / 隐式反馈信号）</div><div id=top></div>
<div class=lbl>建议晋升 · 高频命中但尚非 canonical 的普通节点</div><div id=promote></div>
<div class=lbl>建议下线 · 零曝光的 canonical 胶囊（按创建时间）</div><div id=demote></div>
<div class=ts style="margin-top:1.5rem">每 60s 自动刷新 · 曝光=被注入次数(非贡献度) · 数据同 /api/usage_ranking</div>
<script>var D=__DATA__;var s=D.stats;
document.getElementById('cards').innerHTML='<div class=mc><div class=l>胶囊总数</div><div class=v>'+s.canonical_total+'</div></div><div class=mc><div class=l>胶囊累计注入</div><div class=v>'+s.canonical_total_usage+'</div></div><div class=mc><div class=l>全图有曝光</div><div class=v>'+s.episodes_with_usage+' / '+s.total_episodes+'</div></div>';
function fill(id,arr,render){var el=document.getElementById(id);if(!arr||!arr.length){el.innerHTML='<div class=empty>暂无</div>';return;}el.innerHTML=arr.map(render).join('');}
var mu=Math.max.apply(null,D.top_canonical.map(function(x){return x.usage_count}).concat([1]));
fill('top',D.top_canonical,function(x,i){return '<div class=row><span class=ts style="width:16px">'+(i+1)+'</span><span class=nm>'+x.name.replace('kg-hub-canonical-','')+'</span><div class=bar><i style="width:'+Math.round(x.usage_count/mu*100)+'%;background:#378ADD"></i></div><span style="width:32px;text-align:right;font-weight:500">'+x.usage_count+'</span><span class=ts style="width:80px;text-align:right">'+((x.last_used_at||'').slice(0,10)||'—')+'</span></div>';});
fill('promote',D.promote,function(x){return '<div class=row><span class=nm>'+x.name+(x.preview?' <span class=ts>'+x.preview.replace(/[<>]/g,'')+'</span>':'')+'</span><span style="width:32px;text-align:right;font-weight:500">'+x.usage_count+'</span></div>';});
fill('demote',D.demote,function(x){return '<div class=row><span class=nm>'+x.name.replace('kg-hub-canonical-','')+'</span><span class=ts style="width:90px;text-align:right">'+((x.created_at||'').slice(0,10)||'—')+'</span></div>';});
</script></body></html>"""


async def dashboard_usage(request: Request) -> HTMLResponse:
    """Server-rendered view over the same data as /api/usage_ranking."""
    driver = get_status_driver()

    async def q(cypher, **params):
        rows, _, _ = await driver.execute_query(cypher, **params)
        return rows

    top_n = 15
    try:
        r = await q(
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
            "AND coalesce(n.usage_count, 0) > 0 "
            "RETURN n.name AS name, coalesce(n.usage_count, 0) AS uc, n.last_used_at AS last "
            "ORDER BY uc DESC LIMIT $n", n=top_n)
        top_canonical = [{"name": x.get("name"), "usage_count": int(x.get("uc") or 0),
                          "last_used_at": x.get("last")} for x in r]

        r = await q(
            "MATCH (n:Episodic) WHERE NOT (n.name STARTS WITH 'kg-hub-canonical') "
            "AND coalesce(n.usage_count, 0) > 0 "
            "RETURN n.name AS name, coalesce(n.usage_count, 0) AS uc, "
            "substring(coalesce(n.content, ''), 0, 80) AS preview "
            "ORDER BY uc DESC LIMIT $n", n=top_n)
        promote = [{"name": x.get("name"), "usage_count": int(x.get("uc") or 0),
                    "preview": x.get("preview") or ""} for x in r]

        r = await q(
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
            "AND coalesce(n.usage_count, 0) = 0 "
            "RETURN n.name AS name, n.created_at AS created "
            "ORDER BY n.created_at LIMIT $n", n=top_n)
        demote = [{"name": x.get("name"), "created_at": x.get("created")} for x in r]

        r = await q(
            "MATCH (n:Episodic) RETURN count(n) AS total, "
            "sum(coalesce(n.usage_count, 0)) AS total_usage, "
            "sum(CASE WHEN coalesce(n.usage_count, 0) > 0 THEN 1 ELSE 0 END) AS used_count")
        row = r[0] if r else {}
        rc = await q(
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
            "RETURN count(n) AS total, sum(coalesce(n.usage_count, 0)) AS used")
        crow = rc[0] if rc else {}
        stats = {
            "total_episodes": int(row.get("total") or 0),
            "total_usage_events": int(row.get("total_usage") or 0),
            "episodes_with_usage": int(row.get("used_count") or 0),
            "canonical_total": int(crow.get("total") or 0),
            "canonical_total_usage": int(crow.get("used") or 0),
        }
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p>dashboard 取数失败: {exc}</p>", status_code=503)

    data = {"stats": stats, "top_canonical": top_canonical,
            "promote": promote, "demote": demote}
    return HTMLResponse(_DASH_USAGE_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))


_DASH_KNOWLEDGE_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=60>
<title>kg-hub 知识库速览</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:860px;margin:1.5rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
a.back{font-size:13px;color:GrayText;text-decoration:none}h1{font-size:20px;font-weight:500;margin:.3rem 0}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:1rem 0}
.mc{background:color-mix(in srgb,CanvasText 6%,transparent);border-radius:8px;padding:.7rem .9rem}
.mc .l{font-size:13px;color:GrayText}.mc .v{font-size:22px;font-weight:500}
.lbl{font-size:12px;color:GrayText;margin:1.3rem 0 .3rem}
.row{display:flex;gap:10px;padding:7px 0;border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent)}
.bdg{font-size:11px;padding:1px 7px;border-radius:8px;flex:none;height:fit-content;background:color-mix(in srgb,CanvasText 10%,transparent)}
.sn{flex:1;font-size:13px}.meta{color:GrayText;font-size:12px;margin-top:2px}.ts{color:GrayText;font-size:12px}
details{border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent)}details .row{border-bottom:none}
summary{list-style:none;cursor:pointer}summary::-webkit-details-marker{display:none}
summary:hover{background:color-mix(in srgb,CanvasText 5%,transparent)}
.chev{color:GrayText;transition:transform .15s;flex:none;align-self:center}details[open] .chev{transform:rotate(90deg)}
.src{font-size:11px;color:GrayText;margin:.2rem 0;word-break:break-all}
.dtl{white-space:pre-wrap;font-size:12px;font-family:ui-monospace,Menlo,monospace;background:color-mix(in srgb,CanvasText 5%,transparent);border-radius:8px;padding:10px;margin:.2rem 0 .7rem;max-height:440px;overflow:auto}</style></head><body>
<a class=back href="/portal">← 报表门户</a><h1>知识库速览</h1>
<div class=cards id=cards></div>
<form method=get action="/dashboard/knowledge" style="display:flex;gap:8px;margin:1rem 0 .3rem">
<input id=q name=q placeholder="搜索全图知识（关键词，中英文均可）…" autocomplete=off style="flex:1;padding:6px 10px;border-radius:8px;border:1px solid color-mix(in srgb,CanvasText 25%,transparent);background:Canvas;color:CanvasText;font-size:14px">
<button style="padding:6px 14px;border-radius:8px;border:1px solid color-mix(in srgb,CanvasText 25%,transparent);background:transparent;color:inherit;cursor:pointer">搜索</button>
</form>
<div class=lbl id=lbl></div><div id=items></div>
<div class=ts style="margin-top:1.5rem">搜索走全图 fulltext（+子串兜底）· 无关键词时显示最近知识 · 全图=claude-mem 等工具汇入</div>
<script>var D=__DATA__;var s=D.stats;
document.getElementById('cards').innerHTML='<div class=mc><div class=l>Episode 知识条目</div><div class=v>'+s.episodes+'</div></div><div class=mc><div class=l>实体 Entity</div><div class=v>'+s.entities+'</div></div><div class=mc><div class=l>关系 Edge</div><div class=v>'+s.edges+'</div></div>';
document.getElementById('q').value=D.q||'';
document.getElementById('lbl').textContent=D.q?('搜索结果："'+D.q+'" · '+D.items.length+' 条'):'最近知识 · 最新 observation（全图，非 canonical 胶囊）';
document.getElementById('items').innerHTML=D.items.map(function(r,i){return '<details data-i="'+i+'"><summary class=row><span class=bdg>'+r.type+'</span><div class=sn>'+r.snippet+'<div class=meta>'+r.project+' · '+r.created+'</div></div><span class=chev>›</span></summary><div class=src></div><pre class=dtl></pre></details>';}).join('')||'<div class=ts>'+(D.q?'无匹配':'暂无')+'</div>';
Array.prototype.forEach.call(document.querySelectorAll('#items details'),function(d){d.addEventListener('toggle',function(){if(d.open&&!d.dataset.done){var it=D.items[+d.dataset.i];d.querySelector('.src').textContent=(it.name||'')+(it.source?('  ·  '+it.source):'');d.querySelector('.dtl').textContent=it.detail||'(无内容)';d.dataset.done='1';}});});
</script></body></html>"""


async def dashboard_knowledge(request: Request) -> HTMLResponse:
    import re as _re
    driver = get_status_driver()
    q = (request.query_params.get("q") or "").strip()

    async def one(cy, **p):
        rows, _, _ = await driver.execute_query(cy, **p)
        return rows

    def esc(t):
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    try:
        ent = int((await one("MATCH (n:Entity) RETURN count(n) AS c"))[0].get("c") or 0)
        edg = int((await one("MATCH (a:Entity)-[e:RELATES_TO]->(b:Entity) RETURN count(e) AS c"))[0].get("c") or 0)
        epi = int((await one("MATCH (n:Episodic) RETURN count(n) AS c"))[0].get("c") or 0)
        if q:
            rows = []
            try:  # fulltext first (good for English / multi-word)
                rows = await one(
                    "CALL db.idx.fulltext.queryNodes('Episodic', $q) YIELD node, score "
                    "WHERE NOT coalesce(node.archived, false) "
                    "RETURN substring(coalesce(node.content,''),0,4000) AS detail, "
                    "node.name AS name, node.source_description AS source, node.created_at AS created "
                    "ORDER BY score DESC LIMIT 30", q=q)
            except Exception:
                rows = []
            if not rows:  # substring fallback (handles Chinese / no fulltext hit)
                rows = await one(
                    "MATCH (n:Episodic) WHERE (n.content CONTAINS $q OR n.name CONTAINS $q) "
                    "AND NOT coalesce(n.archived, false) "
                    "RETURN substring(coalesce(n.content,''),0,4000) AS detail, "
                    "n.name AS name, n.source_description AS source, n.created_at AS created LIMIT 30", q=q)
        else:  # no query → recent knowledge
            rows = await one(
                "MATCH (n:Episodic) WHERE NOT (n.name STARTS WITH 'kg-hub-canonical') "
                "AND NOT coalesce(n.archived, false) "
                "RETURN substring(coalesce(n.content,''),0,4000) AS detail, "
                "n.name AS name, n.source_description AS source, n.created_at AS created "
                "ORDER BY n.created_at DESC LIMIT 25")
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p>知识库取数失败: {exc}</p>", status_code=503)

    items = []
    for r in rows:
        src = r.get("source") or ""
        mt = _re.search(r"type=(\S+)", src)
        mp = _re.search(r"project=(\S+)", src)
        full = r.get("detail") or ""
        oneline = full.strip().replace("\n", " ")
        snippet = esc(oneline[:180]) + ("…" if len(oneline) > 180 else "")
        items.append({"type": esc(mt.group(1)) if mt else "obs",
                      "project": esc(mp.group(1)) if mp else "—",
                      "snippet": snippet, "created": (r.get("created") or "")[:16],
                      "name": r.get("name") or "", "source": src, "detail": full})
    data = {"stats": {"entities": ent, "edges": edg, "episodes": epi},
            "q": esc(q), "items": items}
    # detail/name/source 走 textContent 安全；但 JSON 内嵌进内联 <script> 时，
    # 原文里的 "</script>" 会截断脚本——把 "</" 转义为 "<\/"（JS 字符串等价）。
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return HTMLResponse(_DASH_KNOWLEDGE_HTML.replace("__DATA__", data_json))


_DASH_UTIL_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=60>
<title>kg-hub 知识使用率</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:860px;margin:1.5rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
a.back{font-size:13px;color:GrayText;text-decoration:none}h1{font-size:20px;font-weight:500;margin:.3rem 0}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:1rem 0}
.mc{background:color-mix(in srgb,CanvasText 6%,transparent);border-radius:8px;padding:.7rem .9rem}
.mc .l{font-size:13px;color:GrayText}.mc .v{font-size:22px;font-weight:500}.mc .s{font-size:12px;color:GrayText}
.lbl{font-size:12px;color:GrayText;margin:1.3rem 0 .3rem}
.row{display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent)}
.uc{width:30px;text-align:right;font-weight:500;font-size:13px;flex:none}
.bar{width:84px;height:6px;border-radius:3px;overflow:hidden;background:color-mix(in srgb,CanvasText 10%,transparent);flex:none}.bar>i{display:block;height:100%}
.nm{flex:1;font-size:13px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bdg{font-size:11px;padding:1px 7px;border-radius:8px;flex:none}.cap{background:#EEEDFE;color:#3C3489}.con{background:color-mix(in srgb,CanvasText 12%,transparent);color:GrayText}
.chev{color:GrayText;transition:transform .15s;flex:none}details[open] .chev{transform:rotate(90deg)}
details{border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent)}details .row{border-bottom:none}
summary{list-style:none;cursor:pointer}summary::-webkit-details-marker{display:none}summary:hover{background:color-mix(in srgb,CanvasText 5%,transparent)}
.src{font-size:11px;color:GrayText;margin:.2rem 0;word-break:break-all}
.dtl{white-space:pre-wrap;font-size:12px;font-family:ui-monospace,Menlo,monospace;background:color-mix(in srgb,CanvasText 5%,transparent);border-radius:8px;padding:10px;margin:.2rem 0 .7rem;max-height:440px;overflow:auto}
.ts{color:GrayText;font-size:12px}</style></head><body>
<a class=back href="/portal">← 报表门户</a><h1>知识使用率</h1>
<div class=cards id=cards></div>
<div class=lbl>被取用过的知识 · 按使用量排序（胶囊 + 普通知识）</div><div id=items></div>
<div class=ts style="margin-top:1.5rem" id=note></div>
<script>var D=__DATA__;var r=D.rates;
document.getElementById('cards').innerHTML='<div class=mc><div class=l>全图利用率</div><div class=v>'+r.util_pct+'%</div><div class=s>'+r.used+' / '+r.total+' 被取用</div></div><div class=mc><div class=l>胶囊利用率</div><div class=v>'+r.canon_pct+'%</div><div class=s>'+r.canon_used+' / '+r.canon_total+'</div></div><div class=mc><div class=l>从未取用</div><div class=v>'+r.never+'</div><div class=s>沉睡知识</div></div>';
var mu=Math.max.apply(null,D.items.map(function(x){return x.usage}).concat([1]));
document.getElementById('items').innerHTML=D.items.map(function(x,i){return '<details data-i="'+i+'"><summary class=row><span class=uc>'+x.usage+'</span><div class=bar><i style="width:'+Math.round(x.usage/mu*100)+'%;background:'+(x.cap?'#7F77DD':'#888780')+'"></i></div><span class=nm>'+x.label+'</span><span class="bdg '+(x.cap?'cap':'con')+'">'+(x.cap?'胶囊':x.type)+'</span><span class=chev>›</span></summary><div class=src></div><pre class=dtl></pre></details>';}).join('')||'<div class=ts>暂无被取用的知识</div>';
Array.prototype.forEach.call(document.querySelectorAll('#items details'),function(d){d.addEventListener('toggle',function(){if(d.open&&!d.dataset.done){var it=D.items[+d.dataset.i];d.querySelector('.src').textContent=(it.name||'')+(it.source?('  ·  '+it.source):'')+(it.last?('  ·  最近 '+it.last):'');d.querySelector('.dtl').textContent=it.detail||'(无内容)';d.dataset.done='1';}});});
document.getElementById('note').textContent='每 60s 自动刷新 · usage = 被 PUSH hook 注入/填充次数（MCP 检索暂未计入，故为下限）· 利用率低=大量知识沉睡';
</script></body></html>"""


async def dashboard_utilization(request: Request) -> HTMLResponse:
    import re as _re
    driver = get_status_driver()

    async def one(cy):
        rows, _, _ = await driver.execute_query(cy)
        return rows

    def esc(t):
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    try:
        s = (await one("MATCH (n:Episodic) RETURN count(n) AS total, "
                       "sum(CASE WHEN coalesce(n.usage_count,0)>0 THEN 1 ELSE 0 END) AS used"))[0]
        cs = (await one("MATCH (n:Episodic) WHERE n.name STARTS WITH 'kg-hub-canonical' "
                        "RETURN count(n) AS total, "
                        "sum(CASE WHEN coalesce(n.usage_count,0)>0 THEN 1 ELSE 0 END) AS used"))[0]
        top = await one(
            "MATCH (n:Episodic) WHERE coalesce(n.usage_count,0)>0 "
            "RETURN n.name AS name, substring(coalesce(n.content,''),0,4000) AS detail, "
            "n.source_description AS source, coalesce(n.usage_count,0) AS uc, n.last_used_at AS last "
            "ORDER BY uc DESC LIMIT 40")
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p>使用率取数失败: {exc}</p>", status_code=503)

    total, used = int(s.get("total") or 0), int(s.get("used") or 0)
    ct, cu = int(cs.get("total") or 0), int(cs.get("used") or 0)
    items = []
    for x in top:
        nm = x.get("name") or ""
        cap = nm.startswith("kg-hub-canonical-")
        full = x.get("detail") or ""
        src = x.get("source") or ""
        mt = _re.search(r"type=(\S+)", src)
        if cap:
            label, typ = esc(nm.replace("kg-hub-canonical-", "")), "胶囊"
        else:
            oneline = full.strip().replace("\n", " ")
            label = esc(oneline[:80]) + ("…" if len(oneline) > 80 else "")
            typ = esc(mt.group(1)) if mt else "obs"
        items.append({"label": label, "type": typ, "cap": cap,
                      "usage": int(x.get("uc") or 0), "last": (x.get("last") or "")[:10],
                      "name": nm, "source": src, "detail": full})
    rates = {"total": total, "used": used, "util_pct": round(100 * used / max(total, 1), 1),
             "canon_total": ct, "canon_used": cu, "canon_pct": round(100 * cu / max(ct, 1), 1),
             "never": total - used}
    data_json = json.dumps({"rates": rates, "items": items}, ensure_ascii=False).replace("</", "<\\/")
    return HTMLResponse(_DASH_UTIL_HTML.replace("__DATA__", data_json))


_DASH_TOOLS_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=60>
<title>kg-hub 各工具</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:860px;margin:1.5rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
a.back{font-size:13px;color:GrayText;text-decoration:none}h1{font-size:20px;font-weight:500;margin:.3rem 0}
.lbl{font-size:12px;color:GrayText;margin:1.4rem 0 .3rem}
.row{display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent)}
.nm{width:130px;flex:none;font-size:13px}
.bar{flex:1;height:8px;border-radius:4px;overflow:hidden;background:color-mix(in srgb,CanvasText 10%,transparent)}.bar>i{display:block;height:100%}
.n{width:56px;text-align:right;font-weight:500;font-size:13px;flex:none}
.ts{color:GrayText;font-size:12px}.empty{color:GrayText;font-size:13px;padding:6px 0}</style></head><body>
<a class=back href="/portal">← 报表门户</a><h1>各工具与 kg-hub</h1>
<div class=lbl>贡献（写入）· 各工具喂进 kg-hub 的知识条数（按 project 目录名归属）</div><div id=contrib></div>
<div class=lbl>使用（读取）· 各工具被注入 kg-hub 胶囊的次数（PUSH hook 上报）</div><div id=usage></div>
<div class=ts style="margin-top:1.5rem" id=note></div>
<script>var D=__DATA__;
function bars(el,arr,key,label,color){var mx=Math.max.apply(null,arr.map(function(x){return x[key]}).concat([1]));
document.getElementById(el).innerHTML=arr.length?arr.map(function(x){return '<div class=row><span class=nm>'+label(x)+'</span><div class=bar><i style="width:'+Math.round(x[key]/mx*100)+'%;background:'+color+'"></i></div><span class=n>'+x[key]+'</span></div>';}).join(''):'<div class=empty>暂无（尚无上报）</div>';}
bars('contrib',D.contrib,'count',function(x){return x.tool},'#1D9E75');
bars('usage',D.usage,'n',function(x){return x.tool+(x.last?' <span class=ts>'+x.last+'</span>':'')},'#378ADD');
document.getElementById('note').textContent='每 60s 刷新 · 贡献按 project=workspace_<工具> 归属，真实项目目录归"工具未知" · 使用=PUSH hook 注入上报（MCP 检索取知识暂未计入；当前多为 Claude Code）';
</script></body></html>"""


async def dashboard_tools(request: Request) -> HTMLResponse:
    driver = get_status_driver()

    def esc(t):
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    async def cnt(cy, **p):
        rows, _, _ = await driver.execute_query(cy, **p)
        return int(rows[0].get("c") or 0) if rows else 0

    TOOLS = [("Claude Code", "project=workspace_claudeCode "),
             ("Codex", "project=workspace_codex "),
             ("Cursor", "project=workspace_cursor "),
             ("Qoder", "project=workspace_qoder ")]
    try:
        cm_total = await cnt("MATCH (n:Episodic) WHERE n.source_description CONTAINS 'claude-mem obs' "
                             "RETURN count(n) AS c")
        contrib, known = [], 0
        for name, pat in TOOLS:
            c = await cnt("MATCH (n:Episodic) WHERE n.source_description CONTAINS $p RETURN count(n) AS c", p=pat)
            known += c
            contrib.append({"tool": name, "count": c})
        contrib.append({"tool": "工具未知", "count": max(cm_total - known, 0)})
        rows, _, _ = await driver.execute_query(
            "MATCH (t:ToolStat) RETURN t.tool AS tool, coalesce(t.injections,0) AS n, t.last_at AS last "
            "ORDER BY n DESC")
        usage = [{"tool": esc(r.get("tool")), "n": int(r.get("n") or 0),
                  "last": (r.get("last") or "")[:10]} for r in rows]
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p>工具统计取数失败: {exc}</p>", status_code=503)

    data = {"contrib": [c for c in contrib if c["count"] > 0], "usage": usage}
    return HTMLResponse(_DASH_TOOLS_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))


# 可见性枚举（写作素材分层）：内部留存 / 可提炼方法 / 可公开讲成案例
_VIS = {"internal-note": "内部", "professional-guide": "方法", "public-story": "可公开"}


async def dashboard_tag(request: Request) -> JSONResponse:
    """POST /dashboard/tag  {name, visibility?, verified?} — 给一条知识打标签。
    有界写：只能设 visibility(枚举) / verified(bool)，按 name 精确匹配 Episodic。
    免鉴权但仅 tailnet 可达、且是本人的图，故可接受（同 /dashboard* 放行）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "missing name"}, status_code=400)
    sets, params = [], {"name": name}
    if "visibility" in body:
        vis = body.get("visibility") or ""
        if vis and vis not in _VIS:
            return JSONResponse({"ok": False, "error": "bad visibility"}, status_code=400)
        sets.append("n.visibility = $vis")
        params["vis"] = vis
    if "verified" in body:
        sets.append("n.verified = $ver")
        params["ver"] = bool(body.get("verified"))
    if not sets:
        return JSONResponse({"ok": False, "error": "nothing to set"}, status_code=400)
    driver = get_status_driver()
    try:
        r, _, _ = await driver.execute_query(
            f"MATCH (n:Episodic {{name: $name}}) SET {', '.join(sets)} "
            "RETURN n.visibility AS visibility, coalesce(n.verified, false) AS verified",
            **params)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    if not r:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "name": name,
                         "visibility": r[0].get("visibility") or "",
                         "verified": bool(r[0].get("verified"))})


_DASH_CURATE_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>kg-hub 案例整理台</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:880px;margin:1.5rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
a.back{font-size:13px;color:GrayText;text-decoration:none}h1{font-size:20px;font-weight:500;margin:.3rem 0}
.tip{font-size:12px;color:GrayText;margin:.3rem 0 1rem}
.item{border:1px solid color-mix(in srgb,CanvasText 14%,transparent);border-radius:10px;padding:10px 12px;margin:8px 0}
.top{display:flex;gap:8px;align-items:flex-start}
.bdg{font-size:11px;padding:1px 7px;border-radius:8px;flex:none;background:color-mix(in srgb,CanvasText 10%,transparent)}
.sn{flex:1;font-size:13px}
.ctrl{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:8px;font-size:12px}
.ctrl .lb{color:GrayText}
button{font:inherit;font-size:12px;padding:3px 10px;border-radius:7px;border:1px solid color-mix(in srgb,CanvasText 22%,transparent);background:transparent;color:inherit;cursor:pointer}
button.on{background:#378ADD;color:#fff;border-color:#378ADD}
button.ver.on{background:#1D9E75;border-color:#1D9E75}
.saved{color:#1D9E75;font-size:12px;opacity:0;transition:opacity .2s}.saved.show{opacity:1}
.dtl{white-space:pre-wrap;font-size:12px;font-family:ui-monospace,Menlo,monospace;background:color-mix(in srgb,CanvasText 5%,transparent);border-radius:8px;padding:10px;margin-top:8px;max-height:420px;overflow:auto}
.hidden{display:none}.meta{color:GrayText;font-size:12px;margin-top:2px}</style></head><body>
<a class=back href="/portal">← 报表门户</a><h1>案例整理台</h1>
<div class=tip>给知识分层，供写作/复用挑选：<b>内部</b>=只留存 · <b>方法</b>=可提炼方法 · <b>可公开</b>=可讲成案例；<b>✓已验证</b>=结论已核实。点按钮即存，无需命令。</div>
<form method=get action="/dashboard/curate" style="display:flex;gap:8px;margin-bottom:1rem">
<input id=q name=q placeholder="搜索要整理的知识…" autocomplete=off style="flex:1;padding:6px 10px;border-radius:8px;border:1px solid color-mix(in srgb,CanvasText 25%,transparent);background:Canvas;color:CanvasText;font-size:14px">
<button>搜索</button></form>
<div id=synthbar style="display:flex;gap:8px;align-items:center;margin:.4rem 0;font-size:13px"><span>已选 <b id=cnt>0</b> 条</span><button id=synth disabled>合成选中为案例包 ↴</button><span class=tip style="margin:0">勾选相关的几条 → 合成主结论/时间线/证据/结果/可迁移</span></div>
<div id=cpresult></div>
<div id=list></div>
<script>var D=__DATA__;var VIS=[["internal-note","内部"],["professional-guide","方法"],["public-story","可公开"]];
function tag(name,patch,cb){fetch('/dashboard/tag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.assign({name:name},patch))}).then(function(r){return r.json()}).then(cb).catch(function(){cb({ok:false})});}
document.getElementById('q').value=D.q||'';
document.getElementById('list').innerHTML=D.items.length?D.items.map(function(x,i){
 var vb=VIS.map(function(v){return '<button class="vis'+(x.visibility===v[0]?' on':'')+'" data-i="'+i+'" data-v="'+v[0]+'">'+v[1]+'</button>';}).join('');
 return '<div class=item><div class=top><input type=checkbox class=pick data-i="'+i+'" style="margin-top:3px"><span class=bdg>'+x.type+'</span><div class=sn>'+x.snippet+'<div class=meta>'+x.project+' · '+x.created+'</div></div></div>'
   +'<div class=ctrl><span class=lb>可见性:</span>'+vb
   +'<button class="ver'+(x.verified?' on':'')+'" data-i="'+i+'">✓已验证</button>'
   +'<button class=exp data-i="'+i+'">详情</button><span class=saved data-i="'+i+'">✓已存</span></div>'
   +'<pre class="dtl hidden"></pre></div>';
}).join(''):'<div class=tip>无匹配</div>';
function saved(i){var s=document.querySelector('.saved[data-i="'+i+'"]');if(s){s.classList.add('show');setTimeout(function(){s.classList.remove('show')},1200);}}
document.getElementById('list').addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;var i=+b.dataset.i;var it=D.items[i];var item=b.closest('.item');
 if(b.classList.contains('vis')){var nv=(it.visibility===b.dataset.v)?'':b.dataset.v;tag(it.name,{visibility:nv},function(d){if(d.ok){it.visibility=d.visibility;item.querySelectorAll('.vis').forEach(function(x){x.classList.toggle('on',x.dataset.v===d.visibility&&d.visibility!=='');});saved(i);}});}
 else if(b.classList.contains('ver')){tag(it.name,{verified:!it.verified},function(d){if(d.ok){it.verified=d.verified;b.classList.toggle('on',d.verified);saved(i);}});}
 else if(b.classList.contains('exp')){var p=item.querySelector('.dtl');if(p.classList.contains('hidden')){p.textContent=it.detail||'(无内容)';p.classList.remove('hidden');}else{p.classList.add('hidden');}}
});
function selected(){return Array.prototype.filter.call(document.querySelectorAll('.pick'),function(c){return c.checked;}).map(function(c){return D.items[+c.dataset.i].name;});}
document.getElementById('list').addEventListener('change',function(e){if(e.target.classList.contains('pick')){var n=selected().length;document.getElementById('cnt').textContent=n;document.getElementById('synth').disabled=n<1;}});
document.getElementById('synth').addEventListener('click',function(){var names=selected();if(!names.length)return;var btn=this;btn.disabled=true;btn.textContent='合成中…';document.getElementById('cpresult').innerHTML='';
fetch('/dashboard/casepack',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({names:names})}).then(function(r){return r.json();}).then(function(d){btn.textContent='合成选中为案例包 ↴';btn.disabled=false;var box=document.getElementById('cpresult');if(d.ok){var h=document.createElement('div');h.style.margin='.6rem 0 .2rem';h.innerHTML='<b>案例包已生成并保存：</b>'+d.name+' <span class=meta>（'+d.n+' 条合成；可在知识库搜到）</span>';var pre=document.createElement('pre');pre.className='dtl';pre.style.maxHeight='none';pre.textContent=d.markdown;box.appendChild(h);box.appendChild(pre);box.scrollIntoView({behavior:'smooth'});}else{box.textContent='合成失败：'+(d.error||'未知');}}).catch(function(){btn.textContent='合成选中为案例包 ↴';btn.disabled=false;document.getElementById('cpresult').textContent='请求失败';});});
</script></body></html>"""


async def dashboard_curate(request: Request) -> HTMLResponse:
    import re as _re
    driver = get_status_driver()
    q = (request.query_params.get("q") or "").strip()

    def esc(t):
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    async def one(cy, **p):
        rows, _, _ = await driver.execute_query(cy, **p)
        return rows

    RET = ("RETURN n.name AS name, substring(coalesce(n.content,''),0,4000) AS detail, "
           "n.source_description AS source, n.created_at AS created, "
           "n.visibility AS visibility, coalesce(n.verified,false) AS verified ")
    try:
        if q:
            rows = await one("MATCH (n:Episodic) WHERE NOT (n.name STARTS WITH 'kg-hub-canonical') "
                             "AND (n.content CONTAINS $q OR n.name CONTAINS $q) " + RET + "LIMIT 40", q=q)
        else:
            rows = await one("MATCH (n:Episodic) WHERE NOT (n.name STARTS WITH 'kg-hub-canonical') "
                             + RET + "ORDER BY n.created_at DESC LIMIT 30")
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p>整理台取数失败: {exc}</p>", status_code=503)

    items = []
    for r in rows:
        src = r.get("source") or ""
        mt = _re.search(r"type=(\S+)", src)
        mp = _re.search(r"project=(\S+)", src)
        full = r.get("detail") or ""
        oneline = full.strip().replace("\n", " ")
        items.append({"name": r.get("name") or "",
                      "type": esc(mt.group(1)) if mt else "obs",
                      "project": esc(mp.group(1)) if mp else "—",
                      "snippet": esc(oneline[:120]) + ("…" if len(oneline) > 120 else ""),
                      "created": (r.get("created") or "")[:16],
                      "visibility": r.get("visibility") or "",
                      "verified": bool(r.get("verified")), "detail": full})
    data_json = json.dumps({"q": esc(q), "items": items}, ensure_ascii=False).replace("</", "<\\/")
    return HTMLResponse(_DASH_CURATE_HTML.replace("__DATA__", data_json))


async def _llm_complete(prompt: str, max_tokens: int = 1600) -> str:
    """One-shot LLM completion via the server's existing 百炼-proxied Anthropic
    endpoint (thinking disabled, like graphiti_client.build_llm). Used for
    on-demand case-pack synthesis. Raises on failure (caller returns an error)."""
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(
        auth_token=os.environ["ANTHROPIC_AUTH_TOKEN"],
        base_url=os.environ["ANTHROPIC_BASE_URL"],
        max_retries=2, timeout=90.0,
    )
    resp = await client.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "qwen3.6-plus"),
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"thinking": {"type": "disabled"}},
    )
    return "".join(getattr(b, "text", "") for b in resp.content)


CASEPACK_PROMPT = """你在把若干条零散的实践记录合成一个「案例包」，供写作 / 复用。
**只用下面提供的内容**——缺的段落写「(证据不足)」，绝不编造数字、结果或时间。

输出 markdown，正好五段：
## 主结论
一句话：这组记录的核心、可迁移的结论。
## 时间线
按时间列关键事件（带日期，取自记录）。
## 证据
来源（文件 / 报告 / 命令 / 状态）+ 记录里的具体点；没有就写 (证据不足)。
## 结果
成了吗？有什么数字 / 状态？没有明确结果就写 (证据不足)。
## 可迁移经验
下次遇到类似情况能复用什么；并写清**边界**（这个结论不能推出什么）。

== 提供的记录（共 {n} 条）==
{joined}
"""


async def dashboard_casepack(request: Request) -> JSONResponse:
    """POST /dashboard/casepack {names:[...]} — 把选中的知识 LLM 合成为案例包，
    存成一个 Episodic(case_pack=true) 节点(可检索、默认 visibility=professional-guide)。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    names = [n for n in (body.get("names") or []) if isinstance(n, str)][:12]
    if not names:
        return JSONResponse({"ok": False, "error": "no items selected"}, status_code=400)
    driver = get_status_driver()
    try:
        rows, _, _ = await driver.execute_query(
            "MATCH (n:Episodic) WHERE n.name IN $names "
            "RETURN n.name AS name, n.source_description AS source, "
            "substring(coalesce(n.content,''),0,1600) AS content, n.created_at AS created",
            names=names)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"query: {exc}"}, status_code=500)
    if not rows:
        return JSONResponse({"ok": False, "error": "selected items not found"}, status_code=404)
    parts = []
    for r in rows:
        parts.append(f"[{(r.get('created') or '')[:10]}] 来源: {r.get('source') or '—'}\n"
                     f"{(r.get('content') or '').strip()}")
    joined = "\n\n---\n\n".join(parts)[:9000]
    try:
        md = await _llm_complete(CASEPACK_PROMPT.format(n=len(rows), joined=joined))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"LLM 不可用: {type(exc).__name__}"}, status_code=502)
    if not md.strip():
        return JSONResponse({"ok": False, "error": "LLM 空返回"}, status_code=502)
    cpname = "casepack-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        await driver.execute_query(
            "CREATE (n:Episodic {name:$name, uuid:$uuid, group_id:$gid, content:$content, "
            "source_description:$src, created_at:$now, case_pack:true, sources:$srcs, "
            "visibility:'professional-guide'})",
            name=cpname, uuid=str(uuidlib.uuid4()), gid=GROUP_ID, content=md,
            src=f"case-pack 合成自 {len(rows)} 条: {', '.join(names)}",
            now=datetime.now(tz=timezone.utc).isoformat(), srcs=", ".join(names))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"存储失败: {exc}", "markdown": md}, status_code=500)
    return JSONResponse({"ok": True, "name": cpname, "markdown": md, "n": len(rows)})


async def feedback_submit(request: Request) -> JSONResponse:
    """POST /dashboard/feedback — 记一篇文章的运营表现(手动录入)。
    存成 :ArticleFeedback 节点(独立标签,不污染知识/Episodic)。这是把「哪条经验
    真带来阅读/涨粉」接上真实 outcome 信号的写入口。"""
    try:
        b = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    title = (b.get("title") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "error": "标题必填"}, status_code=400)

    def num(v):
        try:
            return int(float(v))
        except Exception:
            return 0
    driver = get_status_driver()
    fid = "fb-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        await driver.execute_query(
            "CREATE (n:ArticleFeedback {id:$id, platform:$p, title:$t, url:$u, "
            "reads:$reads, likes:$likes, shares:$shares, saves:$saves, comments:$comments, "
            "followers_delta:$fd, published_at:$pub, linked_casepack:$link, created_at:$now})",
            id=fid, p=(b.get("platform") or "").strip()[:20], t=title[:300],
            u=(b.get("url") or "").strip()[:500],
            reads=num(b.get("reads")), likes=num(b.get("likes")), shares=num(b.get("shares")),
            saves=num(b.get("saves")), comments=num(b.get("comments")), fd=num(b.get("followers_delta")),
            pub=(b.get("published_at") or "")[:10], link=(b.get("linked_casepack") or "").strip()[:120],
            now=datetime.now(tz=timezone.utc).isoformat())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"存储失败: {exc}"}, status_code=500)
    return JSONResponse({"ok": True, "id": fid})


_DASH_FEEDBACK_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>kg-hub 运营反馈</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:880px;margin:1.5rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
a.back{font-size:13px;color:GrayText;text-decoration:none}h1{font-size:20px;font-weight:500;margin:.3rem 0}
.tip{font-size:12px;color:GrayText;margin:.3rem 0 1rem}
.form{border:1px solid color-mix(in srgb,CanvasText 14%,transparent);border-radius:10px;padding:12px 14px;margin-bottom:1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
label{font-size:12px;color:GrayText;display:block}
input,select{width:100%;box-sizing:border-box;padding:6px 8px;border-radius:7px;border:1px solid color-mix(in srgb,CanvasText 22%,transparent);background:Canvas;color:CanvasText;font-size:14px}
.wide{grid-column:1/-1}
button{font:inherit;font-size:13px;padding:6px 16px;border-radius:8px;border:1px solid #378ADD;background:#378ADD;color:#fff;cursor:pointer;margin-top:10px}
.saved{color:#1D9E75;font-size:13px;margin-left:10px}
.row{display:flex;gap:8px;align-items:baseline;padding:8px 0;border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent);font-size:13px;flex-wrap:wrap}
.ttl{font-weight:500;flex:1;min-width:180px}.pf{font-size:11px;padding:1px 7px;border-radius:8px;background:color-mix(in srgb,CanvasText 10%,transparent)}
.m{color:GrayText;font-size:12px}.lk{color:#378ADD;font-size:12px}.meta{color:GrayText;font-size:12px}</style></head><body>
<a class=back href="/portal">← 报表门户</a><h1>运营反馈</h1>
<div class=tip>文章发出去后花 1 分钟录一条：让"哪条经验/案例真带来阅读和涨粉"变成数据，写回知识库。</div>
<div class=form>
  <div class=grid>
    <div><label>平台</label><select id=platform><option>公众号</option><option>小红书</option><option>微博</option><option>知乎</option><option>其他</option></select></div>
    <div><label>发布日期</label><input id=published type=date></div>
    <div class=wide><label>标题 *</label><input id=title placeholder="文章标题"></div>
    <div class=wide><label>链接</label><input id=url placeholder="https://…"></div>
    <div><label>阅读</label><input id=reads type=number value=0></div>
    <div><label>点赞</label><input id=likes type=number value=0></div>
    <div><label>转发</label><input id=shares type=number value=0></div>
    <div><label>收藏</label><input id=saves type=number value=0></div>
    <div><label>评论</label><input id=comments type=number value=0></div>
    <div><label>涨粉</label><input id=followers_delta type=number value=0></div>
    <div class=wide><label>关联案例包（可选）</label><select id=linked_casepack></select></div>
  </div>
  <button id=save>保存这条反馈</button><span id=saved class=saved></span>
</div>
<h1 style="font-size:16px">已记录</h1><div id=list></div>
<script>var D=__DATA__;
document.getElementById('published').value=new Date().toISOString().slice(0,10);
var sel=document.getElementById('linked_casepack');sel.innerHTML='<option value="">(无)</option>'+D.casepacks.map(function(c){return '<option value="'+c+'">'+c+'</option>';}).join('');
function renderList(items){document.getElementById('list').innerHTML=items.length?items.map(function(x){return '<div class=row><span class=ttl>'+(x.url?'<a class=lk href="'+x.url+'" target=_blank>'+x.title+'</a>':x.title)+'</span><span class=pf>'+x.platform+'</span><span class=m>阅读 '+x.reads+' · 赞 '+x.likes+' · 转 '+x.shares+' · 藏 '+x.saves+' · 评 '+x.comments+' · 粉 '+(x.followers_delta>=0?'+':'')+x.followers_delta+'</span><span class=meta>'+(x.published_at||'')+(x.linked_casepack?(' · '+x.linked_casepack):'')+'</span></div>';}).join(''):'<div class=tip>还没有记录</div>';}
renderList(D.items);
document.getElementById('save').addEventListener('click',function(){var btn=this;var g=function(id){return document.getElementById(id).value;};var payload={platform:g('platform'),published_at:g('published'),title:g('title'),url:g('url'),reads:g('reads'),likes:g('likes'),shares:g('shares'),saves:g('saves'),comments:g('comments'),followers_delta:g('followers_delta'),linked_casepack:g('linked_casepack')};if(!payload.title.trim()){document.getElementById('saved').textContent='标题必填';return;}btn.disabled=true;
fetch('/dashboard/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(function(r){return r.json();}).then(function(d){btn.disabled=false;if(d.ok){document.getElementById('saved').textContent='✓ 已保存';var it={title:payload.title,url:payload.url,platform:payload.platform,reads:+payload.reads||0,likes:+payload.likes||0,shares:+payload.shares||0,saves:+payload.saves||0,comments:+payload.comments||0,followers_delta:+payload.followers_delta||0,published_at:payload.published_at,linked_casepack:payload.linked_casepack};D.items.unshift(it);renderList(D.items);['title','url'].forEach(function(id){document.getElementById(id).value='';});['reads','likes','shares','saves','comments','followers_delta'].forEach(function(id){document.getElementById(id).value=0;});setTimeout(function(){document.getElementById('saved').textContent='';},1500);}else{document.getElementById('saved').textContent='失败: '+(d.error||'');}}).catch(function(){btn.disabled=false;document.getElementById('saved').textContent='请求失败';});});
</script></body></html>"""


async def dashboard_feedback(request: Request) -> HTMLResponse:
    driver = get_status_driver()

    def esc(t):
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    try:
        rows, _, _ = await driver.execute_query(
            "MATCH (n:ArticleFeedback) RETURN n.title AS title, n.url AS url, n.platform AS platform, "
            "coalesce(n.reads,0) AS reads, coalesce(n.likes,0) AS likes, coalesce(n.shares,0) AS shares, "
            "coalesce(n.saves,0) AS saves, coalesce(n.comments,0) AS comments, "
            "coalesce(n.followers_delta,0) AS fd, n.published_at AS pub, n.linked_casepack AS link "
            "ORDER BY n.created_at DESC LIMIT 50")
        cps, _, _ = await driver.execute_query(
            "MATCH (n:Episodic) WHERE n.case_pack = true RETURN n.name AS name ORDER BY n.created_at DESC LIMIT 50")
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p>运营反馈取数失败: {exc}</p>", status_code=503)

    items = [{"title": esc(r.get("title")), "url": esc(r.get("url")), "platform": esc(r.get("platform")),
              "reads": int(r.get("reads") or 0), "likes": int(r.get("likes") or 0),
              "shares": int(r.get("shares") or 0), "saves": int(r.get("saves") or 0),
              "comments": int(r.get("comments") or 0), "followers_delta": int(r.get("fd") or 0),
              "published_at": esc((r.get("pub") or "")[:10]), "linked_casepack": esc(r.get("link"))}
             for r in rows]
    casepacks = [esc(r.get("name")) for r in cps]
    data_json = json.dumps({"items": items, "casepacks": casepacks,
                            "prefill": esc(request.query_params.get("casepack") or "")},
                           ensure_ascii=False).replace("</", "<\\/")
    return HTMLResponse(_DASH_FEEDBACK_HTML.replace("__DATA__", data_json))


_SUGGEST_PROMPT = """给下面每条实践记录判定「写作可见性」，只输出一个 JSON 字符串数组，
顺序与记录一一对应，每项取值：
- "internal-note"        只该内部留存（纯操作日志/临时状态/无普遍价值）
- "professional-guide"   有可提炼的方法或经验，适合内部方法库
- "public-story"         有普遍价值、能讲成对读者有用的案例
默认从严：拿不准归 internal-note。只输出数组，共 {n} 个，例：["internal-note","public-story",...]

记录：
{listing}
"""


async def dashboard_suggest_tags(request: Request) -> JSONResponse:
    """POST /dashboard/suggest_tags {names:[...]} — LLM 批量建议可见性(一次调用)。
    返回 {name: visibility}，供待办台预高亮，用户一键确认。"""
    import re
    try:
        b = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    names = [n for n in (b.get("names") or []) if isinstance(n, str)][:15]
    if not names:
        return JSONResponse({"ok": True, "suggest": {}})
    driver = get_status_driver()
    rows, _, _ = await driver.execute_query(
        "MATCH (n:Episodic) WHERE n.name IN $names "
        "RETURN n.name AS name, substring(coalesce(n.content,''),0,300) AS snip", names=names)
    by = {r.get("name"): (r.get("snip") or "").strip().replace("\n", " ") for r in rows}
    ordered = [n for n in names if n in by]
    if not ordered:
        return JSONResponse({"ok": True, "suggest": {}})
    listing = "\n".join(f"{i+1}. {by[n][:200]}" for i, n in enumerate(ordered))
    try:
        out = await _llm_complete(_SUGGEST_PROMPT.format(n=len(ordered), listing=listing), max_tokens=400)
        m = re.search(r"\[.*\]", out, re.S)
        arr = json.loads(m.group(0)) if m else []
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"LLM: {type(exc).__name__}"}, status_code=502)
    valid = {"internal-note", "professional-guide", "public-story"}
    suggest = {}
    for i, n in enumerate(ordered):
        v = arr[i] if i < len(arr) else ""
        suggest[n] = v if v in valid else "internal-note"
    return JSONResponse({"ok": True, "suggest": suggest})


_DASH_INBOX_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>kg-hub 反馈待办</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:880px;margin:1.5rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
a.back{font-size:13px;color:GrayText;text-decoration:none}h1{font-size:20px;font-weight:500;margin:.3rem 0}h2{font-size:15px;font-weight:500;margin:1.2rem 0 .4rem}
.tip{font-size:12px;color:GrayText;margin:.2rem 0 .6rem}
.item{border:1px solid color-mix(in srgb,CanvasText 14%,transparent);border-radius:10px;padding:9px 12px;margin:7px 0}
.sn{font-size:13px}.meta{color:GrayText;font-size:12px;margin-top:2px}
.ctrl{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:8px;font-size:12px}.lb{color:GrayText}
button{font:inherit;font-size:12px;padding:3px 10px;border-radius:7px;border:1px solid color-mix(in srgb,CanvasText 22%,transparent);background:transparent;color:inherit;cursor:pointer}
button.on{background:#378ADD;color:#fff;border-color:#378ADD}button.ver.on{background:#1D9E75;border-color:#1D9E75}
button.sug{border-color:#EF9F27;box-shadow:0 0 0 1px #EF9F27 inset}
.sugtag{font-size:11px;color:#BA7517}
.saved{color:#1D9E75;font-size:12px;opacity:0;transition:opacity .2s}.saved.show{opacity:1}
.go{color:#378ADD;text-decoration:none;font-size:13px}.empty{color:GrayText;font-size:13px;padding:6px 0}
.dtl{white-space:pre-wrap;font-size:12px;font-family:ui-monospace,Menlo,monospace;background:color-mix(in srgb,CanvasText 5%,transparent);border-radius:8px;padding:10px;margin-top:6px;max-height:360px;overflow:auto}.hidden{display:none}</style></head><body>
<a class=back href="/portal">← 报表门户</a><h1>反馈待办</h1>
<div class=tip>系统自动列出「需要你拍板」的事，尽量一键清掉。<b id=aistat>正在让 AI 预判可见性…</b></div>
<h2>① 待分层 · <span id=c1>0</span> 条（AI 已建议，点确认或改）</h2><div id=cls></div>
<h2>② 待补运营数据 · <span id=c2>0</span> 条（标了可公开但没录表现）</h2><div id=fb></div>
<script>var D=__DATA__;var VIS=[["internal-note","内部"],["professional-guide","方法"],["public-story","可公开"]];
function tag(name,patch,cb){fetch('/dashboard/tag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.assign({name:name},patch))}).then(function(r){return r.json();}).then(cb).catch(function(){cb({ok:false});});}
document.getElementById('c1').textContent=D.classify.length;document.getElementById('c2').textContent=D.needfb.length;
document.getElementById('cls').innerHTML=D.classify.length?D.classify.map(function(x,i){
 var vb=VIS.map(function(v){return '<button class=vis data-i="'+i+'" data-v="'+v[0]+'">'+v[1]+'</button>';}).join('');
 return '<div class=item data-i="'+i+'"><div class=sn>'+x.snippet+'<div class=meta>'+x.project+' · '+x.created+'</div></div>'
  +'<div class=ctrl><span class=lb>可见性:</span>'+vb+'<button class=ver data-i="'+i+'">✓已验证</button><button class=exp data-i="'+i+'">详情</button><span class=sugtag data-i="'+i+'"></span><span class=saved data-i="'+i+'">✓已存</span></div><pre class="dtl hidden"></pre></div>';
}).join(''):'<div class=empty>没有待分层的知识 🎉</div>';
document.getElementById('fb').innerHTML=D.needfb.length?D.needfb.map(function(x){return '<div class=item><div class=sn>'+x.snippet+'<div class=meta>'+x.created+'</div></div><div class=ctrl><a class=go href="/dashboard/feedback?casepack='+encodeURIComponent(x.name)+'">录入表现 →</a></div></div>';}).join(''):'<div class=empty>没有待补数据 🎉</div>';
function saved(i){var s=document.querySelector('.saved[data-i="'+i+'"]');if(s){s.classList.add('show');setTimeout(function(){s.classList.remove('show');},1200);}}
document.getElementById('cls').addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;var i=+b.dataset.i;var it=D.classify[i];var item=b.closest('.item');
 if(b.classList.contains('vis')){var nv=(it.visibility===b.dataset.v)?'':b.dataset.v;tag(it.name,{visibility:nv},function(d){if(d.ok){it.visibility=d.visibility;item.querySelectorAll('.vis').forEach(function(x){x.classList.toggle('on',x.dataset.v===d.visibility&&d.visibility!=='');});saved(i);}});}
 else if(b.classList.contains('ver')){tag(it.name,{verified:!it.verified},function(d){if(d.ok){it.verified=d.verified;b.classList.toggle('on',d.verified);saved(i);}});}
 else if(b.classList.contains('exp')){var p=item.querySelector('.dtl');if(p.classList.contains('hidden')){p.textContent=it.detail||'(无内容)';p.classList.remove('hidden');}else{p.classList.add('hidden');}}
});
if(D.classify.length){fetch('/dashboard/suggest_tags',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({names:D.classify.map(function(x){return x.name;})})}).then(function(r){return r.json();}).then(function(d){var st=document.getElementById('aistat');if(!d.ok){st.textContent='AI 建议不可用，手动分类即可。';return;}st.textContent='AI 已建议（橙框=建议值），点确认或改。';D.classify.forEach(function(x,i){var v=d.suggest[x.name];if(!v)return;x.suggested=v;var lbl=VIS.filter(function(z){return z[0]===v;})[0];var item=document.querySelector('.item[data-i="'+i+'"]');if(!item)return;item.querySelectorAll('.vis').forEach(function(bt){bt.classList.toggle('sug',bt.dataset.v===v);});var tg=item.querySelector('.sugtag');if(tg&&lbl)tg.textContent='AI建议:'+lbl[1];});}).catch(function(){document.getElementById('aistat').textContent='AI 建议请求失败，手动分类即可。';});}else{document.getElementById('aistat').textContent='';}
</script></body></html>"""


async def dashboard_inbox(request: Request) -> HTMLResponse:
    import re as _re
    driver = get_status_driver()

    def esc(t):
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    async def one(cy, **p):
        rows, _, _ = await driver.execute_query(cy, **p)
        return rows

    try:
        untagged = await one(
            "MATCH (n:Episodic) WHERE NOT (n.name STARTS WITH 'kg-hub-canonical') AND n.visibility IS NULL "
            "AND (n.source_description CONTAINS 'type=feature' OR n.source_description CONTAINS 'type=bugfix' "
            "OR n.source_description CONTAINS 'type=decision' OR n.source_description CONTAINS 'type=refactor') "
            "RETURN n.name AS name, substring(coalesce(n.content,''),0,4000) AS detail, "
            "n.source_description AS source, n.created_at AS created "
            "ORDER BY n.created_at DESC LIMIT 15")
        pub = await one(
            "MATCH (n:Episodic) WHERE n.visibility = 'public-story' "
            "RETURN n.name AS name, substring(coalesce(n.content,''),0,160) AS snip, n.created_at AS created "
            "ORDER BY n.created_at DESC LIMIT 50")
        fbrows = await one("MATCH (f:ArticleFeedback) WHERE coalesce(f.linked_casepack,'') <> '' "
                           "RETURN DISTINCT f.linked_casepack AS l")
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p>待办取数失败: {exc}</p>", status_code=503)

    linked = {r.get("l") for r in fbrows}

    def view(r, full_key="detail"):
        src = r.get("source") or ""
        mt = _re.search(r"type=(\S+)", src)
        mp = _re.search(r"project=(\S+)", src)
        one_line = (r.get(full_key) or r.get("snip") or "").strip().replace("\n", " ")
        return {"name": r.get("name") or "",
                "type": esc(mt.group(1)) if mt else "obs",
                "project": esc(mp.group(1)) if mp else "—",
                "snippet": esc(one_line[:120]) + ("…" if len(one_line) > 120 else ""),
                "created": (r.get("created") or "")[:16],
                "visibility": r.get("visibility") or "", "verified": False,
                "detail": (r.get("detail") or "")}
    classify = [view(r) for r in untagged]
    needfb = []
    for r in pub:
        if r.get("name") not in linked:
            sn = (r.get("snip") or "").strip().replace("\n", " ")
            needfb.append({"name": r.get("name") or "",
                           "snippet": esc(sn[:120]) + ("…" if len(sn) > 120 else ""),
                           "created": (r.get("created") or "")[:16]})
    data_json = json.dumps({"classify": classify, "needfb": needfb}, ensure_ascii=False).replace("</", "<\\/")
    return HTMLResponse(_DASH_INBOX_HTML.replace("__DATA__", data_json))


app = Starlette(
    debug=False,
    routes=[
        Route("/", portal, methods=["GET"]),
        Route("/portal", portal, methods=["GET"]),
        Route("/portal_manifest", portal_manifest, methods=["GET"]),
        Route("/dashboard/capsules", dashboard_capsules, methods=["GET"]),
        Route("/dashboard/usage", dashboard_usage, methods=["GET"]),
        Route("/dashboard/knowledge", dashboard_knowledge, methods=["GET"]),
        Route("/dashboard/utilization", dashboard_utilization, methods=["GET"]),
        Route("/dashboard/tools", dashboard_tools, methods=["GET"]),
        Route("/dashboard/curate", dashboard_curate, methods=["GET"]),
        Route("/dashboard/tag", dashboard_tag, methods=["POST"]),
        Route("/dashboard/casepack", dashboard_casepack, methods=["POST"]),
        Route("/dashboard/feedback", dashboard_feedback, methods=["GET"]),
        Route("/dashboard/feedback", feedback_submit, methods=["POST"]),
        Route("/dashboard/inbox", dashboard_inbox, methods=["GET"]),
        Route("/dashboard/suggest_tags", dashboard_suggest_tags, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
        Route("/api/ingest", ingest, methods=["POST"]),
        Route("/api/ingest/status", ingest_status, methods=["GET"]),
        Route("/api/queue_stats", queue_stats, methods=["GET"]),
        Route("/api/search", search, methods=["GET"]),
        Route("/api/search_semantic", search_semantic, methods=["GET"]),
        Route("/api/canonical_context", canonical_context, methods=["GET"]),
        Route("/api/usage_ranking", usage_ranking, methods=["GET"]),
        Route("/api/stats", stats, methods=["GET"]),
        Route("/api/episode_search", episode_search, methods=["GET"]),
        Route("/api/node_neighbors", node_neighbors, methods=["GET"]),
        Route("/api/path_between", path_between, methods=["GET"]),
    ],
    middleware=[Middleware(BearerAuthMiddleware)],
)


if __name__ == "__main__":
    # Boot-race mitigation: server lazy-inits graphiti on first request, but if
    # FalkorDB isn't up at startup, the first incoming request would hit
    # ConnectionError. Block startup until FalkorDB is reachable (up to 90s).
    # If FalkorDB never comes up, KeepAlive will restart us after ThrottleInterval=30s.
    if not wait_for_falkordb(timeout_seconds=90.0):
        print("[fatal] FalkorDB not ready after 90s — exiting; launchd KeepAlive will retry")
        sys.exit(2)

    import uvicorn  # noqa: E402

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
