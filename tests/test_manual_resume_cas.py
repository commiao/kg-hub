"""Manual resumption claims the original IngestedKey with a conditional CAS."""

import ast
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import AsyncMock


SOURCE = Path(__file__).resolve().parents[1] / "kg_hub_server.py"
TREE = ast.parse(SOURCE.read_text())
FUNCTIONS = [node for node in TREE.body
             if isinstance(node, ast.AsyncFunctionDef)
             and node.name == "_cas_manual_resume_claim"]


class ManualResumeCasTests(unittest.IsolatedAsyncioTestCase):
    def namespace(self):
        module = ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *FUNCTIONS,
        ], type_ignores=[]))
        namespace = {"datetime": datetime, "timezone": timezone}
        exec(compile(module, str(SOURCE), "exec"), namespace)
        return namespace

    async def test_claim_requires_original_reconciliation_row_and_request_id(self):
        driver = type("Driver", (), {"execute_query": AsyncMock(
            return_value=([{"c": 1}], None, None))})()
        cas = self.namespace()["_cas_manual_resume_claim"]
        self.assertTrue(await cas(driver, sd="source", sid="id-1",
                                  request_id="request-1", command_id="manual-1"))
        query, params = driver.execute_query.await_args.args[0], driver.execute_query.await_args.kwargs
        self.assertIn("k.status IN ['needs_reconciliation', 'error']", query)
        self.assertIn("k.created_by_request = $request_id", query)
        self.assertIn("k.worker_state IS NULL", query)
        self.assertEqual(params["request_id"], "request-1")
        self.assertEqual(params["command_id"], "manual-1")

    async def test_redelivery_accepts_only_same_command_pending_claim(self):
        driver = type("Driver", (), {"execute_query": AsyncMock(side_effect=[
            ([{"c": 0}], None, None),
            ([{"status": "pending", "worker_state": "queued",
               "command_id": "manual-1", "request_id": "request-1"}], None, None),
        ])})()
        cas = self.namespace()["_cas_manual_resume_claim"]
        self.assertTrue(await cas(driver, sd="source", sid="id-1",
                                  request_id="request-1", command_id="manual-1"))

        changed_driver = type("Driver", (), {"execute_query": AsyncMock(side_effect=[
            ([{"c": 0}], None, None),
            ([{"status": "pending", "worker_state": "queued",
               "command_id": "other-command", "request_id": "request-1"}], None, None),
        ])})()
        self.assertFalse(await cas(changed_driver, sd="source", sid="id-1",
                                   request_id="request-1", command_id="manual-1"))

    async def test_missing_original_request_id_fails_without_graph_write(self):
        driver = type("Driver", (), {"execute_query": AsyncMock()})()
        cas = self.namespace()["_cas_manual_resume_claim"]
        self.assertFalse(await cas(driver, sd="source", sid="id-1",
                                   request_id=None, command_id="manual-1"))
        driver.execute_query.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
