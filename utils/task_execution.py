"""Observe the original ingest worker without making normal intake depend on logging."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from utils.model_attempt_journal import summarize_attempts

log = logging.getLogger("kg_hub.task_execution")


async def read_task(driver, sd, sid):
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey {source_description: $sd, source_obs_id: $sid}) "
        "RETURN k.source_description AS source_description, k.source_obs_id AS source_obs_id, "
        "k.created_by_request AS created_by_request, k.status AS status, "
        "k.worker_execution_id AS worker_execution_id, k.worker_state AS worker_state, "
        "k.manual_resume_command_id AS manual_resume_command_id, "
        "k.episode_uuid AS episode_uuid, k.name AS name, k.stage AS stage, "
        "k.predigest_children AS predigest_children, k.failed_children AS failed_children",
        sd=sd, sid=sid)
    return dict(rows[0]) if rows else None


async def run_task_execution(*, driver, sd, sid, worker, journal_factory,
                             business_result_persisted, deadline_seconds,
                             manual_command_id=None):
    """One worker run is one attempt, regardless of its number of model stages.

    Ordinary ingestion always reaches its worker even if observation fails.
    Manual execution requires a durable claim before running because that claim
    enforces the operator command's deduplication and the three-attempt limit.
    """
    journal = None
    execution_id = None
    claimed = False
    original_request_id = None
    try:
        row = await read_task(driver, sd, sid)
        if not row or not row.get("created_by_request"):
            raise RuntimeError("original task identity unavailable")
        original_request_id = row["created_by_request"]
        execution_id = manual_command_id or original_request_id
        if manual_command_id and (
                row.get("status") != "pending" or row.get("worker_state") != "queued"
                or row.get("manual_resume_command_id") != manual_command_id):
            raise RuntimeError("manual task claim changed")
        journal = await asyncio.to_thread(journal_factory)
        if journal is None:
            raise RuntimeError("task execution journal unavailable")
        claim = await asyncio.to_thread(
            journal.begin_task_execution,
            sd, sid, execution_id, manual_command_id=manual_command_id)
        if not claim["created"]:
            if manual_command_id:
                raise RuntimeError("manual task execution already started")
            # Ordinary intake keeps its existing admission contract. Never
            # overwrite an earlier record when an unexpected duplicate occurs.
            raise RuntimeError("initial execution record already exists")
        claimed = True
        changed, _, _ = await driver.execute_query(
            "MATCH (k:IngestedKey {source_description: $sd, source_obs_id: $sid}) "
            "WHERE k.status = 'pending' AND k.created_by_request = $request_id "
            "AND ($command_id IS NULL OR (k.worker_state = 'queued' "
            "AND k.manual_resume_command_id = $command_id)) "
            "SET k.worker_state = 'running', k.worker_execution_id = $execution_id, "
            "k.updated_at = $now RETURN count(k) AS c",
            sd=sd, sid=sid, request_id=row["created_by_request"],
            command_id=manual_command_id, execution_id=execution_id,
            now=datetime.now(timezone.utc).isoformat())
        if not changed or changed[0].get("c") != 1:
            raise RuntimeError("task execution graph claim changed")
    except Exception:
        log.exception("[task_execution:start] could not record worker start")
        if manual_command_id:
            if claimed:
                await asyncio.to_thread(
                    journal.finish_task_execution, sd, sid, execution_id,
                    state="uncertain", reason="worker_not_started")
            raise

    returned = False
    try:
        result = await worker()
        returned = True
        return result
    finally:
        if claimed:
            try:
                row = await read_task(driver, sd, sid)
                owns_row = bool(row and row.get("created_by_request") == original_request_id
                                and row.get("worker_execution_id") == execution_id)
                complete = bool(owns_row and await business_result_persisted(driver, row))
                attempts = summarize_attempts(
                    await asyncio.to_thread(journal.find_task, sd, sid),
                    deadline_seconds=deadline_seconds)
                state = "uncertain"
                if complete:
                    state = "succeeded"
                    await asyncio.to_thread(journal.queue_business_receipts, sd, sid,
                        'neo4j:ingest:' + str(row.get('episode_uuid') or original_request_id))
                elif (returned and owns_row and row.get("status") in {
                        "error", "needs_reconciliation", "failed"}
                      and not attempts["in_flight"]
                      and not attempts["unknown_without_http_evidence"]):
                    state = "failed"
                    await asyncio.to_thread(journal.queue_business_receipts, sd, sid,
                        'sqlite:task_execution:' + execution_id, 'failed')
                await asyncio.to_thread(
                    journal.finish_task_execution,
                    sd, sid, execution_id, state=state,
                    reason="business_result_persisted" if complete else "business_result_missing")
                summary = (await asyncio.to_thread(journal.task_execution_summary, sd, sid)
                           if state == "failed" else None)
                if summary and summary["failed_attempts"] >= 3:
                    await driver.execute_query(
                        "MATCH (k:IngestedKey {source_description: $sd, source_obs_id: $sid}) "
                        "WHERE k.worker_execution_id = $execution_id "
                        "AND k.status IN ['error', 'needs_reconciliation'] "
                        "SET k.status = 'failed', k.worker_state = null, "
                        "k.error_kind = 'model_attempts_exhausted', k.updated_at = $now",
                        sd=sd, sid=sid, execution_id=execution_id,
                        now=datetime.now(timezone.utc).isoformat())
            except (Exception, asyncio.CancelledError):
                # Observation must never change the original worker's return
                # or cancellation. Reconciliation can read its durable claim.
                log.exception("[task_execution:finish] could not record worker outcome")
