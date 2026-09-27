"""Durable identity/command dedup for the credvault reconciliation mailbox.

All HTTP requests use kg-hub's existing model-gateway caller bearer token.
Mailbox commands are transport only; they never authorize a paid model call.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5


BUSINESS_KEY = "kg_hub.entity_extract"
ZERO_STEP = "0" * 64
_HEX_STEP = re.compile(r"[0-9a-f]{64}\Z")


def task_uuid(source_description: str, source_obs_id: str) -> str:
    # JSON tuple avoids a:b/c vs a/b:c ambiguity while staying deterministic.
    identity = json.dumps([source_description, source_obs_id],
                          ensure_ascii=False, separators=(",", ":"))
    return str(uuid5(NAMESPACE_URL, f"kg-hub:{identity}"))


class MailboxStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS mailbox_tasks (
                task_id TEXT PRIMARY KEY,
                source_description TEXT NOT NULL,
                source_obs_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                report_json TEXT NOT NULL,
                UNIQUE(source_description, source_obs_id)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS mailbox_commands (
                command_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                model_step_id TEXT NOT NULL,
                action TEXT NOT NULL,
                result_state TEXT NOT NULL,
                result_version INTEGER NOT NULL,
                result_reason TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS manual_resume_jobs (
                command_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                source_description TEXT NOT NULL,
                source_obs_id TEXT NOT NULL,
                model_step_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                grant_id TEXT NOT NULL,
                job_json TEXT NOT NULL,
                state TEXT NOT NULL,
                result_state TEXT,
                result_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS manual_resume_one_active
                ON manual_resume_jobs(task_id, model_step_id)
                WHERE state IN ('queued', 'running')""")

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

    def prepare_report(self, source_description: str, source_obs_id: str,
                       *, step_id: str, state: str, failed_attempts: int,
                       retryable: bool, reason: str) -> dict:
        if not _HEX_STEP.fullmatch(step_id):
            raise ValueError("invalid model step identity")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
            raise ValueError("invalid reconciliation reason")
        if state not in {"queued", "running", "reconciliation", "retry_waiting",
                         "succeeded", "skipped", "failed", "unrecoverable"}:
            raise ValueError("invalid reconciliation state")
        if failed_attempts not in (0, 1, 2, 3):
            raise ValueError("invalid failed attempt count")
        identity = task_uuid(source_description, source_obs_id)
        data = {"business_key": BUSINESS_KEY, "task_id": identity,
                "model_step_id": step_id, "state": state,
                "failed_attempts": failed_attempts, "max_attempts": 3,
                "retryable": bool(retryable), "reason": reason}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT source_description, source_obs_id,
                version, report_json FROM mailbox_tasks WHERE task_id = ?""",
                (identity,)).fetchone()
            if row and row[:2] != (source_description, source_obs_id):
                raise RuntimeError("mailbox task identity collision")
            if row:
                old = json.loads(row[3])
                old.pop("version", None)
                version = row[2] if old == data else row[2] + 1
            else:
                version = 0
            data["version"] = version
            db.execute("""INSERT INTO mailbox_tasks
                (task_id, source_description, source_obs_id, version, report_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET version=excluded.version,
                    report_json=excluded.report_json""",
                (identity, source_description, source_obs_id,
                 version, json.dumps(data, separators=(",", ":"))))
        return data

    def lookup_task(self, task_id: str) -> tuple[str, str] | None:
        with self._connect() as db:
            row = db.execute("""SELECT source_description, source_obs_id
                FROM mailbox_tasks WHERE task_id = ?""", (task_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def read_report(self, task_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT report_json FROM mailbox_tasks WHERE task_id = ?",
                             (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def all_reports(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT report_json FROM mailbox_tasks").fetchall()
        return [json.loads(row[0]) for row in rows]

    def enqueue_manual_resume(self, command: dict, *, stage: str,
                              grant_id: str, attempt_epoch: str,
                              input_snapshot: dict,
                              created_by_request: str,
                              journal_step_id: str) -> dict:
        """Durably enqueue one user-authorized continuation; never auto-create it."""
        if stage not in {"node_extraction", "node_resolution", "edge_phase",
                         "attribute_phase"}:
            raise ValueError("unsupported manual resume stage")
        if (not grant_id or not attempt_epoch or not created_by_request
                or not journal_step_id
                or not isinstance(input_snapshot, dict)):
            raise ValueError("manual resume identity is incomplete")
        now = datetime.now(timezone.utc).isoformat()
        job = {"command_id": command["command_id"],
               "task_id": command["task_id"],
               "source_description": input_snapshot["source_description"],
               "source_obs_id": input_snapshot["source_obs_id"],
               "created_by_request": created_by_request,
               "model_step_id": command["model_step_id"], "stage": stage,
               "journal_step_id": journal_step_id,
               "grant_id": grant_id, "attempt_epoch": attempt_epoch,
               "input_snapshot": input_snapshot}
        encoded = json.dumps(job, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("""SELECT job_json, state FROM manual_resume_jobs
                WHERE command_id=?""", (command["command_id"],)).fetchone()
            if existing:
                saved_job = json.loads(existing[0])
                if saved_job != job:
                    raise RuntimeError("manual resume command identity collision")
                return {**saved_job, "state": existing[1]}
            active = db.execute("""SELECT job_json, state FROM manual_resume_jobs
                WHERE task_id=? AND model_step_id=? AND state IN ('queued', 'running')""",
                (command["task_id"], command["model_step_id"])).fetchone()
            if active:
                saved_job = json.loads(active[0])
                return {**saved_job, "state": active[1]}
            db.execute("""INSERT INTO manual_resume_jobs
                (command_id, task_id, source_description, source_obs_id,
                 model_step_id, stage, grant_id, job_json, state,
                 created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (job["command_id"], job["task_id"], job["source_description"],
                 job["source_obs_id"], job["model_step_id"], stage, grant_id,
                 encoded, now, now))
        return {**job, "state": "queued"}

    def claim_manual_resume_job(self) -> dict | None:
        """Claim one persisted human request; process restart never reclaims running jobs."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT command_id, job_json FROM manual_resume_jobs
                WHERE state='queued' ORDER BY created_at, command_id LIMIT 1""").fetchone()
            if row is None:
                return None
            now = datetime.now(timezone.utc).isoformat()
            changed = db.execute("""UPDATE manual_resume_jobs SET state='running',
                updated_at=? WHERE command_id=? AND state='queued'""",
                (now, row[0]))
            if changed.rowcount != 1:
                return None
        return {**json.loads(row[1]), "state": "running"}

    def discard_queued_manual_resume(self, command_id: str) -> bool:
        """Remove an outbox row only when its conditional graph claim failed."""
        with self._connect() as db:
            changed = db.execute("""DELETE FROM manual_resume_jobs
                WHERE command_id=? AND state='queued'""", (command_id,))
            return changed.rowcount == 1

    def finish_manual_resume_job(self, command_id: str, *, state: str,
                                 reason: str) -> None:
        if state not in {"succeeded", "reconciliation", "failed", "unrecoverable"}:
            raise ValueError("invalid manual resume terminal state")
        with self._connect() as db:
            changed = db.execute("""UPDATE manual_resume_jobs SET state='finished',
                result_state=?, result_reason=?, updated_at=? WHERE command_id=?
                AND state='running'""",
                (state, reason, datetime.now(timezone.utc).isoformat(), command_id))
            if changed.rowcount != 1:
                raise RuntimeError("manual resume job is not running")

    def list_manual_resume_jobs(self, *, state: str | None = None) -> list[dict]:
        with self._connect() as db:
            if state is None:
                rows = db.execute("SELECT job_json, state, result_state, result_reason "
                                  "FROM manual_resume_jobs ORDER BY created_at").fetchall()
            else:
                rows = db.execute("SELECT job_json, state, result_state, result_reason "
                                  "FROM manual_resume_jobs WHERE state=? "
                                  "ORDER BY created_at", (state,)).fetchall()
        return [{**json.loads(row[0]), "state": row[1],
                 "result_state": row[2], "result_reason": row[3]} for row in rows]

    def active_manual_resume_job(self, task_id: str,
                                 model_step_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("""SELECT job_json, state FROM manual_resume_jobs
                WHERE task_id=? AND model_step_id=?
                  AND state IN ('queued', 'running')""",
                (task_id, model_step_id)).fetchone()
        return {**json.loads(row[0]), "state": row[1]} if row else None

    def manual_resume_job(self, command_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("""SELECT job_json, state, result_state, result_reason
                FROM manual_resume_jobs WHERE command_id=?""", (command_id,)).fetchone()
        if not row:
            return None
        return {**json.loads(row[0]), "state": row[1],
                "result_state": row[2], "result_reason": row[3]}

    def command_result(self, command_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("""SELECT task_id, model_step_id, action,
                result_state, result_version, result_reason
                FROM mailbox_commands WHERE command_id = ?""",
                (command_id,)).fetchone()
        if not row:
            return None
        return dict(zip(("task_id", "model_step_id", "action", "result_state",
                         "result_version", "result_reason"), row))

    def save_command_result(self, command: dict, *, state: str,
                            version: int, reason: str) -> dict:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO mailbox_commands
                (command_id, task_id, model_step_id, action,
                 result_state, result_version, result_reason)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (command["command_id"], command["task_id"],
                 command["model_step_id"], command["action"],
                 state, version, reason))
        saved = self.command_result(command["command_id"])
        if saved is None or any(saved[field] != command[field]
                                for field in ("task_id", "model_step_id", "action")):
            raise RuntimeError("mailbox command identity collision")
        return saved

    def commit_command_result(self, command: dict, *, state: str, reason: str,
                              expected_version: int,
                              report_data: dict | None = None) -> tuple[dict, dict | None]:
        """Atomically persist a command verdict and its next report version.

        The gateway requires a command completion version newer than the
        version the human acted on. Saving both rows in one SQLite transaction
        lets a redelivered command recover and publish the same verdict after a
        process crash without rerunning its graph check.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("""SELECT task_id, model_step_id, action,
                result_state, result_version, result_reason
                FROM mailbox_commands WHERE command_id = ?""",
                (command["command_id"],)).fetchone()
            if existing:
                saved = dict(zip(("task_id", "model_step_id", "action",
                                  "result_state", "result_version", "result_reason"),
                                 existing))
                if any(saved[field] != command[field]
                       for field in ("task_id", "model_step_id", "action")):
                    raise RuntimeError("mailbox command identity collision")
                report_row = db.execute(
                    "SELECT report_json FROM mailbox_tasks WHERE task_id = ?",
                    (command["task_id"],)).fetchone()
                report = json.loads(report_row[0]) if report_row else None
                return saved, report

            report_row = db.execute("""SELECT version, report_json
                FROM mailbox_tasks WHERE task_id = ?""",
                (command["task_id"],)).fetchone()
            current = json.loads(report_row[1]) if report_row else None
            if report_data is not None:
                if report_data.get("task_id") != command["task_id"]:
                    raise RuntimeError("reconciliation report belongs to another task")
                next_report = dict(report_data)
            elif current is not None:
                next_report = dict(current)
                next_report["reason"] = reason
                state = next_report["state"]
            else:
                next_report = None

            previous = int(report_row[0]) if report_row else -1
            result_version = max(previous + 1, int(expected_version) + 1)
            if next_report is not None:
                next_report["version"] = result_version
                db.execute("""INSERT INTO mailbox_tasks
                    (task_id, source_description, source_obs_id, version, report_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET version=excluded.version,
                        report_json=excluded.report_json""",
                    (next_report["task_id"], next_report["source_description"],
                     next_report["source_obs_id"], result_version,
                     json.dumps(next_report, separators=(",", ":"))))

            db.execute("""INSERT INTO mailbox_commands
                (command_id, task_id, model_step_id, action,
                 result_state, result_version, result_reason)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (command["command_id"], command["task_id"],
                 command["model_step_id"], command["action"],
                 state, result_version, reason))
        saved = self.command_result(command["command_id"])
        if saved is None:
            raise RuntimeError("mailbox command result vanished")
        return saved, next_report


def mailbox_post(base_url: str, token: str, action: str, payload: dict,
                 *, timeout: float = 5) -> dict:
    if action not in {"report", "claim", "complete"}:
        raise ValueError("invalid mailbox action")
    req = Request(base_url.rstrip("/") + f"/v1/reconciliation/{action}",
                  data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                  headers={"Authorization": f"Bearer {token}",
                           "Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as response:
        data = json.load(response)
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("external_calls") != 0:
        raise RuntimeError("invalid mailbox response")
    return data
