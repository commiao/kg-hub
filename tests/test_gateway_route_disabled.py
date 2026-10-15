"""网关侧按业务 key 断路（credvault 2790c59）：kg-hub 认得它、停得住、画得出。

两层断路各管一件事：网关那层（路由 enabled=false，credvault 看板经审计切换）是
管钱的硬闸；kg-hub 自己的断路器（breakers.json）负责停流、保住队列。两者互不
知道，于是网关关了以后 kg-hub 这边曾经：

- 直连路径的 503 business_route_disabled 落进 5xx 兜底成 upstream_error：任务执行
  记一次 failed（占 3 次重试里的一次），预拆路径还会把观测推进 needs_reconciliation；
- 队列路径（常规路径）里网关把作业扣着（disposition=paused），kg-hub 只知道「被扣」，
  看板一律写成「供应商限流」；
- 拓扑页上模型网关与 kg-hub 都是绿的，而入图为 0。

网关的 /health/ready 不报每条路由的 enabled，kg-hub 读不到开关本身，所以这里钉住的
都是「真实请求撞到的证据」怎么被认出、怎么停流、怎么上图。kg-hub 不写网关配置，
图上也不画可按的开关。
"""
from __future__ import annotations

import ast
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kg_refinery as refinery  # noqa: E402
import topology  # noqa: E402
from utils import gateway_queue as gq  # noqa: E402
from utils import refinery_recovery as recovery  # noqa: E402
from utils.model_attempt_journal import NeedsReconciliation  # noqa: E402
from utils.task_execution import REFUSED_BEFORE_RESULT  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = (ROOT / "kg_hub_server.py").read_text("utf-8")
REFINERY_SRC = (ROOT / "kg_refinery.py").read_text("utf-8")
CODE = "business_route_disabled"
# 网关 BusinessRouteDisabled 的线上形状：503，error.code 是机器码，文案可能会变。
BODY_503 = {"type": "error", "error": {"type": "api_error", "code": CODE,
                                       "message": "业务 kg_hub.entity_extract 已被人工断路；恢复调用后再重试"}}


def _classifier():
    tree = ast.parse(SERVER_SRC)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "classify_extract_error")
    ns: dict = {"NeedsReconciliation": NeedsReconciliation}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<classifier>", "exec"), ns)
    return ns["classify_extract_error"]


class _Err(Exception):
    def __init__(self, status_code=None, body=None):
        super().__init__("Error code: 503")
        self.status_code = status_code
        self.body = body


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.classify = _classifier()

    def test_route_disabled_is_its_own_deliberate_kind(self):
        self.assertEqual(self.classify(_Err(503, BODY_503)), CODE)

    def test_judged_by_code_not_by_message(self):
        body = {"error": {"code": CODE, "message": "a sentence never seen before"}}
        self.assertEqual(self.classify(_Err(503, body)), CODE)
        # 同样是 503，没有这个机器码就仍是普通上游错误，不许蹭「有意断路」。
        self.assertEqual(self.classify(_Err(503, {"error": {"code": "other"}})),
                         "upstream_error")

    def test_queue_settled_failure_mirror_is_recognised_too(self):
        # 队列作业若以 retry/new-key 结束，QueueRetryWithNewKey 会把错误码镜像到
        # status_code/body 上，分类走同一条判据。
        exc = gq.QueueRetryWithNewKey({"state": "failed", "error": {
            "code": CODE, "http_status": 503, "disposition": "retry", "same_key": False}})
        self.assertEqual(self.classify(exc), CODE)

    def test_not_an_attempt_of_the_task(self):
        # deferred 而不是 failed：不占 3 次重试，预拆路径也不进 needs_reconciliation。
        self.assertIn(CODE, REFUSED_BEFORE_RESULT)

    def test_a_leaked_error_key_is_released_with_the_other_gateway_kinds(self):
        sweep = SERVER_SRC.split("quota_threshold = ", 1)[1][:1600]
        self.assertIn(f"'{CODE}'", sweep)
        self.assertIn("'breaker_open'", sweep)


