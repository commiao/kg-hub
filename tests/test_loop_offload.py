"""SQLite journal and stage-artifact I/O must never run on the event loop."""
import ast
import asyncio
import os
import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils import model_attempt_journal
from utils.task_execution import run_task_execution

ROOT = pathlib.Path(__file__).resolve().parents[1]
OFFLOADED_MODULES = ("utils/graphiti_stage_adapter.py", "utils/graphiti_parallel.py",
                     "utils/ingest_workflow.py", "utils/task_execution.py")
BLOCKING_RECEIVERS = {"store", "journal"}
BLOCKING_FUNCTIONS = {"_stage_record", "StageArtifactStore", "journal_factory"}


def _direct_blocking_calls(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id in BLOCKING_RECEIVERS):
                found.append(f"{path}:{node.lineno} {target.value.id}.{target.attr}")
            elif isinstance(target, ast.Name) and target.id in BLOCKING_FUNCTIONS:
                found.append(f"{path}:{node.lineno} {target.id}")
    return found


class NoLoopSideSQLiteTests(unittest.TestCase):
    def test_async_ingest_paths_offload_every_sqlite_call(self):
        found = [call for path in OFFLOADED_MODULES for call in _direct_blocking_calls(path)]
        self.assertEqual(found, [])


class JournalReuseTests(unittest.TestCase):
    def test_one_journal_per_path_until_its_file_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = os.path.join(tmp, "ingest-backup.jsonl")
            with patch.dict(os.environ, {"KG_HUB_INGEST_BACKUP_PATH": backup}):
                first = model_attempt_journal.journal_from_backup_env()
                self.assertIs(model_attempt_journal.journal_from_backup_env(), first)
                first.path.unlink()
                again = model_attempt_journal.journal_from_backup_env()
                self.assertIsNot(again, first)
                self.assertEqual(again.find_task("sd", "sid"), [])


class _ThreadRecordingJournal:
    def __init__(self):
        self.threads = []

    def _record(self):
        self.threads.append(threading.get_ident())

    def begin_task_execution(self, *args, **kwargs):
        self._record()
        return {"created": True}

    def find_task(self, *args):
        self._record()
        return []

    def queue_business_receipts(self, *args):
        self._record()

    def finish_task_execution(self, *args, **kwargs):
        self._record()

    def task_execution_summary(self, *args):
        self._record()
        return {"failed_attempts": 0}


class TaskExecutionOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_bookkeeping_runs_off_the_loop_thread(self):
        journal = _ThreadRecordingJournal()
        row = {"created_by_request": "req-1", "worker_execution_id": "req-1",
               "status": "error"}

        async def execute_query(*args, **kwargs):
            return [{"c": 1, **row}], None, None

        async def worker():
            return "done"

        async def persisted(*_):
            return False

        result = await run_task_execution(
            driver=SimpleNamespace(execute_query=execute_query), sd="sd", sid="sid",
            worker=worker, journal_factory=lambda: journal,
            business_result_persisted=persisted, deadline_seconds=180)
        self.assertEqual(result, "done")
        self.assertEqual(len(journal.threads), 5)
        self.assertNotIn(threading.get_ident(), journal.threads)


if __name__ == "__main__":
    unittest.main()
