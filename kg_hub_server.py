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
