"""refinery 与图对账：别的路径（会话自抽取）已入库的观测要记进水位，看板的队列剩余才会下降。

2026-10-17 实测：断路器断开期间 refinery 一条不提交，1,528 条经 /api/ingest_extracted 入库后
看板仍显示 live 待处理 15,026。对账按 GRAPH_SYNC_INTERVAL_SEC 节流，只问待处理的 live id，
只把 status=ok 的记为 ingested。"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import unittest
from unittest import mock

import kg_refinery as R


def _wm():
    return {"ingested": {1, 2, 20}, "rejected": {3}, "failed": set(), "held": {4}, "boundary_id": 10}


class GraphSyncTest(unittest.TestCase):
    def setUp(self):
        R._graph_sync_state.update({"last_at": 0.0, "total": 0})

    def test_pending_live_ids_excludes_backlog_and_settled(self):
        self.assertEqual(R.pending_live_ids(_wm(), 10, [1, 2, 3, 4, 9, 11, 12, 20, 21]), [11, 12, 21])

    def test_apply_only_ok_claude_mem_names(self):
        wm = _wm()
        n = R.apply_graph_statuses(wm, {"claude-mem-obs-11": "ok", "claude-mem-obs-12": "pending",
                                        "claude-mem-obs-20": "ok", "openclaw-x": "ok", "claude-mem-obs-zz": "ok"})
        self.assertEqual(n, 1)
        self.assertIn(11, wm["ingested"]); self.assertNotIn(12, wm["ingested"])

    def test_sync_throttles_chunks_and_writes_status(self):
        wm = _wm()
        calls = []

        def http(method, url, body, timeout=30, headers=None):
            calls.append((url, body))
            return 200, {"items": {n: ("ok" if n.endswith("11") or n.endswith("21") else None) for n in body["names"]}}
        with mock.patch.object(R, "write_status") as ws:
            out = R.sync_graph_ingested(wm, 10, now=1000.0, http=http, fetch=lambda: list(range(1, 23)))
            self.assertEqual(out, {"checked": 11, "synced": 2, "total": 2})   # 11..22 去掉已入水位的 20
            self.assertTrue(calls[0][0].endswith("/api/ingest/check_names"))
            self.assertEqual(calls[0][1]["names"][0], "claude-mem-obs-11")
            self.assertEqual(ws.call_args.kwargs["graph_synced"]["total"], 2)
            # 节流：间隔内不再问
            self.assertIsNone(R.sync_graph_ingested(wm, 10, now=1000.0 + 1, http=http, fetch=lambda: [30]))
            self.assertEqual(len(calls), 1)
            # 到时间后只问还没进水位的
            R.sync_graph_ingested(wm, 10, now=1000.0 + R.GRAPH_SYNC_INTERVAL_SEC + 1, http=http,
                                  fetch=lambda: list(range(1, 23)))
            self.assertNotIn("claude-mem-obs-11", calls[1][1]["names"])

    def test_http_failure_stops_without_marking(self):
        wm = _wm()
        with mock.patch.object(R, "write_status"):
            out = R.sync_graph_ingested(wm, 10, now=5000.0, http=lambda *a, **k: (503, {}), fetch=lambda: [11, 12])
        self.assertEqual(out["synced"], 0)
        self.assertNotIn(11, wm["ingested"])


if __name__ == "__main__":
    unittest.main()
