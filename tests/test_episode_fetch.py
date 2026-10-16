"""episode_search 的 preview_chars 与 GET /api/episode 的边界：纯函数 + 伪驱动，不连库。"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import json
import unittest
from unittest.mock import patch

from starlette.requests import Request

import kg_hub_server as S


def _request(query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/episode",
                    "headers": [], "query_string": query.encode()})


class _Driver:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def execute_query(self, cypher, **params):
        self.calls.append((cypher, params))
        return self.rows, None, None


class PreviewParamTests(unittest.TestCase):
    def test_default_keeps_the_600_char_contract(self):
        item = {"name": "n", "source": "s", "score": 1.0, "uuid": "u", "content": "x" * 1000}
        payload = S.episode_preview_payload(item, S.bounded_int_param(None, 600, 1, 8000))
        self.assertEqual(len(payload["body_preview"]), 600)
        self.assertEqual(payload["content_chars"], 1000)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["uuid"], "u")

    def test_preview_chars_is_clamped_and_garbage_falls_back(self):
        self.assertEqual(S.bounded_int_param("4000", 600, 1, 8000), 4000)
        self.assertEqual(S.bounded_int_param("999999", 600, 1, 8000), 8000)
        self.assertEqual(S.bounded_int_param("0", 600, 1, 8000), 1)
        self.assertEqual(S.bounded_int_param("abc", 600, 1, 8000), 600)
        self.assertEqual(S.bounded_int_param("", 600, 1, 8000), 600)

    def test_short_content_is_not_marked_truncated(self):
        payload = S.episode_preview_payload({"content": "short"}, 4000)
        self.assertEqual(payload["body_preview"], "short")
        self.assertFalse(payload["truncated"])


class FetchPayloadTests(unittest.TestCase):
    def test_newest_duplicate_wins_and_duplicates_are_counted(self):
        rows = [
            {"name": "doc", "uuid": "old", "content": "old body", "created_at": "2026-08-01T00:00:00"},
            {"name": "doc", "uuid": "new", "content": "new body", "created_at": "2026-10-01T00:00:00"},
            {"name": "doc", "uuid": "none", "content": "no date", "created_at": None},
        ]
        payload = S.episode_fetch_payload(rows, 32_000)
        self.assertEqual(payload["uuid"], "new")
        self.assertEqual(payload["content"], "new body")
        self.assertEqual(payload["duplicates"], 2)
        self.assertFalse(payload["truncated"])

    def test_content_is_bounded_by_max_chars_and_cap(self):
        rows = [{"name": "doc", "uuid": "u", "content": "y" * 70_000, "created_at": "2026-10-01"}]
        payload = S.episode_fetch_payload(rows, 100)
        self.assertEqual(len(payload["content"]), 100)
        self.assertEqual(payload["content_chars"], 70_000)
        self.assertTrue(payload["truncated"])
        capped = S.episode_fetch_payload(rows, 999_999)
        self.assertEqual(len(capped["content"]), S._EPISODE_FETCH_MAX_CHARS)

    def test_no_rows_is_none(self):
        self.assertIsNone(S.episode_fetch_payload([], 100))
        self.assertIsNone(S.episode_fetch_payload([{"content": "nameless"}], 100))


class FetchEndpointTests(unittest.TestCase):
    def _run(self, query: str, rows):
        driver = _Driver(rows)
        with patch.object(S, "get_status_driver", return_value=driver):
            response = asyncio.run(S.episode_fetch(_request(query)))
        return response.status_code, json.loads(response.body), driver

    def test_missing_key_and_bad_uuid_are_400(self):
        status, body, driver = self._run("", [])
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        status, body, _ = self._run("uuid=not-a-uuid", [])
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        self.assertEqual(driver.calls, [])

    def test_lookup_by_name_filters_archived_and_never_bumps(self):
        rows = [{"name": "doc", "uuid": "u", "source": "s", "content": "body", "created_at": "2026-10-01"}]
        status, body, driver = self._run("name=doc&max_chars=2", rows)
        self.assertEqual(status, 200)
        self.assertEqual(body["content"], "bo")
        self.assertTrue(body["truncated"])
        self.assertEqual(len(driver.calls), 1, "read-only: exactly one query, no retrieval bump")
        cypher, params = driver.calls[0]
        self.assertIn("NOT coalesce(n.archived, false)", cypher)
        self.assertIn("n.name = $key", cypher)
        self.assertNotIn("SET ", cypher)
        self.assertEqual(params, {"key": "doc"})

    def test_lookup_by_uuid_is_normalized(self):
        rows = [{"name": "doc", "uuid": "00000000-0000-4000-8000-000000000001", "content": "b"}]
        status, body, driver = self._run("uuid=00000000-0000-4000-8000-000000000001", rows)
        self.assertEqual(status, 200)
        self.assertIn("n.uuid = $key", driver.calls[0][0])

    def test_unknown_episode_is_404(self):
        status, body, _ = self._run("name=missing", [])
        self.assertEqual((status, body["code"]), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()
