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
import threading
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
                stage TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(model_attempts)")}
            if "queue_owned" not in columns:
                db.execute("ALTER TABLE model_attempts ADD COLUMN queue_owned INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE TABLE IF NOT EXISTS queue_business_receipts (
                idempotency_key TEXT PRIMARY KEY, business_key TEXT NOT NULL,
                receipt TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0)""")
            # Existing NAS journals predate the local HTTP-start marker.
            columns = {row[1] for row in db.execute("PRAGMA table_info(model_attempts)")}
            if "http_started_at" not in columns:
                db.execute("ALTER TABLE model_attempts ADD COLUMN http_started_at TEXT")
            if "stage" not in columns:
                db.execute("ALTER TABLE model_attempts ADD COLUMN stage TEXT")
            db.execute("""CREATE INDEX IF NOT EXISTS model_attempts_task
                ON model_attempts(source_description, source_obs_id)""")
            db.execute("""CREATE TABLE IF NOT EXISTS gateway_step_mappings (
                task_id TEXT NOT NULL,
                wire_step_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                local_step_id TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                stage TEXT,
                body_digest TEXT NOT NULL,
                mailbox_step_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, wire_step_id, idempotency_key)
            )""")
            wire_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(gateway_step_mappings)")}
            if "body_digest" not in wire_columns:
                db.execute("ALTER TABLE gateway_step_mappings "
                           "ADD COLUMN body_digest TEXT NOT NULL DEFAULT ''")
            if "mailbox_step_id" not in wire_columns:
                db.execute("ALTER TABLE gateway_step_mappings "
                           "ADD COLUMN mailbox_step_id TEXT NOT NULL DEFAULT ''")
            db.execute("""CREATE TABLE IF NOT EXISTS model_retry_grants (
                grant_id TEXT PRIMARY KEY,
                source_description TEXT NOT NULL,
                source_obs_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                stage TEXT,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                consumed_at TEXT
            )""")
            grant_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(model_retry_grants)")}
            if "stage" not in grant_columns:
                db.execute("ALTER TABLE model_retry_grants ADD COLUMN stage TEXT")
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
            db.execute("""CREATE TABLE IF NOT EXISTS task_executions (
                task_id TEXT NOT NULL,
                execution_ordinal INTEGER NOT NULL,
                execution_id TEXT NOT NULL,
                source_description TEXT NOT NULL,
                source_obs_id TEXT NOT NULL,
                manual_command_id TEXT,
                state TEXT NOT NULL,
                reason TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                PRIMARY KEY(task_id, execution_ordinal),
                UNIQUE(task_id, execution_id)
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
                source_obs_id: str, step_id: str, request_digest: str,
                stage: str | None = None, queue_owned: bool = False) -> str | None:
        """Commit exact identity before HTTP; return a prior complete response."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT phase, provider_call_started, step_id, request_digest, result_json, stage, queue_owned "
                "FROM model_attempts WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row and row[2:4] != (step_id, request_digest):
                raise RuntimeError("model idempotency key reused for different input")
            if row and row[5] not in (None, stage):
                raise RuntimeError("model stage changed for an existing exact request")
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
                if queue_owned and row[6]:
                    # Resume only this exact durable queue identity. This does
                    # not authorize a new paid attempt or legacy SDK replay.
                    return row[4] if row[0] == 'completed' else None
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
                 step_id, request_digest, phase, stage, created_at, updated_at, queue_owned)
                 VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?)""",
                 (key, business_key, source_description, source_obs_id,
                 step_id, request_digest, stage, now, now, int(queue_owned)))
        return None

    def queue_business_receipts(self, sd: str, sid: str, reference: str) -> None:
        """Call only after the original graph receipt has been verified."""
        with self._connect() as db:
            receipt = json.dumps({'state': 'completed', 'reference': reference}, sort_keys=True)
            db.execute("""INSERT OR IGNORE INTO queue_business_receipts
                (idempotency_key,business_key,receipt)
                SELECT idempotency_key,business_key,? FROM model_attempts
                WHERE source_description=? AND source_obs_id=? AND queue_owned=1
                AND phase='completed' AND result_json IS NOT NULL""", (receipt, sd, sid))

    def pending_queue_receipts(self):
        with self._connect() as db:
            return [{'idempotency_key': key, 'business_key': business, 'receipt': json.loads(receipt)}
                    for key, business, receipt in db.execute(
                        'SELECT idempotency_key,business_key,receipt FROM queue_business_receipts '
                        'WHERE acknowledged=0 LIMIT 50')]

    def acknowledge_queue_receipt(self, key):
        with self._connect() as db:
            db.execute('UPDATE queue_business_receipts SET acknowledged=1 WHERE idempotency_key=?', (key,))

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
                http_started_at, stage
                FROM model_attempts WHERE source_description = ? AND source_obs_id = ?
                ORDER BY created_at, idempotency_key""",
                (source_description, source_obs_id)).fetchall()
        fields = ("idempotency_key", "business_key", "step_id", "request_digest",
                  "phase", "provider_call_started", "gateway_identity",
                  "gateway_http_status", "result_json", "created_at", "updated_at",
                  "http_started_at", "stage")
        return [dict(zip(fields, row)) for row in rows]

    def begin_task_execution(self, source_description: str, source_obs_id: str,
                             execution_id: str, *,
                             manual_command_id: str | None = None) -> dict:
        """Durably claim one whole original-worker execution, idempotently."""
        from utils.reconciliation_mailbox import task_uuid

        if not execution_id:
            raise RuntimeError("task execution identity is missing")
        task_id = task_uuid(source_description, source_obs_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("""SELECT execution_ordinal, manual_command_id,
                state, started_at, finished_at FROM task_executions
                WHERE task_id=? AND execution_id=?""",
                (task_id, execution_id)).fetchone()
            if prior:
                return {"task_id": task_id, "execution_id": execution_id,
                        "execution_ordinal": prior[0], "manual_command_id": prior[1],
                        "state": prior[2], "started_at": prior[3],
                        "finished_at": prior[4], "created": False}
            rows = db.execute("""SELECT execution_ordinal, state
                FROM task_executions WHERE task_id=? ORDER BY execution_ordinal""",
                (task_id,)).fetchall()
            failed = sum(state == "failed" for _, state in rows)
            if failed >= 3:
                raise RuntimeError("task execution limit exhausted")
            if any(state in {"running", "uncertain"} for _, state in rows):
                raise RuntimeError("prior task execution is not terminal")
            if manual_command_id and (not rows or rows[-1][1] != "failed"):
                raise RuntimeError("manual execution requires a failed prior execution")
            if not manual_command_id and rows:
                raise RuntimeError("initial task execution already exists")
            ordinal = (rows[-1][0] + 1) if rows else 1
            started = _now()
            db.execute("""INSERT INTO task_executions
                (task_id, execution_ordinal, execution_id, source_description,
                 source_obs_id, manual_command_id, state, started_at)
                VALUES (?, ?, ?, ?, ?, ?, 'running', ?)""",
                (task_id, ordinal, execution_id, source_description,
                 source_obs_id, manual_command_id, started))
        return {"task_id": task_id, "execution_id": execution_id,
                "execution_ordinal": ordinal, "manual_command_id": manual_command_id,
                "state": "running", "started_at": started,
                "finished_at": None, "created": True}

    def finish_task_execution(self, source_description: str, source_obs_id: str,
                              execution_id: str, *, state: str,
                              reason: str | None = None) -> None:
        """Settle one whole worker run; sibling HTTP failures cannot double count."""
        from utils.reconciliation_mailbox import task_uuid

        if state not in {"succeeded", "failed", "uncertain", "unrecoverable"}:
            raise ValueError("invalid task execution state")
        task_id = task_uuid(source_description, source_obs_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT state FROM task_executions
                WHERE task_id=? AND execution_id=?""",
                (task_id, execution_id)).fetchone()
            if row is None:
                raise RuntimeError("task execution claim is missing")
            prior = row[0]
            if prior == state:
                return
            if prior not in {"running", "uncertain"} and not (
                    prior == "failed" and state == "succeeded"):
                raise RuntimeError("task execution is already terminal")
            db.execute("""UPDATE task_executions SET state=?, reason=?, finished_at=?
                WHERE task_id=? AND execution_id=?""",
                (state, reason, _now(), task_id, execution_id))

    def task_execution_summary(self, source_description: str,
                               source_obs_id: str) -> dict:
        from utils.reconciliation_mailbox import task_uuid

        task_id = task_uuid(source_description, source_obs_id)
        with self._connect() as db:
            rows = db.execute("""SELECT execution_ordinal, execution_id,
                manual_command_id, state, reason, started_at, finished_at
                FROM task_executions WHERE task_id=? ORDER BY execution_ordinal""",
                (task_id,)).fetchall()
        executions = [dict(zip(("execution_ordinal", "execution_id",
                                "manual_command_id", "state", "reason",
                                "started_at", "finished_at"), row))
                      for row in rows]
        return {"task_id": task_id, "execution_count": len(executions),
                "failed_attempts": min(3, sum(
                    row["state"] == "failed" for row in executions)),
                "active": any(row["state"] in {"running", "uncertain"}
                              for row in executions),
                "executions": executions}

    def record_gateway_step(self, source_description: str, source_obs_id: str,
                            idempotency_key: str, wire_step_id: str,
                            body_digest: str, *,
                            mailbox_step_id: str | None = None) -> None:
        """Persist current wire and stable mailbox identities side by side."""
        from utils.reconciliation_mailbox import task_uuid

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT source_description, source_obs_id,
                step_id, request_digest, stage FROM model_attempts
                WHERE idempotency_key=?""", (idempotency_key,)).fetchone()
            if row is None or row[:2] != (source_description, source_obs_id):
                raise RuntimeError("gateway wire identity has no exact local attempt")
            task_id = task_uuid(source_description, source_obs_id)
            mailbox_step_id = mailbox_step_id or wire_step_id
            mapping = (task_id, wire_step_id, idempotency_key,
                       row[2], row[3], row[4], body_digest, mailbox_step_id)
            saved = db.execute("""SELECT task_id, wire_step_id, idempotency_key,
                local_step_id, request_digest, stage, body_digest, mailbox_step_id
                FROM gateway_step_mappings
                WHERE task_id=? AND wire_step_id=? AND idempotency_key=?""",
                mapping[:3]).fetchone()
            if saved and saved != mapping:
                raise RuntimeError("gateway wire identity mapping changed")
            db.execute("""INSERT OR IGNORE INTO gateway_step_mappings
                (task_id, wire_step_id, idempotency_key, local_step_id,
                 request_digest, stage, body_digest, mailbox_step_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*mapping, _now()))

    def gateway_step_for_attempt(self, idempotency_key: str) -> str | None:
        with self._connect() as db:
            rows = db.execute("""SELECT DISTINCT mailbox_step_id
                FROM gateway_step_mappings WHERE idempotency_key=?""",
                (idempotency_key,)).fetchall()
        values = {row[0] for row in rows}
        if len(values) > 1:
            raise RuntimeError("one exact local attempt mapped to multiple gateway steps")
        return next(iter(values)) if values else None

    def resolve_gateway_step(self, source_description: str, source_obs_id: str,
                             wire_step_id: str) -> dict:
        """Resolve one dashboard/wire id to one exact local request identity."""
        from utils.reconciliation_mailbox import task_uuid

        with self._connect() as db:
            rows = db.execute("""SELECT DISTINCT local_step_id, request_digest, stage
                FROM gateway_step_mappings WHERE task_id=? AND mailbox_step_id=?""",
                (task_uuid(source_description, source_obs_id), wire_step_id)).fetchall()
        identities = {tuple(row) for row in rows}
        if len(identities) != 1:
            raise RuntimeError("gateway step mapping is missing or ambiguous")
        local_step, request_digest, stage = next(iter(identities))
        return {"local_step_id": local_step,
                "request_digest": request_digest, "stage": stage}

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
                        deadline_seconds: float,
                        expected_stage: str | None = None) -> str:
        """Record one human decision; this never calls a model or queues work."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            records = db.execute("""SELECT idempotency_key, business_key, step_id,
                request_digest, phase, provider_call_started, gateway_identity,
                gateway_http_status, result_json, created_at, updated_at,
                http_started_at, stage
                FROM model_attempts WHERE source_description = ? AND source_obs_id = ?
                ORDER BY created_at, idempotency_key""",
                (source_description, source_obs_id)).fetchall()
            fields = ("idempotency_key", "business_key", "step_id", "request_digest",
                      "phase", "provider_call_started", "gateway_identity",
                      "gateway_http_status", "result_json", "created_at", "updated_at",
                      "http_started_at", "stage")
            all_rows = [dict(zip(fields, record)) for record in records]
            rows = [row for row in all_rows if row["step_id"] == step_id]
            if not rows or any(row["request_digest"] != request_digest for row in rows):
                raise RuntimeError("model step input changed")
            if expected_stage is not None and any(
                    row["stage"] != expected_stage for row in rows):
                raise RuntimeError("model step stage is not safely checkpointed")
            summary = summarize_attempts(
                all_rows, deadline_seconds=deadline_seconds,
                missing_receipt_step_id=step_id)
            failed_for_step = summary["failed_calls_by_step"].get(step_id, 0)
            from utils.reconciliation_mailbox import task_uuid
            executions = db.execute("""SELECT state FROM task_executions
                WHERE task_id=? ORDER BY execution_ordinal""",
                (task_uuid(source_description, source_obs_id),)).fetchall()
            failed_executions = sum(row[0] == "failed" for row in executions)
            if (not executions or executions[-1][0] != "failed"
                    or failed_executions >= 3):
                raise RuntimeError("task execution is not eligible for manual retry")
            if (failed_for_step < 1 or failed_for_step >= 3 or summary["in_flight"]
                    or summary["unknown_without_http_evidence"]
                    or any(row["result_json"] for row in rows)):
                raise RuntimeError("model step is not eligible for manual retry")
            # The mailbox may lose its worker after the durable grant is
            # written but before its outbox row is committed. A redelivered
            # human command must recover that exact grant rather than strand
            # the task behind the one-open-grant constraint.
            existing = db.execute("""SELECT grant_id FROM model_retry_grants
                WHERE source_description=? AND source_obs_id=? AND step_id=?
                  AND request_digest=? AND stage IS ? AND state='granted'
                ORDER BY created_at DESC LIMIT 1""",
                (source_description, source_obs_id, step_id,
                 request_digest, expected_stage)).fetchone()
            if existing:
                return existing[0]
            grant_id = str(uuid.uuid4())
            db.execute("""INSERT INTO model_retry_grants
                (grant_id, source_description, source_obs_id, step_id,
                 request_digest, stage, state, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, 'granted', ?)""",
                (grant_id, source_description, source_obs_id, step_id,
                 request_digest, expected_stage, _now()))
        return grant_id

    def revoke_retry(self, grant_id: str) -> None:
        """Revoke a human grant when its graph CAS could not be acquired."""
        with self._connect() as db:
            db.execute("""UPDATE model_retry_grants SET state='revoked'
                WHERE grant_id=? AND state='granted'""", (grant_id,))

    def claim_retry(self, grant_id: str, *, source_description: str,
                    source_obs_id: str, step_id: str, request_digest: str,
                    business_key: str, base_key: str, deadline_seconds: float,
                    stage: str | None = None, execution_id: str | None = None,
                    queue_owned: bool = False) -> str:
        """Consume a grant and reserve a fresh exact-call identity atomically."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            grant = db.execute("""SELECT source_description, source_obs_id, step_id,
                request_digest, state, stage FROM model_retry_grants WHERE grant_id = ?""",
                (grant_id,)).fetchone()
            if grant != (source_description, source_obs_id, step_id,
                         request_digest, "granted", stage):
                raise RuntimeError("manual retry grant absent, consumed, or mismatched")
            records = db.execute("""SELECT idempotency_key, step_id, phase, provider_call_started,
                result_json, created_at, request_digest, http_started_at
                FROM model_attempts WHERE source_description = ? AND source_obs_id = ?""",
                (source_description, source_obs_id)).fetchall()
            rows = [{"idempotency_key": r[0], "step_id": r[1],
                     "phase": r[2], "provider_call_started": r[3],
                     "result_json": r[4], "created_at": r[5],
                     "request_digest": r[6], "http_started_at": r[7]}
                    for r in records]
            step_rows = [row for row in rows if row["step_id"] == step_id]
            summary = summarize_attempts(
                rows, deadline_seconds=deadline_seconds,
                missing_receipt_step_id=step_id)
            failed_for_step = summary["failed_calls_by_step"].get(step_id, 0)
            from utils.reconciliation_mailbox import task_uuid
            executions = db.execute("""SELECT state, execution_id, manual_command_id FROM task_executions
                WHERE task_id=? ORDER BY execution_ordinal""",
                (task_uuid(source_description, source_obs_id),)).fetchall()
            failed_executions = sum(row[0] == "failed" for row in executions)
            running_manual = bool(executions and execution_id
                                  and executions[-1] == ("running", execution_id, execution_id))
            if (not executions or (executions[-1][0] != "failed" and not running_manual)
                    or failed_executions >= 3):
                raise RuntimeError("task execution is no longer eligible")
            if (failed_for_step < 1 or failed_for_step >= 3 or summary["in_flight"]
                    or summary["unknown_without_http_evidence"]
                    or any(row["result_json"]
                           or row["request_digest"] != request_digest
                           for row in step_rows)):
                raise RuntimeError("model retry no longer safe")
            ordinal = failed_for_step + 1
            key = "kg1-" + hashlib.sha256(
                f"{base_key}:{ordinal}:{grant_id}".encode("utf-8")).hexdigest()
            now = _now()
            db.execute("""INSERT INTO model_attempts
                (idempotency_key, business_key, source_description, source_obs_id,
                 step_id, request_digest, phase, stage, created_at, updated_at, queue_owned)
                 VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?)""",
                (key, business_key, source_description, source_obs_id,
                 step_id, request_digest, stage, now, now, int(queue_owned)))
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


_journals: dict[Path, ModelAttemptJournal] = {}
_journals_lock = threading.Lock()


def journal_from_backup_env() -> ModelAttemptJournal | None:
    """One journal per path: its schema setup takes the SQLite write lock."""
    path = os.environ.get("KG_HUB_INGEST_BACKUP_PATH", "").strip()
    if not path:
        return None
    journal_path = Path(path).with_name("model-attempts.sqlite3")
    with _journals_lock:
        journal = _journals.get(journal_path)
        if journal is None or not journal_path.exists():
            journal = _journals[journal_path] = ModelAttemptJournal(journal_path)
    return journal


def summarize_attempts(rows: list[dict], *, deadline_seconds: float,
                       now: datetime | None = None,
                       missing_receipt_step_id: str | None = None) -> dict:
    """Count each locally started model HTTP call once after its deadline.

    A gateway HTTP success is only a model result. It does not make the ingest
    business task successful until the graph result is independently verified.
    """
    now = now or datetime.now(timezone.utc)
    failed_by_step: dict[str, int] = {}
    failed_http_identities: set[str] = set()
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
        if (phase == "completed" and step == missing_receipt_step_id):
            # The gateway confirms the HTTP call returned, but kg-hub never
            # durably recorded that exact response. It cannot be treated as a
            # successful business step: reconciliation must count it against
            # the task-wide limit and let the original flow recover/replay it.
            failed_by_step[step] = failed_by_step.get(step, 0) + 1
            failed_http_identities.add(str(
                row.get("idempotency_key") or
                f"{step}:completed:{row.get('created_at')}"))
            continue
        if phase == "completed":
            # A response on another logical step is not charged as this
            # dashboard command's missing-receipt failure. Its own command
            # report will evaluate that exact step.
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
            failed_http_identities.add(str(
                row.get("idempotency_key") or
                f"{step}:{local_http_start or row.get('created_at')}"))
        else:
            in_flight = True
    return {
        "failed_calls_by_step": failed_by_step,
        "failed_calls_total": len(failed_http_identities),
        "max_failed_calls": max(failed_by_step.values(), default=0),
        "in_flight": in_flight,
        "admission_unknown": admission_unknown,
        "unknown_without_http_evidence": unknown_without_http_evidence,
        "cached_model_steps": len(cached_steps),
        "attempts_recorded": len(rows),
    }
