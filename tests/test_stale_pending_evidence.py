"""A crashed split ingest with persisted parent must not be auto-replayed."""

import ast
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "kg_hub_server.py"
TREE = ast.parse(SOURCE.read_text())
FUNCTION = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "cleanup_stuck_jobs")


class Driver:
    def __init__(self, *, parent_exists):
        self.parent_exists = parent_exists
        self.queries = []

    async def execute_query(self, query, **params):
        self.queries.append((query, params))
        if "RETURN k.source_description AS sd" in query:
            return ([{"sd": "source", "sid": "item-1", "name": "episode",
                      "epoch": "2026-09-20T00:00:00+00:00", "stage": "predigest_child_started"}],
                    None, None)
        if "MATCH (e:Episodic)" in query:
            return [{"c": int(self.parent_exists)}], None, None
        return [{"c": 1 if "needs_reconciliation" in query else 0}], None, None


class StalePendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_parent_or_checkpoint_freezes_claim_without_delete(self):
        module = ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            FUNCTION,
        ], type_ignores=[]))
        namespace = {"datetime": datetime, "timezone": timezone,
                     "timedelta": timedelta, "STUCK_THRESHOLD_MIN": 30,
                     "logger": logging.getLogger(__name__)}
        exec(compile(module, str(SOURCE), "exec"), namespace)
        driver = Driver(parent_exists=True)
        graph = type("Graph", (), {"driver": driver})()
        with patch.dict("os.environ", {"KG_HUB_INGEST_BACKUP_PATH": ""}):
            await namespace["cleanup_stuck_jobs"](graph)
        queries = [q for q, _ in driver.queries]
        self.assertTrue(any("SET k.status = 'needs_reconciliation'" in q
                            for q in queries))
        self.assertFalse(any("WHERE k.status = 'pending'" in q and "DELETE k" in q
                             for q in queries))


if __name__ == "__main__":
    unittest.main()
