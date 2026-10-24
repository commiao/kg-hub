"""HTTP repair authentication, real gate integration and lock coverage."""
import asyncio
from contextlib import asynccontextmanager
import json
import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request
from starlette.responses import JSONResponse

from tests.test_ingest_extracted_handler import HAVE, GOOD
from tests.test_extraction_repair import Driver, Embedder
from tests import test_extraction_repair as repair_tests

if HAVE:
    import kg_hub_server as srv


def request(payload=None, path="/api/extraction/repair", method="POST", token=None, query=b""):
    data = json.dumps(payload).encode()
    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    headers = [(b"authorization", ("Bearer " + token).encode())] if token else []
    return Request({"type": "http", "method": method, "path": path, "scheme": "http",
                    "headers": headers, "query_string": query}, receive)


@unittest.skipUnless(HAVE, "needs graphiti_core dependency environment")
class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_protocol_missing_review_rejected_before_graph_io(self):
        g = AsyncMock()
        with patch.object(srv, "get_graphiti", g), patch.object(srv, "drain_seconds_left", return_value=0):
            response = await srv.ingest_extracted(request({**GOOD, "prompt_version": "agent-v5"}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["code"], "quality_review_failed")
        g.assert_not_awaited()

    async def test_extract_scope_allows_only_v5_reviewed_submission_and_no_repair(self):
        from tests import test_extraction_quality as quality_tests
        middleware = srv.BearerAuthMiddleware(None)
        principal = {"name": "extractor@device", "scopes": ["extract"]}
        async def invoke(payload):
            with patch.object(srv, "API_TOKEN", "unit-admin"), \
                    patch.object(srv.token_auth, "principal_for", return_value=principal):
                return await middleware.dispatch(request(payload, path="/api/ingest_extracted", token="unit-extract"), srv.ingest_extracted)
        graph = AsyncMock()
        with patch.object(srv, "get_graphiti", graph), patch.object(srv, "drain_seconds_left", return_value=0):
            response = await invoke({**GOOD, "prompt_version": "agent-v4"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(json.loads(response.body)["code"], "extract_protocol_required")
            response = await invoke({**GOOD, "prompt_version": "agent-v5"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(json.loads(response.body)["code"], "quality_review_failed")
            graph.assert_not_awaited()
        batch, results, review = quality_tests.fixture()
        payload = quality_tests.payload(batch, results, review)
        payload["dry_run"] = True
        from utils.extracted_ingest import WriteResult
        graph = AsyncMock()
        with patch.object(srv, "get_graphiti", AsyncMock(return_value=graph)), \
                patch.object(srv, "write_extraction", AsyncMock(return_value=WriteResult(episode_uuid=None, nodes_new=2, nodes_reused=0, edges_new=3, edges_reused=0, resolved={"worker": {"uuid": "worker", "reused": False, "matched_name": None}, "provider": {"uuid": "provider", "reused": False, "matched_name": None}}))), \
                patch.object(srv, "drain_seconds_left", return_value=0):
            response = await invoke(payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["status"], "dry_run")
        for path, method in (("/api/extraction/repair", "POST"), ("/api/ingest", "POST")):
            with patch.object(srv, "API_TOKEN", "unit-admin"), \
                    patch.object(srv.token_auth, "principal_for", return_value=principal):
                next_call = AsyncMock()
                response = await middleware.dispatch(request({}, path=path, method=method, token="unit-extract"), next_call)
                self.assertEqual(response.status_code, 403)
                next_call.assert_not_awaited()

    async def test_capabilities_are_real_protocol_not_plain_health(self):
        response = await srv.extraction_capabilities(request(method="GET"))
        body = json.loads(response.body)
        self.assertEqual(body["quality_versions"], ["extraction-quality-v1"])
        self.assertEqual(body["prompt_versions"], ["agent-v5"])
        self.assertTrue(body["repair"])
        self.assertEqual(len(body["revision"]), 64)
        self.assertEqual(body["repair_paths"]["apply"], "/api/extraction/repair")

    async def test_read_token_can_preview_and_read_capability_but_never_apply(self):
        middleware = srv.BearerAuthMiddleware(None)
        for path, method, expected in (
            ("/api/extraction/capabilities", "GET", 200),
            ("/api/extraction/repair/preview", "POST", 200),
            ("/api/extraction/repair", "GET", 200),
            ("/api/extraction/repair", "POST", 403),
        ):
            with self.subTest(path=path, method=method):
                next_call = AsyncMock(return_value=JSONResponse({"status": "ok"}))
                with patch.object(srv, "API_TOKEN", "unit-admin"), patch.object(srv.token_auth, "principal_for", return_value={"name": "reader", "scopes": ["read"]}):
                    response = await middleware.dispatch(request(path=path, method=method, token="unit-read"), next_call)
                self.assertEqual(response.status_code, expected)
                self.assertEqual(next_call.await_count, int(expected == 200))

    async def test_missing_auth_is_rejected(self):
        middleware = srv.BearerAuthMiddleware(None)
        next_call = AsyncMock()
        response = await middleware.dispatch(request(), next_call)
        self.assertEqual(response.status_code, 401)
        next_call.assert_not_awaited()

    async def test_preview_source_is_readonly(self):
        from types import SimpleNamespace
        g = SimpleNamespace(driver=Driver(), embedder=Embedder())
        with patch.object(srv, "get_graphiti", AsyncMock(return_value=g)):
            response = await srv.extraction_repair_preview(request({"source_description": "source", "source_obs_id": "one"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(g.driver.mutations, [])
        self.assertEqual(json.loads(response.body)["snapshot"]["key"]["status"], "ok")

    async def test_apply_uses_shared_writer_lock_for_every_mutation(self):
        from types import SimpleNamespace
        g = SimpleNamespace(driver=Driver(), embedder=Embedder())
        doc = await repair_tests.RepairTests().request(g)
        active, owners = False, []
        @asynccontextmanager
        async def lock(**kw):
            nonlocal active
            owners.append(kw["owner"])
            active = True
            try:
                yield
            finally:
                active = False
        original = g.driver.execute_query
        async def query(sql, **params):
            if "/* repair:" in sql and not any(k in sql for k in ("snapshot-", "audit-read")):
                self.assertTrue(active, "repair mutation escaped shared writer lock")
            return await original(sql, **params)
        g.driver.execute_query = query
        with patch.object(srv, "get_graphiti", AsyncMock(return_value=g)), \
                patch.object(srv, "_ingest_execution_lock", lock), \
                patch.object(srv, "validate_payload_quality", return_value=[]), \
                patch.object(srv, "drain_seconds_left", return_value=0):
            response = await srv.extraction_repair_apply(request(doc))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(owners, ["extraction-repair"])
        self.assertFalse(active)
        self.assertNotIn("wrong", g.driver.edges)

    async def test_draining_and_bad_request_do_not_start_graph_writes(self):
        graph = AsyncMock()
        with patch.object(srv, "get_graphiti", graph), patch.object(srv, "drain_seconds_left", return_value=5):
            response = await srv.extraction_repair_apply(request({}))
        self.assertEqual(response.status_code, 503)
        graph.assert_not_awaited()
        with patch.object(srv, "get_graphiti", graph), patch.object(srv, "drain_seconds_left", return_value=0):
            response = await srv.extraction_repair_apply(request({"source_obs_id": 3}))
        self.assertEqual(response.status_code, 400)
        graph.assert_not_awaited()

    async def test_status_returns_audit_not_graph_mutation(self):
        audit = AsyncMock(return_value={"repair_id": "r1", "state": "applied"})
        with patch.object(srv.extraction_repair, "audit", audit), patch.object(srv, "get_status_driver", return_value=object()):
            response = await srv.extraction_repair_status(request(method="GET", query=b"repair_id=r1"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["repair"]["state"], "applied")


if __name__ == "__main__":
    unittest.main()
