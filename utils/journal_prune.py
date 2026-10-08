"""Prune journal evidence of tasks whose business result is long settled.

Only IngestedKey rows with status 'ok' older than the retention window are
eligible. Such a task cannot be dispatched again (duplicate intake is refused),
resumed or reconciled, so its model answers, stage artifacts and commit fences
guard nothing any more. Every other status keeps its full evidence, and so does
an 'ok' task whose journal still shows an unsettled execution, an open human
retry grant, or a gateway-queue answer whose business receipt the gateway has
not acknowledged yet (the receipt is derived from model_attempts, so deleting
the attempt first would strand the queue job).

Safe while the service runs: short batched transactions, no VACUUM. SQLite
reuses the freed pages, so the file stops growing rather than shrinking.
Without ``--apply`` nothing is deleted; the report shows what would be.

    python -m utils.journal_prune                 # dry run
    python -m utils.journal_prune --apply --max-seconds 300
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.reconciliation_mailbox import task_uuid

log = logging.getLogger("kg_hub.journal_prune")

UNSETTLED_EXECUTIONS = ("running", "uncertain")

# (table, key columns): "task" rows are keyed by task_uuid, the rest by sd/sid.
# Acknowledged receipts are keyed through model_attempts, so they go first.
_TABLES = (
    ("queue_business_receipts", ("idempotency_key",)),
    ("graphiti_stage_artifacts", ("task_sd", "task_sid")),
    ("model_attempts", ("source_description", "source_obs_id")),
    ("episode_contexts", ("source_description", "source_obs_id")),
    ("model_retry_grants", ("source_description", "source_obs_id")),
    ("gateway_step_mappings", ("task_id",)),
    ("task_executions", ("task_id",)),
)


def settled_tasks(graph, *, older_than: str) -> list[tuple[str, str]]:
    """Business-complete task identities last touched before ``older_than``."""
    result = graph.ro_query(
        "MATCH (k:IngestedKey) WHERE k.status = 'ok' "
        "AND coalesce(k.updated_at, k.created_at) < $cutoff "
        "RETURN k.source_description, k.source_obs_id",
        {"cutoff": older_than})
    return [(sd, sid) for sd, sid in result.result_set if sd and sid]


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    return db


def _present_tables(db) -> list[tuple[str, tuple[str, ...]]]:
    names = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return [(table, keys) for table, keys in _TABLES if table in names]


def _still_guarding(db, tables, sd: str, sid: str, tid: str) -> bool:
    names = {table for table, _ in tables}
    if "task_executions" in names and db.execute(
            "SELECT 1 FROM task_executions WHERE task_id=? AND state IN (?, ?) LIMIT 1",
            (tid, *UNSETTLED_EXECUTIONS)).fetchone():
        return True
    if "model_retry_grants" in names and db.execute(
            "SELECT 1 FROM model_retry_grants WHERE source_description=? "
            "AND source_obs_id=? AND state='granted' LIMIT 1", (sd, sid)).fetchone():
        return True
    # Same answer set queue_business_receipts() turns into receipts.
    return "queue_business_receipts" in names and bool(db.execute(
        "SELECT 1 FROM model_attempts a LEFT JOIN queue_business_receipts r "
        "ON r.idempotency_key=a.idempotency_key "
        "WHERE a.source_description=? AND a.source_obs_id=? AND a.queue_owned=1 "
        "AND (a.phase='failed' OR (a.phase='completed' AND a.result_json IS NOT NULL)) "
        "AND coalesce(r.acknowledged, 0)=0 LIMIT 1", (sd, sid)).fetchone())


def _task_rows(db, tables, sd: str, sid: str, tid: str, *, delete: bool) -> dict:
    counts = {}
    for table, keys in tables:
        where = " AND ".join(f"{key}=?" for key in keys)
        values = (tid,) if keys == ("task_id",) else (sd, sid)
        if keys == ("idempotency_key",):
            where = ("acknowledged=1 AND idempotency_key IN (SELECT idempotency_key "
                     "FROM model_attempts WHERE source_description=? AND source_obs_id=?)")
        if delete:
            counts[table] = db.execute(f"DELETE FROM {table} WHERE {where}", values).rowcount
        else:
            counts[table] = db.execute(
                f"SELECT count(*) FROM {table} WHERE {where}", values).fetchone()[0]
    return counts


def prune(journal_path: Path, tasks: list[tuple[str, str]], *, apply: bool,
          batch: int = 25, pause: float = 0.2, max_seconds: float = 600) -> dict:
    """Delete (or count) the journal rows of ``tasks`` in short transactions."""
    started = time.monotonic()
    rows = {table: 0 for table, _ in _TABLES}
    report = {"apply": apply, "eligible": len(tasks), "pruned": 0,
              "skipped_guarding": 0, "remaining": 0, "rows": rows}
    db = _connect(journal_path)
    try:
        tables = _present_tables(db)
        index = 0
        while index < len(tasks):
            if time.monotonic() - started >= max_seconds:
                break
            chunk = tasks[index:index + batch]
            index += len(chunk)
            db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            try:
                for sd, sid in chunk:
                    tid = task_uuid(sd, sid)
                    if _still_guarding(db, tables, sd, sid, tid):
                        report["skipped_guarding"] += 1
                        continue
                    counts = _task_rows(db, tables, sd, sid, tid, delete=apply)
                    if any(counts.values()):
                        report["pruned"] += 1
                    for table, count in counts.items():
                        rows[table] += count
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            if apply and pause:
                time.sleep(pause)
        report["remaining"] = len(tasks) - index
        if apply:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        report["file_bytes"] = db.execute("PRAGMA page_count").fetchone()[0] * page_size
        report["reusable_bytes"] = db.execute("PRAGMA freelist_count").fetchone()[0] * page_size
    finally:
        db.close()
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def _graph():
    from falkordb import FalkorDB

    db = FalkorDB(host=os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1"),
                  port=int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379")),
                  password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None)
    return db.select_graph(os.environ.get("KG_HUB_GRAPH", "kg_hub"))


def _journal_path(value: str | None) -> Path:
    if value:
        return Path(value)
    backup = os.environ.get("KG_HUB_INGEST_BACKUP_PATH", "").strip()
    if not backup:
        raise SystemExit("KG_HUB_INGEST_BACKUP_PATH is unset and --journal not given")
    return Path(backup).with_name("model-attempts.sqlite3")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="delete; default only counts")
    parser.add_argument("--retention-days", type=float, default=1,
                        help="'ok' is terminal; the window only keeps recent evidence for humans")
    parser.add_argument("--batch", type=int, default=25)
    parser.add_argument("--pause", type=float, default=0.2)
    parser.add_argument("--max-seconds", type=float, default=600)
    parser.add_argument("--journal", help="default: next to KG_HUB_INGEST_BACKUP_PATH")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    path = _journal_path(args.journal)
    if not path.exists():
        raise SystemExit(f"journal not found: {path}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.retention_days)).isoformat()
    tasks = settled_tasks(_graph(), older_than=cutoff)
    report = prune(path, tasks, apply=args.apply, batch=args.batch,
                   pause=args.pause, max_seconds=args.max_seconds)
    report["cutoff"] = cutoff
    log.info("[journal:prune] %s", json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
