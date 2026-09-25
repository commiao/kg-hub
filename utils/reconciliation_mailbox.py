"""Durable identity/command dedup for the credvault reconciliation mailbox.

All HTTP requests use kg-hub's existing model-gateway caller bearer token.
Mailbox commands are transport only; they never authorize a paid model call.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
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
