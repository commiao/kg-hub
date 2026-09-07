import asyncio
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import dashboard_status as D
from topology import gateway_quota_node, annotate_gateway

NOW = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)


class StatusTests(unittest.TestCase):
    def test_real_paused_backlog_retains_low_throughput_warning(self):
        result = D.pipeline_signal({"heartbeat_at": NOW.isoformat(),
                                    "idle_outside_window": True}, NOW, 26, 7786)
        self.assertFalse(result["stalled"])
        self.assertTrue(result["low_throughput"])
        self.assertIn("计划暂停", result["activity"]["label"])

    def test_stale_pause_cannot_hide_dead_process(self):
        result = D.pipeline_signal({"heartbeat_at": (NOW - timedelta(hours=1)).isoformat(),
                                    "idle_outside_window": True}, NOW, 26, 7786)
        self.assertTrue(result["stalled"])
        self.assertIn("未收到心跳", result["activity"]["label"])

    def test_error_cannot_be_hidden_by_schedule_and_absent_signal_is_unknown(self):
        r = D.refinery_activity({"heartbeat_at": NOW.isoformat(),
                                "last_error": "db failed", "idle_outside_window": True}, NOW)
        self.assertEqual(r["state"], "red")
        self.assertEqual(D.refinery_activity({}, NOW)["state"], "grey")

    def test_active_slow_pipeline_not_called_stopped(self):
        r = D.pipeline_signal({"heartbeat_at": NOW.isoformat()}, NOW, 26, 7786)
        self.assertFalse(r["stalled"])
        self.assertTrue(r["low_throughput"])

    def test_503_visible_even_with_free_quota_and_recent_success(self):
        node = {"state": "green", "detail": "今日 2484/120000", "metrics": {}}
        edge = {"state": "green"}
        health = {"state": "red", "http_status": 503,
                  "issues": ["rollback_witness_preflight_unresolved"],
                  "provider_last_results": {"kg_hub.entity_extract":
                      {"status": "success", "at": NOW.isoformat()}}}
        D.apply_gateway_health(node, edge, health)
        self.assertEqual((node["state"], edge["state"]), ("red", "red"))
        self.assertIn("503", node["sub"])
        self.assertIn("不代表全部调用已停止", node["detail"])
        self.assertIn("success", node["detail"])

    def test_timeout_not_green_and_success_not_hiding_quota_exhaustion(self):
        for quota, health, expected in [("green", "grey", "grey"),
                                         ("red", "green", "red")]:
            node = {"state": quota, "detail": "quota", "metrics": {}}
            D.apply_gateway_health(node, {}, {"state": health})
            self.assertEqual(node["state"], expected)

    def test_new_health_replaces_old_snapshot_and_updates_overall(self):
        snap = {"overall": "amber", "nodes": [{"id": "kghub"},
                {"id": "gateway", "state": "green"}], "edges": [
                    {"from": "kghub", "to": "gateway", "state": "green"}]}
        annotate_gateway(snap, {"id": "gateway", "state": "red"},
                         {"from": "kghub", "to": "gateway", "state": "red"})
        self.assertEqual(snap["overall"], "red")
        self.assertEqual(snap["nodes"][1]["state"], "red")
        self.assertEqual(len(snap["edges"]), 1)

    def test_other_business_quota_not_blocking_kg_hub(self):
        usage = {"generated_at": NOW.isoformat(), "daily": [
            {"day": "2026-09-07", "business_key": "claude_mem.observation", "count": 5000}],
            "effective_limits": {"claude_mem.observation": {"daily_requests": 5000},
                         "kg_hub.entity_extract": {"daily_requests": 120000}}}
        self.assertEqual(gateway_quota_node(usage, {}, now=NOW)[0]["state"], "green")

    def test_health_only_get_and_concurrent_requests_share_cache(self):
        async def run():
            client = AsyncMock()
            client.get.return_value = httpx.Response(503, json={"status": "error", "checks": {
                "rollback_witness": {"issues": ["rollback_witness_preflight_unresolved"]}}})
            context = AsyncMock()
            context.__aenter__.return_value = client
            with patch.object(D, "_health_cache", None), patch.object(D, "_health_lock", asyncio.Lock()), \
                 patch.object(D.httpx, "AsyncClient", return_value=context), \
                 patch("model_gateway_client.gateway_base_url", return_value="http://model-gateway:39000"):
                results = await asyncio.gather(*(D.gateway_health() for _ in range(12)))
                client.get.assert_awaited_once_with("http://model-gateway:39000/health/ready")
                client.post.assert_not_called()
                self.assertTrue(all(r["http_status"] == 503 for r in results))
        asyncio.run(run())

    def test_quarantine_is_amber_not_completely_healthy_or_globally_blocked(self):
        async def run():
            client = AsyncMock()
            client.get.return_value = httpx.Response(200, json={"status": "ok", "checks": {
                "rollback_witness": {"status": "warning", "issues": [], "quarantined_count": 1,
                                     "quarantined": [{"business_key": "kg_hub.entity_extract",
                                                      "reserved_at": NOW.isoformat()}]}}})
            context = AsyncMock()
            context.__aenter__.return_value = client
            with patch.object(D, "_health_cache", None), patch.object(D, "_health_lock", asyncio.Lock()), \
                 patch.object(D.httpx, "AsyncClient", return_value=context), \
                 patch("model_gateway_client.gateway_base_url", return_value="http://model-gateway:39000"):
                health = await D.gateway_health()
                self.assertEqual(health["state"], "amber")
                for quota, expected in [("green", "amber"), ("red", "red")]:
                    node = {"state": quota, "detail": "quota", "metrics": {}}
                    D.apply_gateway_health(node, {}, health)
                    self.assertEqual(node["state"], expected)
                    self.assertIn("有历史隔离登记", node["sub"])
                    self.assertIn("不是当前待处理数", node["detail"])
                    self.assertIn("原始记录是否仍在当前库需另行核实", node["detail"])
                    self.assertNotIn("原始记录和配额计数保留", node["detail"])
                    self.assertIn("禁止自动重放", node["detail"])
                client.post.assert_not_called()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
