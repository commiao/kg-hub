"""人工断路器：状态语义 + 付费路径上的强制判定。

存在的理由（2026-09-10 实测）：qianfan 恢复后采集全速跑，claude-mem 每小时约 96 次
在流式响应完成前放弃，其中约 28 次在网关留下 `unknown` 记录。那种记录**永远不会
过期**（网关只删 completed/error，因为过期不能证明供应商没扣过钱），每一条都挡住
下一次 cutover——六小时攒了 171 条。而当时没有任何手段能单独切断某一路的模型调用：
网关只有全局排空，一切就把 dsh_nas / student_ask_han 这些无关业务也切了。

这套测试钉三件事：
1. 三种状态故意不对称：不存在=通行、合法=照办、**损坏=按断开**（花钱的闸门，
   读不懂时唯一安全的假设是别花）。
2. 判定在 kg-hub 唯一的模型出口里，且在 inflight 合并**之前**——否则已经在飞的
   那一个照样把钱花掉，而扳开关的人以为已经断了。
3. 损坏后扳开关，不能把没人动过的另一个 key 一起永久扳断。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import breakers
import model_gateway_client as mgc

CM = "claude_mem.observation"
KG = "kg_hub.entity_extract"


class BreakerStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "breakers.json"

    def write_raw(self, text: str):
        self.path.write_text(text, "utf-8")

    # ---- 三种状态 ----------------------------------------------------
    def test_missing_file_lets_traffic_through(self):
        # 「从没配过」是正常初始状态，不能让它把管线焊死。
        state = breakers.read_state(self.path)
        self.assertTrue(state["ok"])
        self.assertTrue(state["missing"])
        self.assertFalse(state["corrupt"])
        for key in breakers.KNOWN_KEYS:
            self.assertFalse(state["breakers"][key]["tripped"])
            self.assertEqual(breakers.is_tripped(key, self.path), (False, ""))
        breakers.assert_closed(KG, self.path)  # 不抛

    def test_valid_file_is_obeyed_per_key(self):
        breakers.set_tripped(KG, True, by="tester", reason="烧钱了",
                             path=self.path)
        self.assertEqual(breakers.is_tripped(KG, self.path), (True, "烧钱了"))
        # 只断一个，另一个照常。
        self.assertEqual(breakers.is_tripped(CM, self.path), (False, ""))
        with self.assertRaises(breakers.BreakerOpen) as caught:
            breakers.assert_closed(KG, self.path)
        self.assertEqual(caught.exception.key, KG)
        breakers.assert_closed(CM, self.path)

        breakers.set_tripped(KG, False, by="tester", path=self.path)
        self.assertEqual(breakers.is_tripped(KG, self.path), (False, ""))
        breakers.assert_closed(KG, self.path)

    def test_every_unreadable_shape_trips_everything(self):
        # fail-closed：读不懂就当全断。逐个形状钉住，避免以后有人「顺手」放宽。
        cases = {
            "不是 JSON": "{ not json",
            "版本不认识": json.dumps({"version": 2, "breakers": {}}),
            "缺 version": json.dumps({"breakers": {}}),
            "结构不对": json.dumps({"version": 1, "breakers": []}),
            "未知 key": json.dumps({"version": 1, "breakers": {
                "some.other": {"tripped": False}}}),
            "tripped 不是布尔": json.dumps({"version": 1, "breakers": {
                KG: {"tripped": "yes"}}}),
            "reason 不是字符串": json.dumps({"version": 1, "breakers": {
                KG: {"tripped": False, "reason": 5}}}),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.write_raw(text)
                state = breakers.read_state(self.path)
                self.assertTrue(state["corrupt"], name)
                self.assertFalse(state["ok"], name)
                self.assertIsNotNone(state["error"], name)
                for key in breakers.KNOWN_KEYS:
                    self.assertTrue(state["breakers"][key]["tripped"], name)
                    with self.assertRaises(breakers.BreakerOpen):
                        breakers.assert_closed(key, self.path)

    def test_read_state_never_raises_on_a_paid_path(self):
        # 判定点在付费路径上：它必须总能给出一个判决，不能把异常甩给调用方。
        self.path.mkdir()                       # 目录而不是文件
        self.assertTrue(breakers.read_state(self.path)["corrupt"])
        with mock.patch.object(Path, "read_text", side_effect=OSError("boom")):
            self.path.rmdir()
            self.write_raw("{}")
            self.assertTrue(breakers.read_state(self.path)["corrupt"])

    def test_oversize_file_is_treated_as_corrupt(self):
        self.write_raw(json.dumps({"version": 1, "breakers": {
            KG: {"tripped": False, "reason": "x" * (breakers.MAX_BYTES + 10)}}}))
        self.assertTrue(breakers.read_state(self.path)["corrupt"])

    # ---- 写 ----------------------------------------------------------
    def test_trip_after_corruption_does_not_strand_the_other_key(self):
        # 损坏时读出来的是「两个都断」，若把它当既有事实写回去，就会把没人动过的
        # 那个 key 也永久扳断——操作员只想动一个。
        self.write_raw("{ not json")
        breakers.set_tripped(KG, True, by="tester", reason="修复中",
                             path=self.path)
        state = breakers.read_state(self.path)
        self.assertTrue(state["ok"])
        self.assertTrue(state["breakers"][KG]["tripped"])
        self.assertFalse(state["breakers"][CM]["tripped"])

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        breakers.set_tripped(CM, True, by="tester", path=self.path)
        leftovers = [p.name for p in self.path.parent.iterdir()
                     if p.name != self.path.name]
        self.assertEqual(leftovers, [])

    def test_write_rejects_keys_outside_the_reviewed_list(self):
        # 可切断的东西必须是人工审过的清单，不能由运行时数据自己长出来。
        with self.assertRaises(ValueError):
            breakers.set_tripped("some.other", True, by="tester", path=self.path)
        with self.assertRaises(ValueError):
            breakers.set_tripped(KG, "yes", by="tester", path=self.path)

    def test_unknown_key_is_not_governed_by_the_breaker(self):
        breakers.set_tripped(KG, True, by="tester", path=self.path)
        self.assertEqual(breakers.is_tripped("student_ask_han.answer", self.path),
                         (False, ""))
        breakers.assert_closed("student_ask_han.answer", self.path)

    def test_known_keys_match_the_two_topology_nodes(self):
        self.assertEqual(set(breakers.KNOWN_KEYS), {CM, KG})
        self.assertEqual(set(breakers.KEY_NODES), set(breakers.KNOWN_KEYS))
        self.assertEqual(breakers.KEY_NODES[CM], "claude-mem")
        self.assertEqual(breakers.KEY_NODES[KG], "refinery")


class _Messages:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        return {"ok": True}


class _Client:
    def __init__(self):
        self.messages = _Messages()


class BreakerEnforcementTests(unittest.TestCase):
    """判定必须在 kg-hub 唯一的模型出口里，且在 inflight 合并之前。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "breakers.json"
        patcher = mock.patch.object(breakers, "DEFAULT_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 生产要求每次调用带持久 operation_id;这里用它自带的、仅限
        # dev/test 的逃生口，免得为了测断路器再搭一套 operation 上下文。
        for name, value in (("KG_HUB_ALLOW_EPHEMERAL_IDEMPOTENCY", "1"),
                            ("KG_HUB_ENV", "test")):
            patch = mock.patch.dict(os.environ, {name: value})
            patch.start()
            self.addCleanup(patch.stop)

    def wrapped(self):
        client = _Client()
        raw = client.messages
        return mgc.install_gateway_request_contract(client), raw

    def test_closed_breaker_lets_the_call_reach_the_provider(self):
        client, raw = self.wrapped()
        asyncio.run(client.messages.create(model=KG, messages=[]))
        self.assertEqual(raw.calls, 1)

    def test_open_breaker_stops_the_request_before_it_is_sent(self):
        breakers.set_tripped(KG, True, by="tester", reason="悬账在涨",
                             path=self.path)
        client, raw = self.wrapped()
        with self.assertRaises(breakers.BreakerOpen):
            asyncio.run(client.messages.create(model=KG, messages=[]))
        # 关键断言：一个请求都没出门，所以一分钱都没花。
        self.assertEqual(raw.calls, 0)

    def test_breaker_is_checked_before_inflight_coalescing(self):
        # 合并之后再判，已经在飞的那一个仍会把钱花掉，而扳开关的人以为已经断了。
        source = Path(mgc.__file__).read_text("utf-8")
        body = source.split("async def create_with_gateway_contract", 1)[1]
        check = body.index("breakers.assert_closed")
        merge = body.index("pending = inflight.get(key)")
        self.assertLess(check, merge)

    def test_corrupt_state_stops_the_paid_path(self):
        self.path.write_text("{ not json", "utf-8")
        client, raw = self.wrapped()
        with self.assertRaises(breakers.BreakerOpen):
            asyncio.run(client.messages.create(model=KG, messages=[]))
        self.assertEqual(raw.calls, 0)

    def test_missing_state_does_not_stop_the_paid_path(self):
        client, raw = self.wrapped()
        asyncio.run(client.messages.create(model=KG, messages=[]))
        self.assertEqual(raw.calls, 1)

    def test_the_other_key_is_unaffected_when_one_is_tripped(self):
        breakers.set_tripped(CM, True, by="tester", path=self.path)
        client, raw = self.wrapped()
        asyncio.run(client.messages.create(model=KG, messages=[]))
        self.assertEqual(raw.calls, 1)



class TopologyAnnotationTests(unittest.TestCase):
    """开关必须挂在它真正管的那个节点上，而且损坏要在图上说出来。"""

    def setUp(self):
        import topology
        self.T = topology
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "breakers.json"

    def snap(self):
        return {"nodes": [
            {"id": "claude-mem", "layer": "worker", "label": "claude-mem",
             "state": "green"},
            {"id": "refinery", "layer": "consumer", "label": "refinery",
             "state": "green"},
            {"id": "kg-hub", "layer": "kghub", "label": "kg-hub", "state": "green"},
        ]}

    def test_switch_lands_on_exactly_the_two_model_calling_nodes(self):
        snap = self.snap()
        self.T.annotate_breakers(snap, breakers.read_state(self.path))
        by_id = {n["id"]: n for n in snap["nodes"]}
        self.assertIn("breaker", by_id["claude-mem"])
        self.assertIn("breaker", by_id["refinery"])
        # 不调模型的节点不该长出开关——开关是控制面，多一个都是误操作入口。
        self.assertNotIn("breaker", by_id["kg-hub"])
        self.assertEqual(by_id["refinery"]["breaker"]["key"], KG)
        self.assertEqual(by_id["claude-mem"]["breaker"]["key"], CM)

    def test_tripped_node_reads_as_deliberate_not_as_a_fault(self):
        breakers.set_tripped(KG, True, by="dashboard", reason="悬账在涨",
                             path=self.path)
        snap = self.snap()
        self.T.annotate_breakers(snap, breakers.read_state(self.path))
        node = next(n for n in snap["nodes"] if n["id"] == "refinery")
        self.assertTrue(node["breaker"]["tripped"])
        self.assertEqual(node["breaker"]["reason"], "悬账在涨")
        # amber 而不是 red：人为切断和真出事必须一眼分得开。
        self.assertEqual(node["state"], "amber")
        self.assertEqual(node["sub"], "已人工断开")
        # 另一个节点不受影响。
        other = next(n for n in snap["nodes"] if n["id"] == "claude-mem")
        self.assertFalse(other["breaker"]["tripped"])
        self.assertEqual(other["state"], "green")

    def test_corruption_is_visible_on_the_map(self):
        # 否则整条管线静悄悄停住，看图的人以为是别人手动关的，要查很久。
        self.path.write_text("{ not json", "utf-8")
        snap = self.snap()
        self.T.annotate_breakers(snap, breakers.read_state(self.path))
        node = next(n for n in snap["nodes"] if n["id"] == "refinery")
        self.assertTrue(node["breaker"]["corrupt"])
        self.assertTrue(node["breaker"]["tripped"])
        self.assertEqual(node["sub"], "断路状态不可读")
        # 没有执行方的那一路,损坏也不许显示成「已断开」——它并没有断。
        other = next(n for n in snap["nodes"] if n["id"] == "claude-mem")
        self.assertTrue(other["breaker"]["corrupt"])
        self.assertFalse(other["breaker"]["tripped"])
        self.assertEqual(other["state"], "green")

    def test_missing_node_is_not_an_error(self):
        # 某台设备上没有 refinery 很正常，不该因此炸掉整张图。
        snap = {"nodes": [{"id": "kg-hub", "layer": "kghub", "label": "kg-hub"}]}
        self.T.annotate_breakers(snap, breakers.read_state(self.path))
        self.assertNotIn("breaker", snap["nodes"][0])


class RefineryGateTests(unittest.TestCase):
    """refinery 必须是「停流」而不是「撞墙」：开关一关，一条都不提交。"""

    def test_breaker_gate_precedes_every_other_cycle_gate(self):
        source = Path(__file__).resolve().parent.parent.joinpath(
            "kg_refinery.py").read_text("utf-8")
        body = source.split("    while True:\n        cycle += 1", 1)[1]
        gate = body.index("breakers.is_tripped(BREAKER_KEY)")
        # 人工闸门排在温度门控、窗口门控、配额停发之前：人按的开关最优先。
        for later in ("max_disk_temp()", "in_backlog_window()",
                      'quota_pause.get("until_cycle"'):
            self.assertLess(gate, body.index(later), later)

    def test_gate_skips_the_cycle_instead_of_submitting(self):
        source = Path(__file__).resolve().parent.parent.joinpath(
            "kg_refinery.py").read_text("utf-8")
        body = source.split("breakers.is_tripped(BREAKER_KEY)", 1)[1][:600]
        self.assertIn("breaker_open=True", body)
        self.assertIn("continue", body)
        # 不能把它记成一次错误——那就等于每轮给观测扣一次重试。
        self.assertIn("last_error=None", body)

    def test_breaker_open_is_not_punished_as_an_extraction_failure(self):
        server = Path(__file__).resolve().parent.parent.joinpath(
            "kg_hub_server.py").read_text("utf-8")
        classifier = server.split("def classify_extract_error", 1)[1][:1200]
        self.assertIn('"breaker_open"', classifier)
        # 归到与 quota/gateway 同一类的 1h 快清，不让观测为一次运维动作白锁 24h。
        self.assertIn("'breaker_open'", server)


class EnforcementHonestyTests(unittest.TestCase):
    """按下去没效果的开关，比没有开关更危险——UI 必须如实说它没接线。"""

    def setUp(self):
        import topology
        self.T = topology
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "breakers.json"

    def test_enforced_flag_covers_every_known_key(self):
        self.assertEqual(set(breakers.ENFORCED), set(breakers.KNOWN_KEYS))

    def test_unenforced_key_never_claims_to_have_cut_anything(self):
        # claude-mem 的 worker 不在本仓库，目前没有执行方。
        self.assertFalse(breakers.ENFORCED[CM])
        breakers.set_tripped(CM, True, by="tester", path=self.path)
        snap = {"nodes": [{"id": "claude-mem", "layer": "worker",
                           "label": "claude-mem", "state": "green"}]}
        self.T.annotate_breakers(snap, breakers.read_state(self.path))
        node = snap["nodes"][0]
        self.assertFalse(node["breaker"]["enforced"])
        # 状态里写着已断，但图上不许显示成已断——因为它并没有真的断。
        self.assertFalse(node["breaker"]["tripped"])
        self.assertEqual(node["state"], "green")
        self.assertNotIn("sub", node)

    def test_the_front_end_refuses_to_toggle_an_unwired_switch(self):
        source = Path(__file__).resolve().parent.parent.joinpath(
            "topology.py").read_text("utf-8")
        self.assertIn("data-enforced=", source)
        self.assertIn("未接线", source)
        handler = source.split("document.addEventListener('click'", 1)[1][:700]
        self.assertIn("dataset.enforced !== '1'", handler)


if __name__ == "__main__":
    unittest.main()
