"""HTTP node-governance authentication, writer lock and read-only preview."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request
from starlette.responses import JSONResponse

from tests.test_ingest_extracted_handler import HAVE
from tests.test_graph_governance import Embedder, MERGE, merge_graph, reviewed, run

if HAVE:
    import kg_hub_server as srv


def request(payload=None, path="/api/graph/governance", method="POST", token=None, query=b""):
    data = json.dumps(payload).encode()
    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    headers = [(b"authorization", ("Bearer " + token).encode())] if token else []
    return Request({"type": "http", "method": method, "path": path, "scheme": "http",
                    "headers": headers, "query_string": query}, receive)


@unittest.skipUnless(HAVE, "needs graphiti_core dependency environment")
class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_token_can_preview_and_read_status_but_never_apply(self):
        middleware = srv.BearerAuthMiddleware(None)
        for scopes, path, method, expected in (
            (["read"], "/api/graph/governance/preview", "POST", 200),
            (["read"], "/api/graph/governance", "GET", 200),
            (["read"], "/api/graph/governance", "POST", 403),
            (["extract"], "/api/graph/governance/preview", "POST", 403),
            (["extract"], "/api/graph/governance", "POST", 403),
        ):
            with self.subTest(scopes=scopes, path=path, method=method):
                next_call = AsyncMock(return_value=JSONResponse({"status": "ok"}))
                with patch.object(srv, "API_TOKEN", "unit-admin"), \
                        patch.object(srv.token_auth, "principal_for", return_value={"name": "d", "scopes": scopes}):
                    response = await middleware.dispatch(request(path=path, method=method, token="unit-read"), next_call)
                self.assertEqual(response.status_code, expected)
                self.assertEqual(next_call.await_count, int(expected == 200))

    async def test_preview_is_read_only_and_refusals_carry_the_snapshot(self):
        graph = merge_graph()
        g = SimpleNamespace(driver=graph, embedder=Embedder())
        with patch.object(srv, "get_graphiti", AsyncMock(return_value=g)):
            response = await srv.graph_governance_preview(request(MERGE, path="/api/graph/governance/preview"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.body)["plan"]["summary"]["edges_absorb"], 1)
            response = await srv.graph_governance_preview(request(
                {"operation": "rename", "node": {"uuid": "dup"}, "rename_to": "CredVault"},
                path="/api/graph/governance/preview"))
        self.assertEqual(response.status_code, 409)
        body = json.loads(response.body)
        self.assertEqual(body["code"], "name_conflict")
        self.assertEqual(len(body["detail"]["snapshot"]["snapshot_sha256"]), 64)
        self.assertEqual(graph.mutations, [])

    async def test_apply_holds_the_shared_writer_lock_for_every_mutation(self):
        graph = merge_graph()
        g = SimpleNamespace(driver=graph, embedder=Embedder())
        doc, _ = await reviewed(graph, MERGE)
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
        original = graph.answer

        def guarded(tag, query, params):
            if tag not in srv.graph_governance._READ_TAGS:
                self.assertTrue(active, "governance mutation escaped the shared writer lock")
            return original(tag, query, params)
        graph.answer = guarded
        with patch.object(srv, "get_graphiti", AsyncMock(return_value=g)), \
                patch.object(srv, "_ingest_execution_lock", lock), \
                patch.object(srv, "drain_seconds_left", return_value=0):
            response = await srv.graph_governance_apply(request(doc))
        self.assertEqual(response.status_code, 200, response.body)
        self.assertEqual(owners, ["graph-governance"])
        self.assertTrue(json.loads(response.body)["verified"])
        status = AsyncMock(return_value={"governance_id": "merge-1", "state": "applied"})
        with patch.object(srv.graph_governance, "audit", status), patch.object(srv, "get_status_driver", return_value=object()):
            response = await srv.graph_governance_status(request(method="GET", query=b"governance_id=merge-1"))
        self.assertEqual(json.loads(response.body)["governance"]["state"], "applied")

    async def test_draining_bad_request_and_stale_requests_do_not_write(self):
        graph = AsyncMock()
        with patch.object(srv, "get_graphiti", graph), patch.object(srv, "drain_seconds_left", return_value=5):
            self.assertEqual((await srv.graph_governance_apply(request({}))).status_code, 503)
        with patch.object(srv, "get_graphiti", graph), patch.object(srv, "drain_seconds_left", return_value=0):
            response = await srv.graph_governance_apply(request({**MERGE}))
        self.assertEqual(response.status_code, 400)
        graph.assert_not_awaited()
        real = merge_graph()
        doc, _ = await reviewed(real, MERGE)
        real.edge("late", "dup", "y", "late", ["e9"], "2026-10-24T00:00:00Z")
        real.tags.clear()
        with patch.object(srv, "get_graphiti", AsyncMock(return_value=SimpleNamespace(driver=real, embedder=Embedder()))), \
                patch.object(srv, "drain_seconds_left", return_value=0):
            response = await srv.graph_governance_apply(request(doc))
        self.assertEqual((response.status_code, json.loads(response.body)["code"]), (409, "stale_snapshot"))
        self.assertEqual(real.mutations, [])

    async def test_capabilities_advertise_governance(self):
        body = json.loads((await srv.extraction_capabilities(request(method="GET"))).body)
        self.assertEqual(body["graph_governance"]["version"], "graph-governance-v1")
        self.assertEqual(body["graph_governance"]["paths"]["preview"], "/api/graph/governance/preview")


if __name__ == "__main__":
    unittest.main()
