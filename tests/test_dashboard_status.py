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


class GatewayHealthFlapTests(unittest.TestCase):
    """网关健康探测的抖动不该变成告警，真正的失联必须变成告警。

    2026-09-17 现场：飞书上 `gateway_monitor_unhealthy` 在红和 resolved 之间反复
    横跳。实测网关 /health/ready 的耗时分布是
    `0.045 0.045 0.046 0.076 0.994 1.038 1.071 2.839` 秒，而当时超时写的是 3.0s
    —— 阈值压在正常范围的上沿，偶尔超时是必然的。代价被两件事放大：异常分支返回
    的 dict 没有 `monitor` 键，且这个失败结果照样缓存 60s。于是一次 3 秒抖动变成
    整整一分钟的"证据不可读"。更糟的是 watchdog 在 sample 为 None 时会**冻结所有
    业务判决**，等于网关慢一下就把真信号一起盖住。
    """

    @staticmethod
    def _client(result):
        client = AsyncMock()
        if isinstance(result, Exception):
            client.get.side_effect = result
        else:
            client.get.return_value = result
        context = AsyncMock()
        context.__aenter__.return_value = client
        return context

    def _run(self, cache, result, now=None):
        async def run():
            patches = [
                patch.object(D, "_health_cache", cache),
                patch.object(D, "_health_lock", asyncio.Lock()),
                patch.object(D.httpx, "AsyncClient", return_value=self._client(result)),
                patch("model_gateway_client.gateway_base_url",
                      return_value="http://model-gateway:39000"),
            ]
            if now is not None:
                patches.append(patch.object(D.time, "monotonic", return_value=now))
            for item in patches:
                item.start()
            try:
                return await D.gateway_health()
            finally:
                for item in reversed(patches):
                    item.stop()
        return asyncio.run(run())

    def test_timeout_clears_the_measured_latency_by_a_real_margin(self):
        # 阈值必须落在实测分布之外，否则它就是个噪音源而不是故障检测器。
        slowest_observed = 2.84
        self.assertGreaterEqual(
            D._HEALTH_TIMEOUT, slowest_observed * 2,
            "超时阈值要留出倍数余量；这个检查随 idempotency 账本增长只会更慢")

    def test_a_blip_does_not_erase_evidence_that_was_readable_a_moment_ago(self):
        good = {"http_status": 503, "checked_at": "2026-09-17T15:00:00+00:00",
                "monitor": {"version": 1, "source_ok": True,
                            "checked_at": "2026-09-17T15:00:00+00:00"}}
        health = self._run((0.0, good), httpx.ReadTimeout("slow"))
        self.assertEqual(health["state"], "grey")
        self.assertIn("monitor", health,
                      "丢掉 monitor 键会让 watchdog 连业务判决一起冻结")
        self.assertEqual(health["monitor"]["checked_at"], "2026-09-17T15:00:00+00:00",
                         "必须沿用原来的 checked_at —— 真失联时它才会自然过期")

    def test_evidence_is_not_invented_when_there_never_was_any(self):
        # 没有旧证据就不能凭空造一个：这时候"不可读"是实话。
        health = self._run(None, httpx.ConnectError("down"))
        self.assertEqual(health["state"], "grey")
        self.assertNotIn("monitor", health)

    def test_a_failure_is_not_cached_as_long_as_a_success(self):
        # 缓存失败只会把一次抖动的影响拉满一分钟，而重试一次很便宜。
        self.assertLess(D._HEALTH_TTL_FAIL, D._HEALTH_TTL_OK)
        failed = {"http_status": None, "checked_at": "x", "issues": ["ReadTimeout"]}
        ok = httpx.Response(200, json={"status": "ok", "checks": {}})
        # 失败缓存刚过 TTL_FAIL、远未到 TTL_OK：必须重新去取，而不是接着返回失败。
        health = self._run((0.0, failed), ok, now=D._HEALTH_TTL_FAIL + 1)
        self.assertEqual(health["http_status"], 200, "失败不该被按成功的 TTL 留着")
        # 而成功的结果在 TTL_OK 之内要真的命中缓存，别把网关打穿。
        good = {"http_status": 200, "checked_at": "y"}
        cached = self._run((0.0, good), httpx.ConnectError("不该被调用"),
                           now=D._HEALTH_TTL_OK - 1)
        self.assertEqual(cached["checked_at"], "y")


if __name__ == "__main__":
    unittest.main()
