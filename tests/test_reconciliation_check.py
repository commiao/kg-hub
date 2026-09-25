"""The operator check reads evidence and never creates another model call."""

import ast
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock

from utils.model_attempt_journal import summarize_attempts


SOURCE = Path(__file__).resolve().parents[1] / "kg_hub_server.py"
TREE = ast.parse(SOURCE.read_text())
NAMES = {"_reconciliation_task_view", "_persisted_business_result",
         "ingest_reconciliation_check"}
FUNCTIONS = [n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in NAMES]


class Response:
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code


class Request:
    async def json(self):
        return {"source_description": "source", "source_obs_id": "id-1"}


class Driver:
    def __init__(self, *, episode_uuid=None, name="episode"):
        self.episode_uuid = episode_uuid
        self.name = name
        self.writes = []

    async def execute_query(self, query, **params):
        if "RETURN k.source_description AS source_description" in query:
            return [{"source_description": "source", "source_obs_id": "id-1",
                     "status": "needs_reconciliation", "episode_uuid": self.episode_uuid,
                     "name": self.name, "stage": None, "predigest_children": None}], None, None
        if "MATCH (e:Episodic {uuid:" in query:
            return [{"c": 1}], None, None
        self.writes.append(query)
        return [{"c": 1}], None, None


class Journal:
    def __init__(self, attempts):
        self.attempts = attempts

    def find_task(self, sd, sid):
        return self.attempts

    def update_gateway_status(self, key, status):
        raise AssertionError("gateway status should not be fetched in these cases")


def attempt(index, started):
    return {"idempotency_key": f"key-{index}", "business_key": "kg_hub.entity_extract",
            "step_id": "same-step", "request_digest": "request-hash",
            "phase": "failed" if started else "unknown",
            "provider_call_started": started, "result_json": None,
            "created_at": "2026-09-25T00:00:00+00:00"}


class ReconciliationCheckTests(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, driver, journal):
        module = ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *FUNCTIONS,
        ], type_ignores=[]))
        namespace = {
            "JSONResponse": Response, "get_status_driver": lambda: driver,
            "journal_from_backup_env": lambda: journal,
            "summarize_attempts": summarize_attempts,
            "MIN_CLIENT_TIMEOUT_SEC": 180,
            "datetime": datetime, "timezone": timezone,
            "MAX_OBS": 20, "asyncio": Mock(),
        }
        exec(compile(module, str(SOURCE), "exec"), namespace)
        return await namespace["ingest_reconciliation_check"](Request())

    async def test_persisted_business_result_marks_success(self):
        driver = Driver(episode_uuid="episode-uuid")
        response = await self.run_check(driver, Journal([]))
        self.assertEqual(response.data["task"]["status"], "ok")
        self.assertTrue(response.data["business_result_persisted"])
        self.assertEqual(len(driver.writes), 1)
        self.assertIn("SET k.status = 'ok'", driver.writes[0])

    async def test_three_proven_failed_calls_mark_failed(self):
        driver = Driver()
        response = await self.run_check(driver, Journal([attempt(i, 1) for i in range(3)]))
        self.assertEqual(response.data["task"]["status"], "failed")
        self.assertFalse(response.data["business_result_persisted"])
        self.assertEqual(response.data["task"]["max_failed_calls"], 3)

    async def test_unknown_admission_freezes_terminal_transition(self):
        driver = Driver()
        response = await self.run_check(driver, Journal([attempt(i, None) for i in range(3)]))
        self.assertEqual(response.data["task"]["status"], "needs_reconciliation")
        self.assertTrue(response.data["task"]["admission_unknown"])
        self.assertEqual(driver.writes, [])

    async def test_three_locally_started_timeouts_are_terminal_without_gateway_arm(self):
        driver = Driver()
        attempts = [attempt(i, None) for i in range(3)]
        for row in attempts:
            row["http_started_at"] = "2026-09-25T00:00:00+00:00"
        response = await self.run_check(driver, Journal(attempts))
        self.assertEqual(response.data["task"]["status"], "failed")
        self.assertEqual(response.data["task"]["max_failed_calls"], 3)
        self.assertTrue(response.data["task"]["admission_unknown"])
        self.assertFalse(response.data["task"]["unknown_without_http_evidence"])

    async def test_saved_model_answer_does_not_mark_business_success_or_failure(self):
        driver = Driver()
        rows = [attempt(1, 1)]
        rows[0]["phase"] = "completed"
        rows[0]["result_json"] = '{"answer":"saved"}'
        rows[0]["http_started_at"] = "2026-09-25T00:00:00+00:00"
        response = await self.run_check(driver, Journal(rows))
        self.assertEqual(response.data["task"]["status"], "needs_reconciliation")
        self.assertFalse(response.data["business_result_persisted"])
        self.assertEqual(response.data["task"]["max_failed_calls"], 0)
        self.assertEqual(driver.writes, [])

    async def test_missing_model_step_is_unrecoverable_failed_list_item(self):
        driver = Driver()
        response = await self.run_check(driver, Journal([]))
        self.assertEqual(response.data["task"]["status"], "failed")
        self.assertEqual(response.data["task"]["error_kind"],
                         "reconciliation_model_step_missing")
        self.assertIn("$reason", driver.writes[0])

    async def test_unconfigured_journal_is_service_unavailable_not_unrecoverable(self):
        driver = Driver()
        response = await self.run_check(driver, None)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "journal_unavailable")
        self.assertEqual(driver.writes, [])

    async def test_missing_source_name_is_unrecoverable(self):
        driver = Driver(name=None)
        response = await self.run_check(driver, Journal([attempt(1, 1)]))
        self.assertEqual(response.data["task"]["status"], "failed")
        self.assertEqual(response.data["task"]["error_kind"],
                         "reconciliation_source_identity_missing")

    async def test_invalid_step_identity_is_unrecoverable(self):
        row = attempt(1, None)
        row["step_id"] = ""
        driver = Driver()
        response = await self.run_check(driver, Journal([row]))
        self.assertEqual(response.data["task"]["status"], "failed")
        self.assertEqual(response.data["task"]["error_kind"],
                         "reconciliation_model_step_identity_missing")

    async def test_in_flight_call_keeps_missing_source_held(self):
        row = attempt(1, 1)
        row["phase"] = "admitted"
        row["created_at"] = datetime.now(timezone.utc).isoformat()
        driver = Driver(name=None)
        response = await self.run_check(driver, Journal([row]))
        self.assertEqual(response.data["task"]["status"], "needs_reconciliation")
        self.assertEqual(driver.writes, [])


if __name__ == "__main__":
    unittest.main()
