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
    from utils.extracted_ingest import WriteResult
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
        write_result = patches.pop("write_result", WriteResult(
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

    def _run_empty(self, payload, merge):
        """空观测：只建 / 复用 IngestedKey、记 status=empty，不写图、不备份正文。"""
        calls = {"status": [], "cypher": [], "backup": 0, "run": 0}

        class FakeDriver:
            async def execute_query(self, cypher, **params):
                calls["cypher"].append((cypher, params))
                return [], None, None

        class FakeG:
            driver = FakeDriver()
            embedder = None

        async def get_g():
            return FakeG()

        async def fake_merge(*a, **k):
            return merge

        async def fake_status(g, sd, sid, status, **k):
            calls["status"].append(status)

        async def fake_run(*a, **k):
            calls["run"] += 1

        def backup(*a, **k):
            calls["backup"] += 1
        with mock.patch.object(srv, "get_graphiti", get_g), \
                mock.patch.object(srv, "_backup_episode", backup), \
                mock.patch.object(srv, "merge_or_get_ingested_key", fake_merge), \
                mock.patch.object(srv, "update_ingested_key_status", fake_status), \
                mock.patch.object(srv, "_run_recorded_extracted", fake_run), \
                mock.patch.object(srv, "drain_seconds_left", lambda: 0):
            resp = asyncio.run(srv.ingest_extracted(_post(payload)))
        return resp.status_code, json.loads(resp.body), calls

    def test_empty_observation_is_recorded_as_empty_key_without_graph_writes(self):
        """2026-10-24：空观测原先服务端没有任何记录，refinery 对账认不出、一直挂在「剩余」里。"""
        base = dict(GOOD, entities=[], edges=[], empty=True, extractor_tool="codex", run_id="r1")
        new = {"newly_created": True, "status": "pending", "episode_uuid": None}
        code, d, calls = self._run_empty(base, new)
        self.assertEqual((code, d["status"], d["duplicate"]), (200, "empty_recorded", False))
        self.assertEqual(calls["status"], ["empty"])
        self.assertEqual((calls["backup"], calls["run"]), (0, 0))                     # 不备份正文、不写图
        tag = [p for c, p in calls["cypher"] if "empty_by_model" in c][0]
        self.assertEqual((tag["model"], tag["tool"], tag["run_id"]), ("claude-fable-5-1", "codex", "r1"))
        # 再交一次：幂等，不重复改状态
        code, d, calls = self._run_empty(base, {"newly_created": False, "status": "empty", "episode_uuid": None})
        self.assertEqual((code, d["status"], d["duplicate"], calls["status"]), (200, "empty_recorded", True, []))
        # 已入库 / 正在入库 / 之前失败：不覆盖
        for st, want in (("ok", (200, "skipped")), ("pending", (202, "in_progress")), ("error", (409, "error"))):
            code, d, calls = self._run_empty(base, {"newly_created": False, "status": st, "episode_uuid": "e"})
            self.assertEqual(((code, d["status"]), calls["status"]), (want, []))

    def test_empty_flag_rejects_content_and_dry_run_writes_nothing(self):
        new = {"newly_created": True, "status": "pending", "episode_uuid": None}
        code, d, calls = self._run_empty(dict(GOOD, empty=True), new)                 # 带实体还标 empty
        self.assertEqual((code, d["code"], calls["status"]), (400, "empty_with_content", []))
        code, d, calls = self._run_empty(dict(GOOD, entities=[], edges=[], empty=True, dry_run=True), new)
        self.assertEqual((code, d["status"], calls["status"], calls["cypher"]), (200, "dry_run_empty", [], []))
        code, d = self._run(dict(GOOD, entities=[], edges=[]))                         # 没标 empty 的空提交照旧拒收
        self.assertEqual((code, d["code"]), (400, "empty_extraction"))

    def test_dry_run_passes_pending_and_reports_new_entities(self):
        """2026-10-22 补交 b0024：dry-run 带上同批前面条目的新实体，响应给出本条会新建哪些。"""
        seen = {}
        async def fake_write(driver, embedder, extraction, **kw):
            seen.update(kw)
            return WriteResult(episode_uuid="", nodes_new=1, nodes_reused=1, edges_new=0, edges_reused=0,
                      resolved={"kg-hub": {"uuid": None, "reused": False, "matched_name": None},
                                "commit abc1234": {"uuid": None, "reused": True, "matched_name": "Commit ABC1234",
                                                   "matched_type": "Tool", "match": "exact", "in_batch": True}})
        payload = dict(GOOD, dry_run=True, dry_run_pending=[{"name": "Commit ABC1234", "type": "Tool"}])
        with mock.patch.object(srv, "write_extraction", fake_write):
            code, d = self._run(payload)
        self.assertEqual(code, 200)
        self.assertEqual(seen["pending"], [{"name": "Commit ABC1234", "type": "Tool"}])
        self.assertTrue(seen["dry_run"])
        self.assertEqual(d["new_entities"], [{"name": "kg-hub", "type": "Project"}])
        self.assertTrue(d["resolved"]["commit abc1234"]["in_batch"])
        self.assertNotIn("in_batch", d["resolved"]["kg-hub"])
        # 每个实体一行：新建 / 复用到谁（名字、类型、怎么命中），同批新建的标 in_batch
        self.assertEqual(d["entities"], [
            {"name": "kg-hub", "type": "Project", "reused": False},
            {"name": "commit abc1234", "type": "Tool", "reused": True, "matched_name": "Commit ABC1234",
             "matched_type": "Tool", "match": "exact", "in_batch": True}])

    def test_dry_run_entities_carry_cross_type_graph_match(self):
        async def fake_write(driver, embedder, extraction, **kw):
            return WriteResult(episode_uuid="", nodes_new=0, nodes_reused=2, edges_new=0, edges_reused=0, resolved={
                "kg-hub": {"uuid": "u1", "reused": True, "matched_name": "kg-hub", "matched_type": "Tool",
                           "match": "exact", "matched_summary": "另一个同名工具"},
                "commit abc1234": {"uuid": "u2", "reused": True, "matched_name": "commit abc1234",
                                   "matched_type": None, "match": "exact"}})
        with mock.patch.object(srv, "write_extraction", fake_write):
            code, d = self._run(dict(GOOD, dry_run=True))
        self.assertEqual(code, 200)
        self.assertEqual(d["new_entities"], [])
        self.assertEqual(d["entities"][0], {"name": "kg-hub", "type": "Project", "reused": True, "matched_name": "kg-hub",
                                            "matched_type": "Tool", "match": "exact", "matched_summary": "另一个同名工具"})
        self.assertEqual(d["entities"][1]["matched_type"], None)       # 旧节点没有类型标签：照实给 None
        self.assertNotIn("in_batch", d["entities"][1])

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



@unittest.skipUnless(HAVE, "需要 graphiti_core（容器或 venv）")
class ProvenanceFieldsHandlerTest(HandlerTest):
    def test_optional_tool_and_run_id_pass_through(self):
        code, d = self._run(dict(GOOD, extractor_tool="codex", run_id="20261019-codex-s50"))
        self.assertEqual(code, 200, d)
        self.assertEqual((d["extractor_tool"], d["run_id"]), ("codex", "20261019-codex-s50"))
        body, _ = self.captured
        self.assertEqual((body.extractor_tool, body.run_id), ("codex", "20261019-codex-s50"))
        # 旧客户端不带这两个字段：仍然成功，响应里为 null
        code, d = self._run(GOOD)
        self.assertEqual((code, d["extractor_tool"], d["run_id"]), (200, None, None))
        self.assertEqual((self.captured[0].extractor_tool, self.captured[0].run_id), (None, None))

    def test_bad_tool_or_run_id_rejected(self):
        code, d = self._run(dict(GOOD, extractor_tool="bad tool!"))
        self.assertEqual((code, d["code"]), (400, "bad_extractor_tool"))
        code, d = self._run(dict(GOOD, run_id="has space"))
        self.assertEqual((code, d["code"]), (400, "bad_run_id"))


if __name__ == "__main__":
    unittest.main()
