"""/api/ingest_extracted 处理函数：校验、幂等分支、成功响应带纪律指标与来源标记。

kg_hub_server 一加载就要 graphiti_core，本机没有时整文件跳过（与 test_kg_eval_edge_path 同一做法）；
在 NAS 容器或带 venv 的环境里真正执行。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import json
import unittest
from unittest import mock

try:
    import graphiti_core  # noqa: F401
    import kg_hub_server as srv
    HAVE = True
except Exception:  # noqa: BLE001
    HAVE = False

from starlette.requests import Request


def _post(payload: dict) -> Request:
    body = json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/api/ingest_extracted",
                    "headers": [], "query_string": b""}, receive)


GOOD = {"name": "claude-mem-obs-1", "episode_body": "body", "source_description": "claude-mem obs id=1",
        "reference_time": "2026-10-17T00:00:00Z", "source_obs_id": "h1", "extractor_model": "claude-fable-5-1",
        "entities": [{"name": "kg-hub", "type": "Project", "summary": ""},
                     {"name": "commit abc1234", "type": "tool", "summary": ""}],
        "edges": [{"source": "kg-hub", "name": "HAS", "target": "commit abc1234", "fact": "f"}]}


@unittest.skipUnless(HAVE, "需要 graphiti_core（容器或 venv）")
class HandlerTest(unittest.TestCase):
    def _run(self, payload, **patches):
        class FakeDriver:
            async def execute_query(self, cypher, **params):
                return [{"error_message": "boom"}], None, None

        class FakeG:
            driver = FakeDriver()
            embedder = None

        async def get_g():
            return FakeG()
        merge = patches.pop("merge", {"newly_created": True, "status": "pending", "episode_uuid": None,
                                      "execution_epoch": "e", "created_at": "e"})

        async def fake_merge(*a, **k):
            return merge
        write_result = patches.pop("write_result", srv.write_extraction.__globals__["WriteResult"](
            episode_uuid="ep", nodes_new=2, nodes_reused=0, edges_new=1, edges_reused=0,
            resolved={"kg-hub": {"uuid": "u1", "reused": False, "matched_name": None}}))

        async def fake_run(g, body, extraction, ref_time):
            self.captured = (body, extraction)
            return write_result

        async def noop(*a, **k):
            return None
        with mock.patch.object(srv, "get_graphiti", get_g), \
                mock.patch.object(srv, "_backup_episode", lambda *a, **k: None), \
                mock.patch.object(srv, "cleanup_stuck_jobs", noop), \
                mock.patch.object(srv, "merge_or_get_ingested_key", fake_merge), \
                mock.patch.object(srv, "_run_recorded_extracted", fake_run), \
                mock.patch.object(srv, "drain_seconds_left", lambda: 0):
            resp = asyncio.run(srv.ingest_extracted(_post(payload)))
        return resp.status_code, json.loads(resp.body)

    def test_ok_response_carries_stats_and_model(self):
        code, d = self._run(GOOD)
        self.assertEqual(code, 200, d)
        self.assertEqual((d["status"], d["episode_uuid"], d["nodes_new"], d["edges_new"]), ("ok", "ep", 2, 1))
        self.assertEqual(d["extractor_model"], "claude-fable-5-1")
        self.assertEqual((d["stats"]["offschema_edge"], d["stats"]["dirty_name"]), (1, 1))
        body, extraction = self.captured
        self.assertEqual(body.extractor_model, "claude-fable-5-1")
        self.assertEqual([e.type for e in extraction.entities], ["Project", "Tool"])
        self.assertEqual(extraction.edges[0].name, "relates_to")

    def test_validation_errors(self):
        code, d = self._run(dict(GOOD, extractor_model="bad model!"))
        self.assertEqual((code, d["code"]), (400, "bad_extractor_model"))
        code, d = self._run(dict(GOOD, entities=[]))
        self.assertEqual((code, d["code"]), (400, "empty_extraction"))
        code, d = self._run({k: v for k, v in GOOD.items() if k != "entities"})
        self.assertEqual((code, d["code"]), (400, "bad_schema"))
        code, d = self._run(dict(GOOD, reference_time="yesterday"))
        self.assertEqual((code, d["code"]), (400, "bad_reference_time"))

    def test_duplicate_and_error_branches(self):
        code, d = self._run(GOOD, merge={"newly_created": False, "status": "ok", "episode_uuid": "old"})
        self.assertEqual((code, d["status"], d["episode_uuid"]), (200, "skipped", "old"))
        code, d = self._run(GOOD, merge={"newly_created": False, "status": "error", "episode_uuid": None,
                                         "error_message": "previous"})
        self.assertEqual((code, d["code"]), (409, "previous_attempt_error"))
        code, d = self._run(GOOD, write_result=None)
        self.assertEqual((code, d["code"], d["message"]), (500, "ingest_failed", "boom"))


if __name__ == "__main__":
    unittest.main()