class QueueHoldTests(unittest.IsolatedAsyncioTestCase):
    """常规路径：网关把作业扣在队列里（disposition=paused），kg-hub 要说清扣的原因。"""

    def setUp(self):
        gq._provider_wait.clear()

    async def test_hold_reason_is_kept_for_the_board(self):
        body = {"model": "kg_hub.entity_extract", "max_tokens": 10,
                "messages": [{"role": "user", "content": "x"}]}
        answer = {"id": "m", "type": "message", "role": "assistant", "model": "l",
                  "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                  "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}

        def job(state, error=None):
            return {"version": 1, "job": {"request_key": "k", "business_key": body["model"],
                                          "state": state, "response": answer, "error": error}}
        statuses = [job("queued", {"code": CODE, "disposition": "paused"})] * 2 + [job("succeeded")]
        seen = []

        def handle(req):
            seen.append(req.url.path)
            return httpx.Response(200, json=statuses[min(len(seen), len(statuses)) - 1])
        views = []

        async def sleep(_):
            views.append(gq.provider_wait())
        with mock.patch.object(gq.asyncio, "sleep", sleep):
            await gq.execute_queued("http://model-gateway:39000", "t", "k", body,
                                    transport=httpx.MockTransport(handle),
                                    poll_seconds=0, held_poll_seconds=0, wait_seconds=0)
        self.assertEqual(views[0]["code"], CODE)
        self.assertIsNone(gq.provider_wait(), "恢复后第一条成功就该清掉")


class RefineryTests(unittest.TestCase):
    def poll(self, *answers):
        calls = []

        def fake_http(method, url, body=None, timeout=30):
            calls.append(url)
            return 200, answers[min(len(calls), len(answers)) - 1]
        with mock.patch.object(refinery, "_http", fake_http), \
                mock.patch.object(refinery.asyncio, "sleep", mock.AsyncMock()):
            return asyncio.run(refinery.poll_until_done("claude-mem", "42", max_wait=1)), calls

    def test_refusal_returns_at_once_and_halts_the_batch(self):
        st, calls = self.poll({"status": "error", "error_kind": CODE})
        self.assertEqual(st, CODE)
        self.assertEqual(len(calls), 1)
        self.assertIn(CODE, recovery.PAUSING_FAILURES)
        # 它不是基础设施故障：不该被报成「上游 5xx 停发」。
        self.assertNotIn(CODE, recovery.INFRA_FAILURES)

    def test_pause_is_long_because_readiness_cannot_see_the_route(self):
        state: dict = {}
        recovery.record_failure(state, CODE, cycle=1, interval=90, quota_delay=3600, now=1000.0)
        self.assertEqual(state["retry_at"], 1000.0 + recovery.ROUTE_DISABLED_DELAY)
        self.assertGreaterEqual(recovery.ROUTE_DISABLED_DELAY, 300)
        fields = recovery.status_fields(state)
        self.assertTrue(fields["gateway_route_disabled"])
        self.assertFalse(fields["upstream_error_paused"])

    def test_the_observation_is_not_cooled_for_an_operator_decision(self):
        body = REFINERY_SRC[REFINERY_SRC.index("elif st in recovery.PAUSING_FAILURES:"):]
        body = body[:body.index("record_failure(")]
        self.assertIn("if st != recovery.ROUTE_DISABLED:", body)

    def test_held_poll_records_the_reason(self):
        refinery._provider_wait_seen[0] = 0.0
        refinery._provider_wait_code[0] = None
        # 被扣的时间不计入轮询上限（等多久不由 kg-hub 定），所以第二次轮询给出终态。
        st, _ = self.poll({"status": "pending", "provider_wait": {"since": 1, "code": CODE}},
                          {"status": "ok"})
        self.assertEqual(st, "ok")
        self.assertTrue(refinery.provider_waiting())
        self.assertEqual(refinery.provider_wait_code(), CODE)

    def test_status_carries_the_reason_next_to_provider_wait(self):
        self.assertIn("provider_wait_code=provider_wait_code()", REFINERY_SRC)
        self.assertIn('"gateway_route_disabled": False', REFINERY_SRC)


