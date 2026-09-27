"""Business task mapping and mailbox command dedup survive process restarts."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from utils.reconciliation_mailbox import MailboxStore, task_uuid, ZERO_STEP
from utils.reconciliation_worker import (
    prepare_task_report, process_command, refresh_tracked_reports,
)


class MailboxTests(unittest.TestCase):
    def test_missing_step_is_reported_as_unrecoverable(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            report = prepare_task_report(store, None, {
                "source_description": "source", "source_obs_id": "id-1",
                "status": "failed", "error_kind": "reconciliation_model_step_missing",
            }, deadline_seconds=180)
            self.assertEqual(report["state"], "unrecoverable")
            self.assertEqual(report["reason"], "reconciliation_model_step_missing")
            self.assertEqual(report["model_step_id"], ZERO_STEP)

    def test_task_identity_is_unambiguous_and_versioned(self):
        self.assertNotEqual(task_uuid("a:b", "c"), task_uuid("a", "b:c"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mailbox.sqlite3"
            store = MailboxStore(path)
            fields = dict(step_id=ZERO_STEP, state="reconciliation",
                          failed_attempts=0, retryable=False,
                          reason="business_state_unknown")
            first = store.prepare_report("source", "id-1", **fields)
            self.assertEqual(first["version"], 0)
            reopened = MailboxStore(path)
            self.assertEqual(reopened.lookup_task(first["task_id"]), ("source", "id-1"))
            self.assertEqual(reopened.prepare_report("source", "id-1", **fields)["version"], 0)
            self.assertEqual(reopened.prepare_report(
                "source", "id-1", **{**fields, "state": "failed"})["version"], 1)

    def test_ok_graph_status_without_verified_persistence_is_not_success(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            report = prepare_task_report(store, None, {
                "source_description": "source", "source_obs_id": "id-1",
                "status": "ok", "error_kind": None,
            }, deadline_seconds=180)
            self.assertEqual(report["state"], "reconciliation")
            self.assertEqual(report["reason"], "business_result_unverified")

    def test_manual_resume_outbox_is_durable_single_claim_and_single_active_job(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mailbox.sqlite3"
            store = MailboxStore(path)
            report = store.prepare_report(
                "source", "id-1", step_id="a" * 64, state="reconciliation",
                failed_attempts=1, retryable=True, reason="manual_retry_available")
            command = {"command_id": "manual-1", "task_id": report["task_id"],
                       "model_step_id": "a" * 64, "action": "reconcile"}
            job = store.enqueue_manual_resume(
                command, stage="node_extraction", grant_id="grant-1",
                attempt_epoch="2026-09-25T00:00:00+00:00",
                input_snapshot={"source_description": "source", "source_obs_id": "id-1"},
                created_by_request="request-1", journal_step_id="local-step")
            self.assertEqual(job["state"], "queued")
            reopened = MailboxStore(path)
            self.assertEqual(reopened.manual_resume_job("manual-1")["grant_id"],
                             "grant-1")
            duplicate = reopened.enqueue_manual_resume(
                command, stage="node_extraction", grant_id="grant-1",
                attempt_epoch="2026-09-25T00:00:00+00:00",
                input_snapshot={"source_description": "source", "source_obs_id": "id-1"},
                created_by_request="request-1", journal_step_id="local-step")
            self.assertEqual(duplicate["command_id"], "manual-1")
            claimed = reopened.claim_manual_resume_job()
            self.assertEqual(claimed["state"], "running")
            self.assertIsNone(reopened.claim_manual_resume_job())
            self.assertEqual(reopened.active_manual_resume_job(
                report["task_id"], "a" * 64)["state"], "running")
            reopened.finish_manual_resume_job(
                "manual-1", state="reconciliation", reason="manual_retry_worker_error")
            self.assertIsNone(reopened.active_manual_resume_job(
                report["task_id"], "a" * 64))

    def test_failed_graph_cas_can_discard_only_queued_outbox(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            report = store.prepare_report(
                "source", "id-1", step_id="a" * 64, state="reconciliation",
                failed_attempts=1, retryable=True, reason="manual_retry_available")
            command = {"command_id": "manual-1", "task_id": report["task_id"],
                       "model_step_id": "a" * 64}
            store.enqueue_manual_resume(
                command, stage="edge_phase", grant_id="grant-1",
                attempt_epoch="2026-09-25T00:00:00+00:00",
                input_snapshot={"source_description": "source", "source_obs_id": "id-1"},
                created_by_request="request-1", journal_step_id="local-step")
            self.assertTrue(store.discard_queued_manual_resume("manual-1"))
            self.assertIsNone(store.manual_resume_job("manual-1"))

    def test_deadline_controls_in_flight_vs_failed_attempt_report(self):
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            now = datetime.now(timezone.utc)
            attempt = {"idempotency_key": "key-1", "business_key": "kg_hub.entity_extract",
                       "step_id": "a" * 64, "request_digest": "digest",
                       "stage": "node_extraction",
                       "phase": "unknown", "provider_call_started": None,
                       "result_json": None, "created_at": now.isoformat(),
                       "http_started_at": now.isoformat()}
            journal = Mock()
            journal.task_execution_summary.return_value = {
                "execution_count": 1, "failed_attempts": 1, "active": False,
                "executions": [{"state": "failed"}]}
            journal.find_task.return_value = [attempt]
            journal.gateway_step_for_attempt.return_value = "b" * 64
            journal.resolve_gateway_step.return_value = {
                "local_step_id": attempt["step_id"],
                "request_digest": attempt["request_digest"], "stage": "node_extraction"}
            journal.task_execution_summary.return_value = {
                "execution_count": 1, "failed_attempts": 0, "active": True,
                "executions": [{"state": "running"}]}
            in_flight = prepare_task_report(store, journal, {
                "source_description": "source", "source_obs_id": "id-1",
                "status": "needs_reconciliation", "error_kind": None,
            }, deadline_seconds=180)
            self.assertEqual(in_flight["failed_attempts"], 0)
            self.assertEqual(in_flight["reason"], "model_call_in_flight")

            attempt["http_started_at"] = (now - timedelta(seconds=181)).isoformat()
            journal.task_execution_summary.return_value = {
                "execution_count": 1, "failed_attempts": 1, "active": False,
                "executions": [{"state": "failed"}]}
            expired = prepare_task_report(store, journal, {
                "source_description": "source", "source_obs_id": "id-1",
                "status": "needs_reconciliation", "error_kind": None,
            }, deadline_seconds=180)
            self.assertEqual(expired["failed_attempts"], 1)
            self.assertEqual(expired["reason"], "retry_adapter_unavailable")

    def test_error_row_and_gateway_completed_without_local_receipt_are_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            step = "a" * 64
            attempt = {"idempotency_key": "completed-no-local-receipt",
                       "business_key": "kg_hub.entity_extract", "step_id": step,
                       "request_digest": "digest", "stage": "node_extraction",
                       "phase": "completed", "provider_call_started": True,
                       "result_json": None, "created_at": "2026-09-25T00:00:00+00:00",
                       "http_started_at": "2026-09-25T00:00:00+00:00"}
            journal = Mock()
            journal.task_execution_summary.return_value = {
                "execution_count": 1, "failed_attempts": 1, "active": False,
                "executions": [{"state": "failed"}]}
            journal.find_task.return_value = [attempt]
            journal.gateway_step_for_attempt.return_value = step
            journal.resolve_gateway_step.return_value = {
                "local_step_id": step, "request_digest": "digest",
                "stage": "node_extraction"}
            report = prepare_task_report(store, journal, {
                "source_description": "source", "source_obs_id": "id-1",
                "status": "error", "error_kind": "model_timeout",
            }, deadline_seconds=180, manual_resume_available=True)
            self.assertEqual(report["state"], "reconciliation")
            self.assertEqual(report["failed_attempts"], 1)
            self.assertTrue(report["retryable"])
            self.assertEqual(report["reason"], "manual_retry_available")

    def test_three_task_wide_failures_on_error_row_publish_terminal_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            step = "a" * 64
            attempts = [{
                "idempotency_key": f"failed-{index}",
                "business_key": "kg_hub.entity_extract", "step_id": step,
                "request_digest": "digest", "stage": "node_extraction",
                "phase": "failed", "provider_call_started": True,
                "result_json": None, "created_at": "2026-09-25T00:00:00+00:00",
                "http_started_at": "2026-09-25T00:00:00+00:00",
            } for index in range(3)]
            journal = Mock()
            journal.task_execution_summary.return_value = {
                "execution_count": 3, "failed_attempts": 3, "active": False,
                "executions": [{"state": "failed"}] * 3}
            journal.find_task.return_value = attempts
            journal.gateway_step_for_attempt.return_value = step
            journal.resolve_gateway_step.return_value = {
                "local_step_id": step, "request_digest": "digest",
                "stage": "node_extraction"}
            report = prepare_task_report(store, journal, {
                "source_description": "source", "source_obs_id": "id-1",
                "status": "error", "error_kind": "model_timeout",
            }, deadline_seconds=180)
            self.assertEqual(report["failed_attempts"], 3)
            self.assertEqual(report["state"], "failed")
            self.assertFalse(report["retryable"])
            self.assertEqual(report["reason"], "model_attempts_exhausted")

    def test_duplicate_command_keeps_first_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mailbox.sqlite3"
            store = MailboxStore(path)
            command = {"command_id": "c-1", "task_id": "t-1",
                       "model_step_id": ZERO_STEP, "action": "check"}
            first = store.save_command_result(command, state="reconciliation",
                                              version=2, reason="business_state_unknown")
            reopened = MailboxStore(path)
            second = reopened.save_command_result(command, state="failed",
                                                  version=3, reason="model_attempts_exhausted")
            self.assertEqual(first, second)


class MailboxCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracked_task_reports_queued_running_succeeded_and_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            queued = store.prepare_report(
                "source", "id-1", step_id=ZERO_STEP, state="queued",
                failed_attempts=0, retryable=False, reason="business_task_running")
            check = AsyncMock(side_effect=[
                {"status": "ok", "business_result_persisted": False, "task": {
                    "source_description": "source", "source_obs_id": "id-1",
                    "status": "running", "error_kind": None}},
                {"status": "ok", "business_result_persisted": True, "task": {
                    "source_description": "source", "source_obs_id": "id-1",
                    "status": "ok", "error_kind": None}},
            ])
            running = await refresh_tracked_reports(
                store=store, journal=None, check_task=check, deadline_seconds=180)
            self.assertEqual(running[0]["state"], "running")
            self.assertGreater(running[0]["version"], queued["version"])
            succeeded = await refresh_tracked_reports(
                store=store, journal=None, check_task=check, deadline_seconds=180)
            self.assertEqual(succeeded[0]["state"], "succeeded")
            self.assertEqual(store.read_report(queued["task_id"])["state"], "succeeded")
            self.assertEqual(check.await_count, 2)

    async def test_tracked_task_reports_terminal_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            queued = store.prepare_report(
                "source", "id-1", step_id=ZERO_STEP, state="queued",
                failed_attempts=2, retryable=False, reason="business_task_running")
            check = AsyncMock(return_value={
                "status": "ok", "business_result_persisted": False, "task": {
                    "source_description": "source", "source_obs_id": "id-1",
                    "status": "failed", "error_kind": "model_attempts_exhausted"}})
            reports = await refresh_tracked_reports(
                store=store, journal=None, check_task=check, deadline_seconds=180)
            self.assertEqual(reports[0]["state"], "failed")
            self.assertEqual(reports[0]["reason"], "model_attempts_exhausted")
            self.assertGreater(reports[0]["version"], queued["version"])

    async def test_check_uses_local_business_result_and_reports_terminal_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            report = store.prepare_report(
                "source", "id-1", step_id=ZERO_STEP, state="reconciliation",
                failed_attempts=0, retryable=False, reason="business_state_unknown")
            command = {"command_id": "command-check", "task_id": report["task_id"],
                       "model_step_id": ZERO_STEP, "action": "reconcile",
                       "expected_version": 0, "lease_token": "lease-1"}
            check = AsyncMock(return_value={"status": "ok",
                "business_result_persisted": True, "task": {
                "source_description": "source", "source_obs_id": "id-1",
                "status": "ok", "error_kind": None}})
            with patch("utils.reconciliation_worker.mailbox_post",
                       return_value={"version": 1, "external_calls": 0,
                                     "command": {"state": "done"}}) as post:
                await process_command(
                    command, store=store, journal=None, check_task=check,
                    base_url="http://mailbox.test", token="token",
                    deadline_seconds=180)
            check.assert_awaited_once_with("source", "id-1",
                                           model_step_id=ZERO_STEP)
            self.assertEqual(store.read_report(report["task_id"])["state"], "succeeded")
            self.assertEqual(store.command_result("command-check")["result_state"],
                             "succeeded")
            self.assertGreater(store.command_result("command-check")["result_version"],
                               command["expected_version"])
            self.assertEqual([call.args[2] for call in post.call_args_list],
                             ["report", "complete"])

    async def test_legacy_retry_action_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            report = store.prepare_report(
                "source", "id-1", step_id=ZERO_STEP, state="reconciliation",
                failed_attempts=1, retryable=False, reason="business_state_unknown")
            command = {"command_id": "command-1", "task_id": report["task_id"],
                       "model_step_id": ZERO_STEP, "action": "retry",
                       "expected_version": 0, "lease_token": "lease-1"}
            with self.assertRaisesRegex(RuntimeError, "unsupported mailbox command"):
                await process_command(
                    command, store=store, journal=None, check_task=AsyncMock(),
                    base_url="http://mailbox.test", token="token",
                    deadline_seconds=180)

    async def test_human_check_reports_count_but_never_grants_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            step = "a" * 64
            report = store.prepare_report(
                "source", "id-1", step_id=step, state="reconciliation",
                failed_attempts=0, retryable=False, reason="business_state_unknown")
            command = {"command_id": "command-check", "task_id": report["task_id"],
                       "model_step_id": step, "action": "reconcile",
                       "expected_version": 0, "lease_token": "lease-1"}
            attempt = {"idempotency_key": "key-1", "business_key": "kg_hub.entity_extract",
                       "step_id": step, "request_digest": "digest", "stage": "edge_phase",
                       "phase": "failed",
                       "provider_call_started": 1, "gateway_identity": None,
                       "gateway_http_status": 504, "result_json": None,
                       "created_at": "2026-09-01T00:00:00+00:00",
                       "updated_at": "2026-09-01T00:00:00+00:00",
                       "http_started_at": "2026-09-01T00:00:00+00:00"}
            journal = Mock()
            journal.task_execution_summary.return_value = {
                "execution_count": 1, "failed_attempts": 1, "active": False,
                "executions": [{"state": "failed"}]}
            journal.find_task.return_value = [attempt]
            journal.gateway_step_for_attempt.return_value = step
            journal.resolve_gateway_step.return_value = {
                "local_step_id": step, "request_digest": "digest",
                "stage": "edge_phase"}
            journal.authorize_retry.side_effect = AssertionError("must fail closed")
            check = AsyncMock(return_value={"status": "ok",
                "business_result_persisted": False, "task": {
                "source_description": "source", "source_obs_id": "id-1",
                "status": "needs_reconciliation", "error_kind": None}})
            with patch("utils.reconciliation_worker.mailbox_post",
                       return_value={"version": 1, "external_calls": 0,
                                     "command": {"state": "done"}}):
                await process_command(
                    command, store=store, journal=journal, check_task=check,
                    base_url="http://mailbox.test", token="token",
                    deadline_seconds=180)
            updated = store.read_report(report["task_id"])
            self.assertEqual(updated["failed_attempts"], 1)
            self.assertFalse(updated["retryable"])
            self.assertEqual(updated["reason"], "retry_adapter_unavailable")
            journal.authorize_retry.assert_not_called()
            self.assertGreater(store.command_result("command-check")["result_version"],
                               command["expected_version"])

    async def test_manual_check_queues_exactly_one_safe_retry_callback(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            step = "a" * 64
            report = store.prepare_report(
                "source", "id-1", step_id=step, state="reconciliation",
                failed_attempts=0, retryable=False, reason="business_state_unknown")
            command = {"command_id": "command-reconcile", "task_id": report["task_id"],
                       "model_step_id": step, "action": "reconcile",
                       "expected_version": report["version"], "lease_token": "lease-1"}
            attempt = {"idempotency_key": "key-1", "business_key": "kg_hub.entity_extract",
                       "step_id": step, "request_digest": "digest", "stage": "edge_phase",
                       "phase": "failed", "provider_call_started": True,
                       "result_json": None, "created_at": "2026-09-01T00:00:00+00:00",
                       "http_started_at": "2026-09-01T00:00:00+00:00"}
            journal = Mock()
            journal.task_execution_summary.return_value = {
                "execution_count": 1, "failed_attempts": 1, "active": False,
                "executions": [{"state": "failed"}]}
            journal.find_task.return_value = [attempt]
            journal.gateway_step_for_attempt.return_value = step
            journal.resolve_gateway_step.return_value = {
                "local_step_id": step, "request_digest": "digest",
                "stage": "edge_phase"}
            check = AsyncMock(return_value={"status": "ok",
                "business_result_persisted": False, "task": {
                "source_description": "source", "source_obs_id": "id-1",
                "status": "error", "worker_state": None,
                "error_kind": None}})
            enqueue = AsyncMock(return_value={"state": "queued", "retryable": False,
                                               "reason": "manual_retry_queued"})
            with patch("utils.reconciliation_worker.mailbox_post",
                       return_value={"version": 1, "external_calls": 0,
                                     "command": {"state": "done"}}):
                await process_command(
                    command, store=store, journal=journal, check_task=check,
                    base_url="http://mailbox.test", token="token",
                    deadline_seconds=180, enqueue_manual_resume=enqueue)
            enqueue.assert_awaited_once()
            updated = store.read_report(report["task_id"])
            self.assertEqual(updated["state"], "queued")
            self.assertFalse(updated["retryable"])
            self.assertEqual(updated["reason"], "manual_retry_queued")
            self.assertEqual(store.command_result(command["command_id"])["result_state"],
                             "queued")


if __name__ == "__main__":
    unittest.main()
