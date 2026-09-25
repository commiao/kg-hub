"""Durable evidence for one exact kg-hub model request.

This journal is deliberately not a retry queue. An interrupted request cannot
leave this module as a second paid call until a separate operator workflow has
checked its business result and explicitly authorized recovery.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import hashlib
import uuid
from datetime import datetime, timezone
from urllib.request import Request, urlopen


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NeedsReconciliation(RuntimeError):
    """A model request has no usable local result; automatic replay is unsafe."""

    def __init__(self, step_id: str, phase: str, provider_call_started: bool | None):
        self.step_id = step_id
        self.phase = phase
        self.provider_call_started = provider_call_started
        super().__init__(f"model step {step_id[:12]} requires reconciliation ({phase})")


class ModelAttemptJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS model_attempts (
                idempotency_key TEXT PRIMARY KEY,
                business_key TEXT NOT NULL,
                source_description TEXT NOT NULL,
                source_obs_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                phase TEXT NOT NULL,
                provider_call_started INTEGER,
                result_json TEXT,
                gateway_identity TEXT,
                gateway_http_status INTEGER,
                http_started_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            # Existing NAS journals predate the local HTTP-start marker.
            columns = {row[1] for row in db.execute("PRAGMA table_info(model_attempts)")}
            if "http_started_at" not in columns:
                db.execute("ALTER TABLE model_attempts ADD COLUMN http_started_at TEXT")
            db.execute("""CREATE INDEX IF NOT EXISTS model_attempts_task
                ON model_attempts(source_description, source_obs_id)""")
            db.execute("""CREATE TABLE IF NOT EXISTS model_retry_grants (
                grant_id TEXT PRIMARY KEY,
                source_description TEXT NOT NULL,
                source_obs_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                consumed_at TEXT
            )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS model_retry_grant_open
                ON model_retry_grants(source_description, source_obs_id, step_id)
                WHERE state = 'granted'""")
            db.execute("""CREATE TABLE IF NOT EXISTS episode_contexts (
                source_description TEXT NOT NULL,
                source_obs_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                previous_episode_uuids_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(source_description, source_obs_id, operation_id)
            )""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def prepare(self, *, key: str, business_key: str, source_description: str,
                source_obs_id: str, step_id: str, request_digest: str) -> str | None:
        """Commit exact identity before HTTP; return a prior complete response."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT phase, provider_call_started, step_id, request_digest, result_json "
                "FROM model_attempts WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row and row[2:4] != (step_id, request_digest):
                raise RuntimeError("model idempotency key reused for different input")
            # A successful human retry has a fresh HTTP key. Future process
            # restarts still reach the original deterministic key; replay that
            # exact step's saved answer rather than trying the old failed key.
            completed = db.execute("""SELECT result_json FROM model_attempts
                WHERE source_description = ? AND source_obs_id = ?
                  AND step_id = ? AND request_digest = ?
                  AND phase = 'completed' AND result_json IS NOT NULL
                ORDER BY updated_at DESC LIMIT 1""",
                (source_description, source_obs_id, step_id, request_digest)).fetchone()
            if completed:
                return completed[0]
            if row:
                if row[0] == "completed" and row[4]:
                    return row[4]
                if row[0] == "preflight" and row[1] == 0:
                    # Gateway proved no provider call started. Reusing this
                    # identity later remains the first actual attempt.
                    db.execute("""UPDATE model_attempts SET phase = 'prepared',
                        provider_call_started = NULL, http_started_at = NULL,
                        updated_at = ?
                        WHERE idempotency_key = ?""", (_now(), key))
                    return None
                raise NeedsReconciliation(step_id, row[0],
                                          None if row[1] is None else bool(row[1]))
            now = _now()
            db.execute("""INSERT INTO model_attempts
                (idempotency_key, business_key, source_description, source_obs_id,
                 step_id, request_digest, phase, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)""",
                 (key, business_key, source_description, source_obs_id,
                 step_id, request_digest, now, now))
        return None

    def complete(self, key: str, result_json: str) -> None:
        """The client received a model response; business completion is separate."""
        with self._connect() as db:
            changed = db.execute("""UPDATE model_attempts SET phase = 'completed',
                provider_call_started = 1, result_json = ?, updated_at = ?
                WHERE idempotency_key = ?""", (result_json, _now(), key))
            if changed.rowcount != 1:
                raise RuntimeError("model attempt intent vanished before response persistence")

    def start_http(self, key: str) -> None:
        """Durably mark the SDK HTTP-call boundary before invoking the SDK."""
        with self._connect() as db:
            now = _now()
            changed = db.execute("""UPDATE model_attempts
                SET phase = 'http_started', http_started_at = ?, updated_at = ?
                WHERE idempotency_key = ? AND phase = 'prepared'
                  AND http_started_at IS NULL""", (now, now, key))
            if changed.rowcount != 1:
                raise RuntimeError("model HTTP start marker missing or duplicated")

    def update_gateway_status(self, key: str, status: dict) -> NeedsReconciliation:
        started = status.get("provider_call_started")
        if started not in (True, False, None):
            started = None
        phase = str(status.get("phase") or "unknown")
        if phase not in {"absent", "preflight", "admitted", "completed", "failed", "unknown"}:
            phase = "unknown"
            started = None
        identity = status.get("identity")
        http_status = status.get("http_status")
        with self._connect() as db:
            db.execute("""UPDATE model_attempts SET phase = ?,
                provider_call_started = ?, gateway_identity = ?,
                gateway_http_status = ?, updated_at = ?
                WHERE idempotency_key = ?""",
                (phase, None if started is None else int(started),
                 identity if isinstance(identity, str) else None,
                 http_status if isinstance(http_status, int) else None,
                 _now(), key))
            row = db.execute("SELECT step_id FROM model_attempts WHERE idempotency_key = ?",
                             (key,)).fetchone()
        return NeedsReconciliation(row[0] if row else "unknown", phase, started)

    def find_task(self, source_description: str, source_obs_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT idempotency_key, business_key, step_id,
                request_digest, phase, provider_call_started, gateway_identity,
                gateway_http_status, result_json, created_at, updated_at,
                http_started_at
                FROM model_attempts WHERE source_description = ? AND source_obs_id = ?
                ORDER BY created_at, idempotency_key""",
                (source_description, source_obs_id)).fetchall()
        fields = ("idempotency_key", "business_key", "step_id", "request_digest",
                  "phase", "provider_call_started", "gateway_identity",
                  "gateway_http_status", "result_json", "created_at", "updated_at",
                  "http_started_at")
        return [dict(zip(fields, row)) for row in rows]

    def read_episode_context(self, source_description: str, source_obs_id: str,
                             operation_id: str, input_digest: str) -> list[str] | None:
        with self._connect() as db:
            row = db.execute("""SELECT input_digest, previous_episode_uuids_json
                FROM episode_contexts WHERE source_description = ? AND source_obs_id = ?
                  AND operation_id = ?""",
                (source_description, source_obs_id, operation_id)).fetchone()
        if row is None:
            return None
        if row[0] != input_digest:
            raise RuntimeError("episode input changed since model operation began")
        value = json.loads(row[1])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise RuntimeError("episode context checkpoint is invalid")
        return value

    def save_episode_context(self, source_description: str, source_obs_id: str,
                             operation_id: str, input_digest: str,
                             previous_episode_uuids: list[str]) -> list[str]:
        if not all(isinstance(v, str) for v in previous_episode_uuids):
            raise RuntimeError("episode context contains invalid UUIDs")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO episode_contexts
                (source_description, source_obs_id, operation_id, input_digest,
                 previous_episode_uuids_json, created_at)
                 VALUES (?, ?, ?, ?, ?, ?)""",
                (source_description, source_obs_id, operation_id, input_digest,
                 json.dumps(previous_episode_uuids), _now()))
        saved = self.read_episode_context(source_description, source_obs_id,
                                          operation_id, input_digest)
        if saved is None:
            raise RuntimeError("episode context checkpoint vanished")
        return saved

    def read_episode_context(self, source_description: str, source_obs_id: str,
                             operation_id: str, input_digest: str) -> list[str] | None:
        with self._connect() as db:
            row = db.execute("""SELECT input_digest, previous_episode_uuids_json
                FROM episode_contexts WHERE source_description = ? AND source_obs_id = ?
                  AND operation_id = ?""",
                (source_description, source_obs_id, operation_id)).fetchone()
        if row is None:
            return None
        if row[0] != input_digest:
            raise RuntimeError("episode input changed since model operation began")
        value = json.loads(row[1])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise RuntimeError("episode context checkpoint is invalid")
        return value

    def save_episode_context(self, source_description: str, source_obs_id: str,
                             operation_id: str, input_digest: str,
                             previous_episode_uuids: list[str]) -> list[str]:
        if not all(isinstance(v, str) for v in previous_episode_uuids):
            raise RuntimeError("episode context contains invalid UUIDs")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO episode_contexts
                (source_description, source_obs_id, operation_id, input_digest,
                 previous_episode_uuids_json, created_at)
                 VALUES (?, ?, ?, ?, ?, ?)""",
                (source_description, source_obs_id, operation_id, input_digest,
                 json.dumps(previous_episode_uuids), _now()))
        saved = self.read_episode_context(source_description, source_obs_id,
                                          operation_id, input_digest)
        if saved is None:
            raise RuntimeError("episode context checkpoint vanished")
        return saved

    def authorize_retry(self, source_description: str, source_obs_id: str,
                        step_id: str, request_digest: str, *,
                        deadline_seconds: float) -> str:
        """Record one human decision; this never calls a model or queues work."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            records = db.execute("""SELECT idempotency_key, business_key, step_id,
                request_digest, phase, provider_call_started, gateway_identity,
                gateway_http_status, result_json, created_at, updated_at,
                http_started_at
                FROM model_attempts WHERE source_description = ? AND source_obs_id = ?
                  AND step_id = ? ORDER BY created_at, idempotency_key""",
                (source_description, source_obs_id, step_id)).fetchall()
            fields = ("idempotency_key", "business_key", "step_id", "request_digest",
                      "phase", "provider_call_started", "gateway_identity",
                      "gateway_http_status", "result_json", "created_at", "updated_at",
                      "http_started_at")
            rows = [dict(zip(fields, record)) for record in records]
            if not rows or any(row["request_digest"] != request_digest for row in rows):
                raise RuntimeError("model step input changed")
            summary = summarize_attempts(rows, deadline_seconds=deadline_seconds)
            failed = summary["failed_calls_by_step"].get(step_id, 0)
            if (failed < 1 or failed >= 3 or summary["in_flight"]
                    or summary["unknown_without_http_evidence"]
                    or any(row["result_json"] for row in rows)):
                raise RuntimeError("model step is not eligible for manual retry")
            grant_id = str(uuid.uuid4())
            db.execute("""INSERT INTO model_retry_grants
                (grant_id, source_description, source_obs_id, step_id,
                 request_digest, state, created_at)
                 VALUES (?, ?, ?, ?, ?, 'granted', ?)""",
                (grant_id, source_description, source_obs_id, step_id,
                 request_digest, _now()))
        return grant_id

    def claim_retry(self, grant_id: str, *, source_description: str,
                    source_obs_id: str, step_id: str, request_digest: str,
                    business_key: str, base_key: str, deadline_seconds: float) -> str:
        """Consume a grant and reserve a fresh exact-call identity atomically."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            grant = db.execute("""SELECT source_description, source_obs_id, step_id,
                request_digest, state FROM model_retry_grants WHERE grant_id = ?""",
                (grant_id,)).fetchone()
            if grant != (source_description, source_obs_id, step_id,
                         request_digest, "granted"):
                raise RuntimeError("manual retry grant absent, consumed, or mismatched")
            records = db.execute("""SELECT phase, provider_call_started, result_json,
                created_at, request_digest, http_started_at FROM model_attempts
                WHERE source_description = ? AND source_obs_id = ? AND step_id = ?""",
                (source_description, source_obs_id, step_id)).fetchall()
            rows = [{"step_id": step_id, "phase": r[0], "provider_call_started": r[1],
                     "result_json": r[2], "created_at": r[3],
                     "http_started_at": r[5]} for r in records]
            summary = summarize_attempts(rows, deadline_seconds=deadline_seconds)
            failed = summary["failed_calls_by_step"].get(step_id, 0)
            if (failed < 1 or failed >= 3 or summary["in_flight"]
                    or summary["unknown_without_http_evidence"]
                    or any(r[2] or r[4] != request_digest for r in records)):
                raise RuntimeError("model retry no longer safe")
            ordinal = failed + 1
            key = "kg1-" + hashlib.sha256(
                f"{base_key}:{ordinal}:{grant_id}".encode("utf-8")).hexdigest()
            now = _now()
            db.execute("""INSERT INTO model_attempts
                (idempotency_key, business_key, source_description, source_obs_id,
                 step_id, request_digest, phase, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)""",
                (key, business_key, source_description, source_obs_id,
                 step_id, request_digest, now, now))
            changed = db.execute("""UPDATE model_retry_grants SET state = 'consumed',
                consumed_at = ? WHERE grant_id = ? AND state = 'granted'""",
                (now, grant_id))
            if changed.rowcount != 1:
                raise RuntimeError("manual retry grant raced")
        return key


def query_gateway_attempt_status(base_url: str, token: str, business_key: str,
                                 idempotency_key: str, *, timeout: float = 5) -> dict:
    body = json.dumps({"business_key": business_key,
                       "idempotency_key": idempotency_key}).encode("utf-8")
    req = Request(base_url.rstrip("/") + "/v1/attempt-status", data=body,
                  headers={"Authorization": f"Bearer {token}",
                           "Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as response:
        value = json.load(response)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise RuntimeError("invalid gateway attempt-status response")
    return value


def journal_from_backup_env() -> ModelAttemptJournal | None:
    path = os.environ.get("KG_HUB_INGEST_BACKUP_PATH", "").strip()
    if not path:
        return None
    return ModelAttemptJournal(Path(path).with_name("model-attempts.sqlite3"))


def summarize_attempts(rows: list[dict], *, deadline_seconds: float,
                       now: datetime | None = None) -> dict:
    """Count each locally started model HTTP call once after its deadline.

    A gateway HTTP success is only a model result. It does not make the ingest
    business task successful until the graph result is independently verified.
    """
    now = now or datetime.now(timezone.utc)
    failed_by_step: dict[str, int] = {}
    in_flight = False
    admission_unknown = False
    unknown_without_http_evidence = False
    cached_steps: set[str] = set()
    for row in rows:
        step = row["step_id"]
        phase = row["phase"]
        started = row["provider_call_started"]
        if row.get("result_json"):
            # The exact model response is durable even if a later gateway
            # status refresh changed the phase. Business graph completion is
            # checked separately; this paid model step itself did not fail.
            cached_steps.add(step)
            continue
        if started == 0:
            # Gateway proved pre-provider refusal. It is not a model call.
            continue
        local_http_start = row.get("http_started_at")
        if started is None:
            admission_unknown = True
        if started is None and not local_http_start:
            # Old or interrupted prepared intent: no durable proof the SDK
            # HTTP boundary was crossed. Do not count it as a real call.
            unknown_without_http_evidence = True
            continue
        try:
            created = datetime.fromisoformat(
                (local_http_start or row["created_at"]).replace("Z", "+00:00"))
            elapsed = (now - created).total_seconds()
        except (TypeError, ValueError):
            elapsed = deadline_seconds
        if phase == "failed" or elapsed >= deadline_seconds:
            failed_by_step[step] = failed_by_step.get(step, 0) + 1
        else:
            in_flight = True
    return {
        "failed_calls_by_step": failed_by_step,
        "max_failed_calls": max(failed_by_step.values(), default=0),
        "in_flight": in_flight,
        "admission_unknown": admission_unknown,
        "unknown_without_http_evidence": unknown_without_http_evidence,
        "cached_model_steps": len(cached_steps),
        "attempts_recorded": len(rows),
    }
