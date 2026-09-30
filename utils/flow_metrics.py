"""Durable, bounded commit telemetry for the flow dashboard.

Only timings and outcome flags are stored; no observation or session identity.
The backup volume survives server container replacement. In-memory samples
remain available if the durable store is temporarily unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from contextlib import closing
from datetime import datetime
from pathlib import Path

_samples: deque[dict] = deque(maxlen=4000)
_lock = threading.Lock()
_db_lock = threading.Lock()
_write_error: str | None = None
_read_error: str | None = None
_RETENTION_S = 72 * 3600
_COLUMNS = ("at", "conflict", "lock_wait_s", "commit_s", "validate_skipped",
            "prevalidated_conflict")
_COMMIT_FIELDS = ("lock_wait_avg", "lock_wait_p90", "commit_avg", "commit_p50",
                  "commit_attempts", "conflicts", "prevalidated_conflicts",
                  "validate_skipped", "conflict_rate")
log = logging.getLogger(__name__)


def _backup_sibling(name: str) -> Path | None:
    backup = os.environ.get("KG_HUB_INGEST_BACKUP_PATH", "").strip()
    return Path(backup).with_name(name) if backup else None


def storage_error() -> str | None:
    with _lock:
        return "；".join(e for e in (_write_error, _read_error) if e) or None


def _set_error(kind: str, error: str | None) -> None:
    global _write_error, _read_error
    with _lock:
        if kind == "write":
            _write_error = error
        else:
            _read_error = error


def record(*, conflict: bool, lock_wait_s: float = 0, commit_s: float | None = None,
           validate_skipped: bool = False, prevalidated_conflict: bool = False,
           at: float | None = None) -> None:
    sample = {"at": time.time() if at is None else at,
              "conflict": conflict, "lock_wait_s": max(0, lock_wait_s),
              "commit_s": commit_s, "validate_skipped": validate_skipped,
              "prevalidated_conflict": prevalidated_conflict,
              "_id": uuid.uuid4().hex}
    with _lock:
        _samples.append(sample)
    path = _backup_sibling("flow-commit-metrics.sqlite3")
    if path is None:
        return
    try:
        with _db_lock, closing(sqlite3.connect(path, timeout=2)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS commit_samples (
                sample_id TEXT PRIMARY KEY, at REAL NOT NULL,
                conflict INTEGER NOT NULL, lock_wait_s REAL NOT NULL,
                commit_s REAL, validate_skipped INTEGER NOT NULL,
                prevalidated_conflict INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS commit_samples_at ON commit_samples(at)")
            db.execute("INSERT INTO commit_samples VALUES (?,?,?,?,?,?,?)",
                       (sample["_id"], *(sample[k] for k in _COLUMNS)))
            db.execute("DELETE FROM commit_samples WHERE at < ?",
                       (sample["at"] - _RETENTION_S,))
        _set_error("write", None)
    except (OSError, sqlite3.Error) as exc:
        _set_error("write", "写入 " + type(exc).__name__)
        log.warning("flow commit telemetry persistence failed: %s", exc)


def recent(*, since: float) -> list[dict]:
    """Merge durable and live samples by private random ID; never expose IDs."""
    with _lock:
        rows = {row["_id"]: row.copy() for row in _samples if row["at"] >= since}
    path = _backup_sibling("flow-commit-metrics.sqlite3")
    if path and path.is_file():
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)) as db:
                saved = db.execute(
                    "SELECT sample_id,at,conflict,lock_wait_s,commit_s,"
                    "validate_skipped,prevalidated_conflict FROM commit_samples "
                    "WHERE at >= ? ORDER BY at", (since,)).fetchall()
            for sample_id, *values in saved:
                rows[sample_id] = {"_id": sample_id, **dict(zip(_COLUMNS, values))}
            _set_error("read", None)
        except (OSError, sqlite3.Error) as exc:
            _set_error("read", "读取 " + type(exc).__name__)
            log.warning("flow commit telemetry read failed: %s", exc)
    return [{k: row[k] for k in _COLUMNS}
            for row in sorted(rows.values(), key=lambda row: row["at"])]


def archived_hourly(*, since: float) -> list[dict]:
    """Read a one-time snapshot of complete hours from before durability existed."""
    path = _backup_sibling("flow-commit-hourly-seed.json")
    if path is None or not path.is_file():
        return []
    source = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(source, list):
        raise ValueError("flow commit hourly seed must be a list")
    rows = []
    for row in source:
        if not isinstance(row, dict) or not isinstance(row.get("hour"), str):
            continue
        try:
            hour_at = datetime.fromisoformat(row["hour"] + ":00:00+00:00").timestamp()
        except ValueError:
            continue
        if hour_at < since or not isinstance(row.get("commit_attempts"), int):
            continue
        rows.append({"hour": row["hour"], **{
            key: row[key] for key in _COMMIT_FIELDS
            if isinstance(row.get(key), (int, float))}})
    return rows