def _snap():
    return {"nodes": [
        {"id": "worker", "state": "green", "label": "claude-mem"},
        {"id": "refinery", "state": "green", "label": "refinery"},
        {"id": "kghub", "state": "green", "label": "kg-hub", "detail": "ok"},
        {"id": "gateway", "state": "green", "label": "模型网关", "detail": "健康检查通过"},
    ], "edges": [
        {"from": "refinery", "to": "kghub", "state": "green"},
        {"from": "kghub", "to": "gateway", "state": "green"},
    ]}


class TopologyTests(unittest.TestCase):
    QUEUE_HOLD = {"provider_wait": True, "provider_wait_code": CODE}

    def test_evidence_from_either_path(self):
        self.assertIsNotNone(topology.gateway_route_disabled(self.QUEUE_HOLD))
        self.assertIsNotNone(topology.gateway_route_disabled({"recovery_reason": CODE}))
        self.assertIsNotNone(topology.gateway_route_disabled({"gateway_route_disabled": True}))
        # 供应商限流同样是「被扣」，但不是网关侧断路。
        self.assertIsNone(topology.gateway_route_disabled(
            {"provider_wait": True, "provider_wait_code": "provider_rate_limited"}))
        # 新鲜度过了（provider_wait 已 False）就不再报，哪怕 code 还留着。
        self.assertIsNone(topology.gateway_route_disabled(
            {"provider_wait": False, "provider_wait_code": CODE}))
        self.assertIsNone(topology.gateway_route_disabled(None))

    def test_gateway_and_kghub_turn_amber_without_a_switch(self):
        snap = _snap()
        topology.annotate_gateway_route(snap, topology.gateway_route_disabled(self.QUEUE_HOLD))
        by_id = {n["id"]: n for n in snap["nodes"]}
        for node_id in ("gateway", "kghub"):
            self.assertEqual(by_id[node_id]["state"], "amber")
            self.assertEqual(by_id[node_id]["sub"], "网关侧已断路")
            self.assertIn("credvault 看板", by_id[node_id]["detail"])
            # 开关归 credvault 看板：不能长出一个可按的按钮。
            self.assertNotIn("breaker", by_id[node_id])
        self.assertEqual(by_id["refinery"]["state"], "green")
        self.assertEqual(by_id["worker"]["state"], "green")
        edge = next(e for e in snap["edges"] if e["from"] == "kghub")
        self.assertEqual(edge["state"], "amber")

    def test_does_not_hide_a_real_fault(self):
        snap = _snap()
        snap["nodes"][3]["state"] = "red"
        topology.annotate_gateway_route(snap, "x")
        self.assertEqual(snap["nodes"][3]["state"], "red")

    def test_nothing_happens_without_evidence(self):
        snap = _snap()
        topology.annotate_gateway_route(snap, None)
        self.assertEqual(snap, _snap())

    def test_kg_hub_breaker_still_wins_on_the_kghub_node(self):
        # 两边都断时 kg-hub 节点显示自己的开关：网关侧先挂，本地断路器后挂。
        src = (ROOT / "topology.py").read_text("utf-8")
        load = src[src.index("async def _load_snapshots"):]
        load = load[:load.index("async def breakers_state")]
        self.assertLess(load.index("annotate_gateway_route(snap"),
                        load.index("annotate_breakers(snap"))

    def test_halt_verdict_names_it_and_calls_it_deliberate(self):
        v = topology.refinery_halt({"backlog_window_open": True, **self.QUEUE_HOLD})
        self.assertTrue(v["halted"])
        self.assertTrue(v["deliberate"])
        self.assertIn("网关侧断路（kg_hub.entity_extract）", v["gates"])

    def test_flow_dashboard_does_not_call_it_a_rate_limit(self):
        import flow_dashboard
        from datetime import datetime, timezone
        found = flow_dashboard.find_bottlenecks(
            status={"heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    "backlog_window_open": True, **self.QUEUE_HOLD},
            stages={}, digest={}, gateway_node=None, keys=None, timing={},
            calls_per_obs=None, now=datetime.now(timezone.utc))
        gateway = [f for f in found if f["stage"] == "gateway"]
        self.assertEqual(len(gateway), 1)
        self.assertIn("网关侧已断路", gateway[0]["title"])
        self.assertTrue(gateway[0]["deliberate"])


if __name__ == "__main__":
    unittest.main()
