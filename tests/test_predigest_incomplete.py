"""A split ingest is successful only when all child business results exist."""

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock
from unittest.mock import patch

import kg_refinery


SOURCE = Path(__file__).resolve().parents[1] / "kg_hub_server.py"
TREE = ast.parse(SOURCE.read_text())
FUNCTION = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_predigest_extract")


class Driver:
    def __init__(self):
        self.writes = []

    async def execute_query(self, query, **params):
        self.writes.append((query, params))
        if "RETURN count(e) AS c" in query:
            return [{"c": 0}], None, None
        return [], None, None


class PredigestFailureTests(IsolatedAsyncioTestCase):
    async def test_zero_of_two_children_is_not_business_success(self):
        driver = Driver()
        statuses = []

        async def update(*args, **kwargs):
            statuses.append((args, kwargs))

        async def fail_child(*args, **kwargs):
            raise TimeoutError("model result unavailable")

        module = ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            FUNCTION,
        ], type_ignores=[]))
        namespace = {
            "classify_provenance": lambda body: "firsthand",
            "PREDIGEST_PROMPT": "{max_obs} {body}", "MAX_OBS": 2,
            "_llm_complete": AsyncMock(return_value="split"),
            "parse_observations": lambda raw: [{"type": "fact"}, {"type": "fact"}],
            "_bare_episode_node": AsyncMock(return_value="parent-uuid"),
            "_tag_schema_fields": AsyncMock(),
            "_locked_add_episode": fail_child,
            "obs_to_episode_body": lambda obs, name: "fact",
            "update_ingested_key_status": update,
            "logger": Mock(), "datetime": datetime, "timezone": timezone,
            "GROUP_ID": "kg_hub",
        }
        exec(compile(module, str(SOURCE), "exec"), namespace)
        body = type("Body", (), {
            "source_description": "source", "source_obs_id": "id-1",
            "episode_body": "source content", "name": "episode",
            "provenance": None,
        })()

        handled = await namespace["_predigest_extract"](
            type("Graph", (), {"driver": driver})(), body,
            datetime.now(tz=timezone.utc), "split",
            datetime.now(tz=timezone.utc), "epoch",
        )

        self.assertTrue(handled)  # parent exists; never fall back to whole ingest
        self.assertEqual(len(statuses), 1)
        args, kwargs = statuses[0]
        self.assertEqual(args[3], "needs_reconciliation")
        self.assertEqual(kwargs["error_kind"], "predigest_incomplete")
        self.assertEqual(kwargs["episode_uuid"], "parent-uuid")
        self.assertIn("0/2", kwargs["error_message"])
        self.assertEqual(driver.writes[-1][1]["failed"],
                         ["episode--obs-01", "episode--obs-02"])

    async def test_refinery_holds_incomplete_task_without_resubmission(self):
        row = {"id": 17, "content_hash": "hash", "created_at": "2026-09-25T00:00:00Z",
               "project": "p", "type": "fact", "title": "title"}
        wm = {"ingested": set(), "rejected": set(), "failed": set()}
        calls = []

        async def submit(_obs):
            calls.append(17)
            return "needs_reconciliation"

        cfg = {"shadow_mode": True, "global": {}, "scoring": {},
               "platforms": {"_default": {}}}
        with patch.object(kg_refinery, "ingest_via_api", submit), patch.object(
                kg_refinery, "save_watermark"):
            for cycle in (1, 2):
                await kg_refinery.process_batch(
                    [row], wm, cfg, kg_refinery.QuotaTracker(), {17: True},
                    {}, cycle, "test")

        self.assertEqual(calls, [17])
        self.assertEqual(wm["held"], {17})
        self.assertNotIn(17, wm["ingested"])
        self.assertNotIn(17, wm["failed"])


if __name__ == "__main__":
    import unittest
    unittest.main()
