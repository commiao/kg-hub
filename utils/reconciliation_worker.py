"""Poll human-created mailbox commands without creating automatic retries.

Only an existing human command may grant one failed-step retry. The original
worker restores its durable stage outputs and publishes the business result.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.error import HTTPError

from utils.model_attempt_journal import summarize_attempts
from utils.reconciliation_mailbox import (
    BUSINESS_KEY, ZERO_STEP, MailboxStore, mailbox_post, task_uuid,
)


log = logging.getLogger("kg_hub.reconciliation")
REPLAYABLE_GRAPHITI_STAGES = frozenset({
    "node_extraction", "node_resolution", "edge_phase", "attribute_phase", "predigest_split",
})


def _error_detail(exc: HTTPError) -> str:
    try:
        return exc.read(300).decode("utf-8", "replace").replace("\n", " ")
    except Exception:
        return ""


def _reason(value: object, fallback: str = "business_state_unknown") -> str:
    text = str(value or "").lower()
    return text if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text) else fallback


def _task_report_data(journal, row: dict, *, deadline_seconds: float,
                     business_result_persisted: bool = False,
                     manual_resume_available: bool = False) -> dict:
    sd, sid = row["source_description"], row["source_obs_id"]
    attempts = journal.find_task(sd, sid) if journal else []
    summary = summarize_attempts(attempts, deadline_seconds=deadline_seconds)
    executions = None
    if journal and hasattr(journal, "task_execution_summary"):
        try:
            candidate = journal.task_execution_summary(sd, sid)
            if isinstance(candidate, dict):
                executions = candidate
        except Exception:
            executions = None
    execution_history_known = bool(executions and executions.get("execution_count", 0) > 0)
    failed = min(3, int(executions.get("failed_attempts", 0))) if executions else 0
    latest_execution = (executions.get("executions", [])[-1]
                        if execution_history_known else None)
    open_attempts = [a for a in attempts if not a["result_json"]
                     and a["provider_call_started"] != 0]
    failed_steps = summary["failed_calls_by_step"]
    failed_attempts = [a for a in attempts
                       if failed_steps.get(a["step_id"], 0) > 0
                       and not a["result_json"]]
    completed_without_receipt = [a for a in attempts
                                 if a.get("phase") == "completed"
                                 and not a.get("result_json")]
    failed_keys = {a.get("idempotency_key") for a in failed_attempts}
    open_keys = {a.get("idempotency_key") for a in open_attempts}
    target_candidates = [a for a in attempts
                         if a.get("idempotency_key") in failed_keys
                         or a.get("idempotency_key") in open_keys
                         or (a.get("phase") == "completed" and not a.get("result_json"))]
    target = target_candidates[-1] if target_candidates else None
    if target:
        summary = summarize_attempts(
            attempts, deadline_seconds=deadline_seconds,
            missing_receipt_step_id=target["step_id"])
    step = (target["step_id"] if target else
            open_attempts[-1]["step_id"] if open_attempts else
            attempts[-1]["step_id"] if attempts else ZERO_STEP)
    wire_step_id = None
    mapping_valid = False
    if target and journal:
        try:
            wire_step_id = journal.gateway_step_for_attempt(target["idempotency_key"])
            if wire_step_id:
                resolved = journal.resolve_gateway_step(sd, sid, wire_step_id)
                mapping_valid = (resolved["local_step_id"] == step
                                 and resolved["request_digest"] == target["request_digest"]
                                 and resolved["stage"] == target.get("stage"))
        except Exception:
            wire_step_id = None
    gateway_mapping_missing = bool(target and (not wire_step_id or not mapping_valid))
    raw_status = row.get("status")
    state = {"pending": ("running" if row.get("worker_state") == "running"
                          else "queued"),
             "queued": "queued", "running": "running",
             "needs_reconciliation": "reconciliation", "error": "reconciliation",
             "failed": "failed"}.get(
        raw_status, "reconciliation")
    if raw_status == "ok":
        state = "succeeded" if business_result_persisted else "reconciliation"
    if raw_status == "failed" and row.get("error_kind") in {
            "reconciliation_plan_missing", "task_execution_history_missing",
            "reconciliation_source_identity_missing",
            "reconciliation_model_step_missing",
            "reconciliation_model_step_identity_missing",
            "reconciliation_gateway_step_missing"}:
        state = "unrecoverable"
    if gateway_mapping_missing and raw_status in {"needs_reconciliation", "error"}:
        state = "unrecoverable"
    if not business_result_persisted and failed >= 3:
        state = "failed"
    stage = target.get("stage") if target else row.get("stage")
    replay_conditions = bool(
        raw_status in {"needs_reconciliation", "error"}
        and target is not None
        and stage in REPLAYABLE_GRAPHITI_STAGES
        and target.get("business_key") == BUSINESS_KEY
        and target.get("idempotency_key")
        and target.get("request_digest")
        and bool(wire_step_id)
        and execution_history_known
        and latest_execution.get("state") == "failed"
        and not executions.get("active")
        and failed < 3
        and not summary["in_flight"]
        and not summary["unknown_without_http_evidence"]
    )
    retryable = replay_conditions and manual_resume_available
    reason = ("manual_retry_available" if retryable else
              "model_attempts_exhausted" if state == "failed" and failed >= 3 else
              "task_execution_history_missing" if not execution_history_known
              and raw_status in {"needs_reconciliation", "error"} else
              "retry_adapter_unavailable" if replay_conditions else
              row.get("error_kind"))
    if gateway_mapping_missing and not reason:
        reason = "reconciliation_gateway_step_missing"
    if not reason:
        if state == "succeeded":
            reason = "business_result_persisted"
        elif raw_status == "ok":
            reason = "business_result_unverified"
        elif summary["in_flight"]:
            # A local HTTP-start marker has not reached the configured maximum
            # request deadline yet. Keep tracking it; this is not a failed call.
            reason = "model_call_in_flight"
        elif summary["unknown_without_http_evidence"]:
            reason = "model_admission_unknown"
        elif failed >= 3:
            reason = "model_attempts_exhausted"
        elif state in {"queued", "running"}:
            reason = "business_task_running"
        elif state == "reconciliation" and target is not None and failed < 3:
            reason = "retry_adapter_unavailable"
        elif state == "reconciliation":
            reason = "retry_adapter_unavailable"
        else:
            reason = "business_state_unknown"
    return {"business_key": BUSINESS_KEY,
            "task_id": task_uuid(sd, sid),
            "source_description": sd, "source_obs_id": sid,
            "stage": stage,
            "model_step_id": wire_step_id if mapping_valid else ZERO_STEP,
            "journal_step_id": step,
            "request_digest": target.get("request_digest") if target else None,
            "state": state,
            "failed_attempts": failed, "max_attempts": 3,
            "retryable": retryable, "reason": _reason(reason)}


def prepare_task_report(store: MailboxStore, journal, row: dict,
                        *, deadline_seconds: float,
                        business_result_persisted: bool = False,
                        manual_resume_available: bool = False) -> dict:
    data = _task_report_data(
        journal, row, deadline_seconds=deadline_seconds,
        business_result_persisted=business_result_persisted,
        manual_resume_available=manual_resume_available)
    return store.prepare_report(
        data["source_description"], data["source_obs_id"],
        step_id=data["model_step_id"], state=data["state"],
        failed_attempts=data["failed_attempts"], retryable=data["retryable"],
        reason=data["reason"])


ACTIVE_REPORT_STATES = frozenset({"queued", "running", "reconciliation", "retry_waiting"})


class RefreshSchedule:
    """Which tracked tasks to re-check this cycle: the head of a due-time queue.

    2026-10-10: every ~10s cycle re-checked all 7382 tracked tasks (graph
    queries, journal reads and gateway attempt-status for each). It only looked
    bounded because each check also paid a FULL-sync journal write; once that
    write went away the loop ran flat out (~1000 attempt-status/min, 67% CPU).
    Same shape as the gateway's own reconcile_due: earliest due first, at most
    MAX_CHECKS per cycle, an unchanged report doubles its wait up to MAX_DELAY,
    a changed one (new version) resets it. New tasks are due at once.
    """
    FIRST_DELAY = 10.0
    MAX_DELAY = 600.0
    MAX_CHECKS = 20

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._state: dict[str, tuple[float, float, object]] = {}

    def pick(self, reports: list[dict]) -> list[dict]:
        now = self.clock()
        active = {r["task_id"]: r for r in reports if r.get("state") in ACTIVE_REPORT_STATES}
        for task_id in set(self._state) - set(active):
            del self._state[task_id]
        due = [(self._state.get(task_id, (0.0,))[0], task_id)
               for task_id in active if now >= self._state.get(task_id, (0.0,))[0]]
        return [active[task_id] for _, task_id in sorted(due)[:self.MAX_CHECKS]]

    def observed(self, task_id: str, version: object) -> float:
        """``version=None`` means the check failed: back off, keep the last version."""
        previous = self._state.get(task_id)
        changed = previous is None or (version is not None and version != previous[2])
        delay = self.FIRST_DELAY if changed else min(previous[1]*2, self.MAX_DELAY)
        kept = version if version is not None else (previous[2] if previous else None)
        self._state[task_id] = (self.clock()+delay, delay, kept)
        return delay


async def refresh_tracked_reports(*, store: MailboxStore, journal,
                                  check_task, deadline_seconds: float,
                                  manual_resume_available: bool = False,
                                  schedule: RefreshSchedule | None = None) -> list[dict]:
    """Refresh durable nonterminal mailbox identities from authoritative state.

    A human reconcile command can move an IngestedKey out of the original
    needs_reconciliation/failed query. Keep polling the task identities already
    in the mailbox until they have a verified business terminal state.
    Without ``schedule`` every active task is checked each call.
    """
    refreshed = []
    priors = await asyncio.to_thread(store.all_reports)
    if schedule is not None:
        priors = schedule.pick(priors)
    for prior in priors:
        if prior.get("state") not in ACTIVE_REPORT_STATES:
            continue
        identity = await asyncio.to_thread(store.lookup_task, prior["task_id"])
        if identity is None:
            continue
        try:
            result = await check_task(*identity,
                                      model_step_id=prior.get("model_step_id"))
        except Exception:
            # A temporary graph/journal read error cannot be translated into a
            # business failure or overwrite the last known report.
            if schedule is not None:
                schedule.observed(prior["task_id"], None)
            continue
        if (result.get("status") != "ok"
                or not isinstance(result.get("task"), dict)
                or not isinstance(result.get("business_result_persisted"), bool)):
            if schedule is not None:
                schedule.observed(prior["task_id"], None)
            continue
        report = await asyncio.to_thread(
            prepare_task_report, store, journal, result["task"],
            deadline_seconds=deadline_seconds,
            business_result_persisted=result["business_result_persisted"],
            manual_resume_available=manual_resume_available)
        if schedule is not None:
            schedule.observed(prior["task_id"], report.get("version"))
        refreshed.append(report)
    return refreshed


async def process_command(command: dict, *, store: MailboxStore, journal,
                          check_task, base_url: str, token: str,
                          deadline_seconds: float,
                          enqueue_manual_resume=None) -> dict:
    """Dedup by command_id before delivering the mailbox completion."""
    for field in ("command_id", "task_id", "model_step_id", "action",
                  "expected_version", "lease_token"):
        if field not in command:
            raise RuntimeError(f"mailbox command missing {field}")
    if command["action"] != "reconcile":
        raise RuntimeError("unsupported mailbox command")
    saved = store.command_result(command["command_id"])
    if saved is None:
        identity = store.lookup_task(command["task_id"])
        report = store.read_report(command["task_id"])
        if identity is None or report is None:
            state, reason, report_data = "unrecoverable", "task_not_found", None
        elif (report["model_step_id"] != command["model_step_id"]
              or report["version"] != command["expected_version"]):
            state, reason, report_data = report["state"], "command_stale", None
        else:
            result = await check_task(*identity,
                                      model_step_id=command["model_step_id"])
            if (result.get("status") != "ok"
                    or not isinstance(result.get("task"), dict)
                    or not isinstance(result.get("business_result_persisted"), bool)):
                raise RuntimeError("business status check unavailable")
            report_data = _task_report_data(
                journal, result["task"], deadline_seconds=deadline_seconds,
                business_result_persisted=result["business_result_persisted"],
                manual_resume_available=enqueue_manual_resume is not None)
            if (not result["business_result_persisted"]
                    and report_data["retryable"]):
                if enqueue_manual_resume is None:
                    report_data = {**report_data, "state": "reconciliation",
                                   "retryable": False,
                                   "reason": "retry_adapter_unavailable"}
                else:
                    decision = await enqueue_manual_resume(
                        command, result["task"], report_data)
                    if decision:
                        report_data = {**report_data, **decision}
            state, reason = report_data["state"], report_data["reason"]
        saved, updated = store.commit_command_result(
            command, state=state, reason=reason,
            expected_version=int(command["expected_version"]),
            report_data=report_data)
    else:
        updated = store.read_report(command["task_id"])
    if updated is not None and updated["version"] > int(command["expected_version"]):
        await asyncio.to_thread(mailbox_post, base_url, token, "report", updated)
    payload = {"business_key": BUSINESS_KEY,
               "command_id": command["command_id"],
               "lease_token": command["lease_token"],
               "result_state": saved["result_state"],
               "result_version": saved["result_version"],
               "result_reason": saved["result_reason"]}
    return await asyncio.to_thread(mailbox_post, base_url, token, "complete", payload)


async def run_mailbox_cycle(*, driver, store: MailboxStore, journal, check_task,
                            base_url: str, token: str, deadline_seconds: float,
                            sent_versions: dict[str, int],
                            enqueue_manual_resume=None,
                            dispatch_manual_resume=None,
                            schedule: RefreshSchedule | None = None) -> None:
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey) "
        "WHERE k.status IN ['needs_reconciliation', 'error', 'failed'] "
        "RETURN k.source_description AS source_description, "
        "k.source_obs_id AS source_obs_id, k.status AS status, "
        "k.error_kind AS error_kind")
    tracked = set()
    if schedule is not None:
        # Already-tracked active tasks are refreshed by the scheduled check
        # below, which reads the same graph row plus the business result. Here
        # we only admit new ones; re-preparing all of them each cycle is what
        # made the loop a full scan.
        tracked = {r["task_id"] for r in await asyncio.to_thread(store.all_reports)
                   if r.get("state") in ACTIVE_REPORT_STATES}
    for row in rows:
        if tracked and task_uuid(row["source_description"], row["source_obs_id"]) in tracked:
            continue
        await asyncio.to_thread(
            prepare_task_report, store, journal, row,
            deadline_seconds=deadline_seconds,
            manual_resume_available=enqueue_manual_resume is not None)
    await refresh_tracked_reports(
        store=store, journal=journal, check_task=check_task,
        deadline_seconds=deadline_seconds,
        manual_resume_available=enqueue_manual_resume is not None,
        schedule=schedule)
    for report in await asyncio.to_thread(store.all_reports):
        if sent_versions.get(report["task_id"]) == report["version"]:
            continue
        try:
            await asyncio.to_thread(mailbox_post, base_url, token, "report", report)
        except HTTPError as exc:
            if not 400 <= exc.code < 500:
                raise
            # The gateway refused this exact version (2026-10-11: one "stale
            # task report" 400 aborted every cycle -- the reports after it and
            # the human-command claim never ran -- and logged a full traceback
            # every ~12s). Resending the same version cannot succeed; a new
            # version is sent again.
            log.warning("[mailbox:report_rejected] task=%s version=%s http=%s detail=%s",
                        report["task_id"], report["version"], exc.code, _error_detail(exc))
        sent_versions[report["task_id"]] = report["version"]
    response = await asyncio.to_thread(mailbox_post, base_url, token,
                                       "claim", {"business_key": BUSINESS_KEY})
    command = response.get("command")
    if command is not None:
        if not isinstance(command, dict):
            raise RuntimeError("invalid mailbox command")
        await process_command(command, store=store, journal=journal,
                              check_task=check_task, base_url=base_url,
                              token=token, deadline_seconds=deadline_seconds,
                              enqueue_manual_resume=enqueue_manual_resume)
    if dispatch_manual_resume is not None:
        await dispatch_manual_resume()
