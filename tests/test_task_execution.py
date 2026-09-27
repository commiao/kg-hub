"""Exercise the actual worker wrapper with durable SQLite and local graph I/O."""

import ast
import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from model_gateway_client import model_business_task
from utils.model_attempt_journal import ModelAttemptJournal

SERVER = Path(__file__).resolve().parents[1] / "kg_hub_server.py"


class Driver:
    def __init__(self):
        self.row = {"source_description": "source", "source_obs_id": "one",
                    "created_by_request": "original-request", "status": "pending"}

    async def execute_query(self, query, **params):
        if "RETURN k.source_description" in query:
            return [dict(self.row)], None, None
        if "SET k.worker_state = 'running'" in query:
            self.row.update(worker_state="running", worker_execution_id=params["execution_id"])
            return [{"c": 1}], None, None
        if "SET k.status = 'failed'" in query:
            self.row.update(status="failed", worker_state=None)
            return [], None, None
        raise AssertionError(query)


class WorkerExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = ModelAttemptJournal(Path(self.temp.name) / "attempts.sqlite3")
        self.driver = Driver()
        self.body = SimpleNamespace(source_description="source", source_obs_id="one",
                                    name="task", episode_body="content")
        self.worker = AsyncMock(side_effect=self.fail_worker)
        self.active = 0
        tree = ast.parse(SERVER.read_text())
        funcs = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name in {"do_extract", "_run_recorded_extract"}]
        self.ns = {"_extraction_started": self.started,
                   "_extraction_finished": self.finished,
                   "_do_extract_inner": self.worker,
                   "model_business_task": model_business_task,
                   "journal_from_backup_env": lambda: self.journal,
                   "_persisted_business_result": AsyncMock(return_value=False),
                   "MIN_CLIENT_TIMEOUT_SEC": 180, "PREDIGEST_ENABLED": False}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), str(SERVER), "exec"), self.ns)

    def started(self):
        self.active += 1

    def finished(self):
        self.active -= 1

    async def fail_worker(self, *args, **kwargs):
        self.assertEqual(self.active, 1)
        self.driver.row.update(status="error", worker_state=None)
        return "original-result"

    async def run_worker(self, command=None):
        if command:
            self.driver.row.update(status="pending", worker_state="queued",
                                   manual_resume_command_id=command)
        return await self.ns["do_extract"](
            SimpleNamespace(driver=self.driver), self.body,
            datetime(2026, 9, 27, tzinfo=timezone.utc), manual_command_id=command)

    async def test_three_failed_original_worker_runs_are_terminal_and_fourth_cannot_run(self):
        self.assertEqual(await self.run_worker(), "original-result")
        await self.run_worker("manual-2")
        await self.run_worker("manual-3")
        self.assertEqual(self.journal.task_execution_summary("source", "one")["failed_attempts"], 3)
        self.assertEqual(self.driver.row["status"], "failed")
        with self.assertLogs("kg_hub.task_execution", level=logging.ERROR):
            with self.assertRaisesRegex(RuntimeError, "limit exhausted"):
                await self.run_worker("manual-4")
        self.assertEqual(self.worker.await_count, 3)
        self.assertEqual(self.active, 0)

    async def test_record_store_unavailable_does_not_skip_original_ingestion(self):
        def unavailable():
            raise OSError("journal disk unavailable")
        self.ns["journal_from_backup_env"] = unavailable
        with self.assertLogs("kg_hub.task_execution", level=logging.ERROR):
            self.assertEqual(await self.run_worker(), "original-result")
        self.worker.assert_awaited_once()
        self.assertEqual(self.active, 0)

    async def test_missing_original_identity_does_not_skip_normal_ingestion(self):
        self.driver.row.pop("created_by_request")
        with self.assertLogs("kg_hub.task_execution", level=logging.ERROR):
            await self.run_worker()
        self.worker.assert_awaited_once()

    async def test_manual_run_requires_journal_and_never_falls_through(self):
        self.ns["journal_from_backup_env"] = lambda: None
        with self.assertLogs("kg_hub.task_execution", level=logging.ERROR):
            with self.assertRaisesRegex(RuntimeError, "journal unavailable"):
                await self.run_worker("manual-2")
        self.worker.assert_not_awaited()
        self.assertEqual(self.active, 0)

    async def test_only_persisted_business_result_records_success(self):
        self.ns["_persisted_business_result"].return_value = True
        await self.run_worker()
        summary = self.journal.task_execution_summary("source", "one")
        self.assertEqual(summary["failed_attempts"], 0)
        self.assertEqual(summary["executions"][0]["state"], "succeeded")

    async def test_cancellation_remains_cancellation_and_releases_drain_counter(self):
        self.worker.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_worker()
        self.assertEqual(self.active, 0)
        summary = self.journal.task_execution_summary("source", "one")
        self.assertEqual(summary["failed_attempts"], 0)
        self.assertEqual(summary["executions"][0]["state"], "uncertain")

    async def test_duplicate_manual_command_cannot_start_worker_twice(self):
        await self.run_worker()
        await self.run_worker("manual-2")
        with self.assertLogs("kg_hub.task_execution", level=logging.ERROR):
            with self.assertRaisesRegex(RuntimeError, "already started"):
                await self.run_worker("manual-2")
        self.assertEqual(self.worker.await_count, 2)

    async def test_old_execution_cannot_settle_a_replaced_task(self):
        async def replace(*args, **kwargs):
            self.driver.row.update(created_by_request="new-request", status="error",
                                   worker_execution_id="new-request")
        self.worker.side_effect = replace
        self.ns["_persisted_business_result"].return_value = True
        await self.run_worker()
        summary = self.journal.task_execution_summary("source", "one")
        self.assertEqual(summary["executions"][0]["state"], "uncertain")
        self.ns["_persisted_business_result"].assert_not_awaited()
        self.assertEqual(self.driver.row["worker_execution_id"], "new-request")


if __name__ == "__main__":
    unittest.main()
