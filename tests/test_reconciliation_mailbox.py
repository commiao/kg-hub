"""Business task mapping and mailbox command dedup survive process restarts."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from utils.reconciliation_mailbox import MailboxStore, task_uuid, ZERO_STEP
from utils.reconciliation_worker import prepare_task_report, process_command


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
    async def test_check_uses_local_business_result_and_reports_terminal_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            report = store.prepare_report(
                "source", "id-1", step_id=ZERO_STEP, state="reconciliation",
                failed_attempts=0, retryable=False, reason="business_state_unknown")
            command = {"command_id": "command-check", "task_id": report["task_id"],
                       "model_step_id": ZERO_STEP, "action": "check",
                       "expected_version": 0, "lease_token": "lease-1"}
            check = AsyncMock(return_value={"status": "ok", "task": {
                "source_description": "source", "source_obs_id": "id-1",
                "status": "ok", "error_kind": None}})
            with patch("utils.reconciliation_worker.mailbox_post",
                       return_value={"version": 1, "external_calls": 0,
                                     "command": {"state": "done"}}) as post:
                await process_command(
                    command, store=store, journal=None, check_task=check,
                    base_url="http://mailbox.test", token="token",
                    deadline_seconds=180)
            check.assert_awaited_once_with("source", "id-1")
            self.assertEqual(store.read_report(report["task_id"])["state"], "succeeded")
            self.assertEqual(store.command_result("command-check")["result_state"],
                             "succeeded")
            self.assertEqual([call.args[2] for call in post.call_args_list],
                             ["report", "complete"])

    async def test_retry_is_durably_refused_without_business_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            store = MailboxStore(Path(temp) / "mailbox.sqlite3")
            report = store.prepare_report(
                "source", "id-1", step_id=ZERO_STEP, state="reconciliation",
                failed_attempts=1, retryable=False, reason="business_state_unknown")
            command = {"command_id": "command-1", "task_id": report["task_id"],
                       "model_step_id": ZERO_STEP, "action": "retry",
                       "expected_version": 0, "lease_token": "lease-1"}
            check = AsyncMock()
            with patch("utils.reconciliation_worker.mailbox_post",
                       return_value={"version": 1, "external_calls": 0,
                                     "command": {"state": "done"}}) as post:
                await process_command(
                    command, store=store, journal=None, check_task=check,
                    base_url="http://mailbox.test", token="token",
                    deadline_seconds=180)
                await process_command(
                    {**command, "lease_token": "lease-2"}, store=store,
                    journal=None, check_task=check,
                    base_url="http://mailbox.test", token="token",
                    deadline_seconds=180)
            check.assert_not_called()
            self.assertEqual(store.command_result("command-1")["result_reason"],
                             "retry_not_available")
            self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
