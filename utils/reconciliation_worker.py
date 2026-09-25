"""Poll human mailbox commands; check is local-only, retry remains disabled."""

from __future__ import annotations

import asyncio
import logging
import re

from utils.model_attempt_journal import summarize_attempts
from utils.reconciliation_mailbox import (
    BUSINESS_KEY, ZERO_STEP, MailboxStore, mailbox_post,
)


log = logging.getLogger("kg_hub.reconciliation")


def _reason(value: object, fallback: str = "business_state_unknown") -> str:
    text = str(value or "").lower()
    return text if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text) else fallback


def prepare_task_report(store: MailboxStore, journal, row: dict,
                        *, deadline_seconds: float) -> dict:
    sd, sid = row["source_description"], row["source_obs_id"]
    attempts = journal.find_task(sd, sid) if journal else []
    summary = summarize_attempts(attempts, deadline_seconds=deadline_seconds)
    open_attempts = [a for a in attempts if not a["result_json"]
                     and a["provider_call_started"] != 0]
    step = open_attempts[-1]["step_id"] if open_attempts else ZERO_STEP
    failed = min(3, summary["failed_calls_by_step"].get(step, 0))
    raw_status = row.get("status")
    state = {"needs_reconciliation": "reconciliation", "failed": "failed",
             "ok": "succeeded"}.get(raw_status, "reconciliation")
    return store.prepare_report(
        sd, sid, step_id=step, state=state,
        failed_attempts=failed, retryable=False,
        reason=_reason(row.get("error_kind"),
                       "business_result_persisted" if state == "succeeded"
                       else "business_state_unknown"))


async def process_command(command: dict, *, store: MailboxStore, journal,
                          check_task, base_url: str, token: str,
                          deadline_seconds: float) -> dict:
    """Dedup by command_id before delivering the mailbox completion."""
    for field in ("command_id", "task_id", "model_step_id", "action",
                  "expected_version", "lease_token"):
        if field not in command:
            raise RuntimeError(f"mailbox command missing {field}")
    if command["action"] not in {"check", "retry"}:
        raise RuntimeError("unsupported mailbox command")
    saved = store.command_result(command["command_id"])
    if saved is None:
        identity = store.lookup_task(command["task_id"])
        report = store.read_report(command["task_id"])
        if identity is None or report is None:
            state, version, reason = "unrecoverable", max(0, int(command["expected_version"])), "task_not_found"
        elif (report["model_step_id"] != command["model_step_id"]
              or report["version"] != command["expected_version"]):
            state, version, reason = report["state"], report["version"], "command_stale"
        elif command["action"] == "retry":
            # A grant cannot be executed safely until Graphiti continuation is
            # proven. Do not call the model or original ingest endpoint here.
            state, version, reason = report["state"], report["version"], "retry_not_available"
        else:
            result = await check_task(*identity)
            if result.get("status") != "ok" or not isinstance(result.get("task"), dict):
                raise RuntimeError("business status check unavailable")
            updated = prepare_task_report(store, journal, result["task"],
                                          deadline_seconds=deadline_seconds)
            await asyncio.to_thread(mailbox_post, base_url, token,
                                    "report", updated)
            state, version, reason = (updated["state"], updated["version"],
                                      updated["reason"])
        saved = store.save_command_result(command, state=state,
                                          version=version, reason=reason)
    payload = {"business_key": BUSINESS_KEY,
               "command_id": command["command_id"],
               "lease_token": command["lease_token"],
               "result_state": saved["result_state"],
               "result_version": saved["result_version"],
               "result_reason": saved["result_reason"]}
    return await asyncio.to_thread(mailbox_post, base_url, token, "complete", payload)


async def run_mailbox_cycle(*, driver, store: MailboxStore, journal, check_task,
                            base_url: str, token: str, deadline_seconds: float,
                            sent_versions: dict[str, int]) -> None:
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey) "
        "WHERE k.status IN ['needs_reconciliation', 'failed'] "
        "RETURN k.source_description AS source_description, "
        "k.source_obs_id AS source_obs_id, k.status AS status, "
        "k.error_kind AS error_kind")
    for row in rows:
        prepare_task_report(store, journal, row,
                            deadline_seconds=deadline_seconds)
    for report in store.all_reports():
        if sent_versions.get(report["task_id"]) == report["version"]:
            continue
        await asyncio.to_thread(mailbox_post, base_url, token, "report", report)
        sent_versions[report["task_id"]] = report["version"]
    response = await asyncio.to_thread(mailbox_post, base_url, token,
                                       "claim", {"business_key": BUSINESS_KEY})
    command = response.get("command")
    if command is not None:
        if not isinstance(command, dict):
            raise RuntimeError("invalid mailbox command")
        await process_command(command, store=store, journal=journal,
                              check_task=check_task, base_url=base_url,
                              token=token, deadline_seconds=deadline_seconds)
