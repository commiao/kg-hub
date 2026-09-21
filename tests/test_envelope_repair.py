"""模型把外壳搞错时，正确的载荷不该跟着作废 —— 但修正必须可见。

2026-09-21 实测：图里 17 条非 503 的 error 键，约 10 条是**载荷正确、外壳错**：

    形态 A  extracted_entities 该是 list，模型给的是内容正确的 JSON 字符串
            ValidationError: Input should be a valid list [type=list_type,
              input_value='\\n[{"name": "192.168.10...", "episode_indices": [0]}]\\n']
    形态 B  列表被多包一层：{"edges": [{"edges": [ ...真正的边... ]}]}
            ValidationError: edges.0.source_entity_name Field required

抽取结果没问题，却被 pydantic 拒掉 → 落 error 键 → 24h 锁住 → 重推撞 409。

这套测试钉三件事，缺任何一件这个修复都会变成新的坑：
1. 两条规则**确实**修好那两个真实形状（用线上原文当输入）；
2. 两条规则**确实不动**长得像但不该动的东西 —— 这比第 1 条重要，
   因为误修会把「模型真的少答了」静默放过去；
3. 每次修正都被计数，且计数**走到了状态文件那一侧**。
   看不见次数的修补就是静默修补，那是本项目反复消灭的东西。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kg_refinery as refinery  # noqa: E402
import model_gateway_client as gw  # noqa: E402

SERVER_SRC = (ROOT / "kg_hub_server.py").read_text("utf-8")

# 线上 error_message 里带出来的原文（截断处补全成合法 JSON）。
REAL_JSON_STRING = '\n[{"name": "192.168.10.1", "entity_type_id": 0, "episode_indices": [0]}]\n'
REAL_DOUBLE_WRAP = [{"edges": [{"source_entity_name": "a", "target_entity_name": "b"}]}]


class _Block:
    def __init__(self, type_: str, payload):
        self.type = type_
        self.input = payload


class _Response:
    def __init__(self, *blocks):
        self.content = list(blocks)


class RepairTests(unittest.TestCase):
    def setUp(self):
        gw._REPAIRS_TOTAL.clear()

    def repair(self, payload: dict) -> dict:
        r = _Response(_Block("tool_use", payload))
        gw.repair_structured_envelopes(r)
        return r.content[0].input

    def test_json_string_becomes_the_list_it_already_was(self):
        out = self.repair({"extracted_entities": REAL_JSON_STRING})
        self.assertEqual(out["extracted_entities"],
                         [{"name": "192.168.10.1", "entity_type_id": 0,
                           "episode_indices": [0]}])
        self.assertEqual(gw.envelope_repairs_total(), {"json_string": 1})

    def test_double_wrapped_list_loses_exactly_one_layer(self):
        out = self.repair({"edges": REAL_DOUBLE_WRAP})
        self.assertEqual(out["edges"],
                         [{"source_entity_name": "a", "target_entity_name": "b"}])
        self.assertEqual(gw.envelope_repairs_total(), {"double_wrapped": 1})


class DoNotTouchTests(unittest.TestCase):
    """误修比不修坏：它会把「模型真的少答了」静默放过去。"""

    def setUp(self):
        gw._REPAIRS_TOTAL.clear()

    def unchanged(self, payload: dict):
        r = _Response(_Block("tool_use", dict(payload)))
        gw.repair_structured_envelopes(r)
        self.assertEqual(r.content[0].input, payload)
        self.assertEqual(gw.envelope_repairs_total(), {},
                         "没修还记了一次，计数就不可信了")

    def test_plain_text_field_is_not_parsed_as_json(self):
        self.unchanged({"summary": "一句普通的话，不是 JSON"})

    def test_a_string_that_merely_starts_like_json_is_left_alone(self):
        self.unchanged({"summary": "[未完成的方括号"})

    def test_a_legitimate_single_element_list_survives(self):
        """只有一条边是完全正常的结果，不许被当成「多包了一层」。"""
        self.unchanged({"edges": [{"source_entity_name": "x"}]})

    def test_nesting_under_a_different_key_is_not_unwrapped(self):
        """同名才是「多包一层」的证据；不同名可能是正当的嵌套结构。"""
        self.unchanged({"nodes": [{"edges": [{"source_entity_name": "x"}]}]})

    def test_same_key_plus_other_fields_is_a_real_object_not_an_envelope(self):
        """只有「元素的键集**恰好**只有那一个」才算外壳。

        这条是那个守卫的唯一分辨点：下面这个元素是一条真实的边，它自己带一个
        同名字段。把「同名」单独当条件（不管还有没有别的键）就会把它剥成 []，
        **静默丢掉这条边** —— 正是这两条规则最该防住的误修。
        """
        self.unchanged({"edges": [{"edges": [], "source_entity_name": "x"}]})

    def test_a_string_parsing_to_a_scalar_is_not_replaced(self):
        """解析得出标量就不是「外壳错」。

        `"123"` 是一个正当的字符串字段值；换成整数 123 是改类型，不是修外壳。
        这条钉的是**首字符那道闸** —— 它就是唯一的闸：有了它，json.loads 只可能
        得出 list / dict，所以再加一个 isinstance 判断是走不到的死分支
        （2026-09-21 变异验证抓到过：摘掉那个 isinstance 没有任何用例转红）。
        """
        for raw in ('123', 'null', '"text"'):
            r = _Response(_Block("tool_use", {"count": raw}))
            gw.repair_structured_envelopes(r)
            self.assertEqual(r.content[0].input["count"], raw, f"{raw!r} 被改写了")
        self.assertEqual(gw.envelope_repairs_total(), {})

    def test_non_tool_use_blocks_are_never_rewritten(self):
        r = _Response(_Block("text", {"extracted_entities": REAL_JSON_STRING}))
        gw.repair_structured_envelopes(r)
        self.assertEqual(r.content[0].input["extracted_entities"], REAL_JSON_STRING)
        self.assertEqual(gw.envelope_repairs_total(), {})

    def test_a_broken_response_object_cannot_fail_a_paid_call(self):
        """钱已经花了,答案已经拿到了 —— 修正出错绝不能让这次调用作废。"""
        class Hostile:
            @property
            def content(self):
                raise RuntimeError("boom")
        gw.repair_structured_envelopes(Hostile())   # 不抛即通过


class VisibilityTests(unittest.TestCase):
    """修正次数必须走到状态文件那一侧,否则就是静默修补。"""

    def setUp(self):
        gw._REPAIRS_TOTAL.clear()
        refinery._envelope_repairs.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self._orig = (refinery.STATE_DIR, refinery.STATUS)
        refinery.STATE_DIR, refinery.STATUS = d, d / "status.json"

    def tearDown(self):
        refinery.STATE_DIR, refinery.STATUS = self._orig

    def test_health_exposes_the_counter(self):
        self.assertIn('"envelope_repairs": envelope_repairs_total()', SERVER_SRC,
                      "/health 没把计数播出来,refinery 就抄不到")

    def test_refinery_copies_it_into_status(self):
        def fake_http(method, url, body=None, timeout=30):
            return 200, {"status": "ok", "envelope_repairs": {"json_string": 7}}
        original, refinery._http = refinery._http, fake_http
        try:
            refinery.refresh_envelope_repairs()
        finally:
            refinery._http = original
        refinery.write_status(disk_temp=40, last_error=None)
        self.assertEqual(json.loads(refinery.STATUS.read_text())["envelope_repairs"],
                         {"json_string": 7})

    def test_every_status_path_carries_it(self):
        """在 write_status 里注入,而不是让四条路径各自记得带。

        2026-09-21 刚因为「每条路径各带一部分」修过一次（_MOMENTARY_DEFAULTS）。
        """
        refinery._envelope_repairs.update({"double_wrapped": 3})
        # 窗口外那条路径的原样参数,压根没提 envelope_repairs
        refinery.write_status(disk_temp=48, thermal_hold=False, thermal={},
                              backlog_window_open=False, idle_outside_window=True,
                              last_error=None, **refinery.cycle_budget_fields())
        self.assertEqual(json.loads(refinery.STATUS.read_text())["envelope_repairs"],
                         {"double_wrapped": 3})

    def test_an_unreachable_server_never_zeroes_the_count(self):
        """累计量取不到就沿用。归零会让一次真实发生过的修正看起来没发生过。"""
        refinery._envelope_repairs.update({"json_string": 5})
        def dead_http(method, url, body=None, timeout=30):
            return 0, {"error": "ConnectionError"}
        original, refinery._http = refinery._http, dead_http
        try:
            refinery.refresh_envelope_repairs()
        finally:
            refinery._http = original
        self.assertEqual(refinery._envelope_repairs, {"json_string": 5})

    def test_the_fetch_happens_before_every_gate(self):
        """窗口外 refinery 不干活,但 ingester / task-hub 桥仍在写,修正照样发生。"""
        src = (ROOT / "kg_refinery.py").read_text("utf-8")
        body = src.split("while True:\n        cycle += 1", 1)[1][:600]
        fetch = body.index("refresh_envelope_repairs()")
        self.assertLess(fetch, body.index("breakers.is_tripped"),
                        "取数排在门控之后,窗口外就看不到那些修正")


if __name__ == "__main__":
    unittest.main()


class OffscriptTests(unittest.TestCase):
    """模型被**强制**调工具却回了一段文本 —— 判据取自请求/响应本身，不 match 报错文案。

    2026-09-21 的 4 条 `Could not extract JSON from model response`，文本是模型自己
    编的一段 XML 风格函数调用（去"读"观测 files_read 里的路径，而且编出来的路径和
    观测里的还不是同一个）。最初以为是观测正文含标记、模型顺着续写 —— 查了：
    28184 条观测里只有 1 条含 `<parameter=`，而且那条正是记录这次排查的产物。
    所以**假设是错的**，escape 正文什么也修不了。

    真正能判的地方在模型出口：graphiti 传 `tool_choice={'type':'tool',...}` 强制调
    那一个工具，强制之下没有 tool_use 块就只有一种解释。这样判还避开了去 match
    第三方库那句英文报错（准则 22）。
    """

    def setUp(self):
        gw._OFFSCRIPT_TOTAL[0] = 0

    def check(self, kwargs, *types):
        gw.note_offscript_if_missing_tool_use(
            kwargs, _Response(*[_Block(t, {}) for t in types]))
        return gw.offscript_total()

    def test_forced_tool_answered_with_text_is_offscript(self):
        self.assertEqual(self.check({"tool_choice": {"type": "tool", "name": "X"}},
                                    "text"), 1)

    def test_forced_tool_actually_used_is_not(self):
        self.assertEqual(self.check({"tool_choice": {"type": "tool", "name": "X"}},
                                    "text", "tool_use"), 0)

    def test_without_a_forced_choice_text_is_a_legitimate_answer(self):
        """没强制就回文本是正常的，记成脱稿会把计数变得没意义。"""
        self.assertEqual(self.check({}, "text"), 0)
        self.assertEqual(self.check({"tool_choice": {"type": "auto"}}, "text"), 0)

    def test_the_check_cannot_fail_a_paid_call(self):
        class Hostile:
            @property
            def content(self):
                raise RuntimeError("boom")
        gw.note_offscript_if_missing_tool_use(
            {"tool_choice": {"type": "tool", "name": "X"}}, Hostile())

    def test_classifier_names_it_and_the_sweep_releases_it_in_an_hour(self):
        """脱稿是采样噪声，不是这条观测的毛病 —— 不该按 24h 锁住。"""
        self.assertIn('return "model_offscript"', SERVER_SRC)
        sweep = SERVER_SRC.split("quota_threshold = ", 1)[1][:1200]
        self.assertIn("'model_offscript'", sweep, "没进 1h 快清名单，等于没改")

    def test_the_verdict_travels_from_the_model_exit_to_the_key(self):
        """判据产生在模型出口，落账在 do_extract 的 except —— 中间不能断（准则 23）。"""
        self.assertIn("with model_operation(\"ingest.episode\", operation_id) as op_tally",
                      SERVER_SRC)
        self.assertIn("model_tally = op_tally", SERVER_SRC)
        self.assertIn('offscript=bool(model_tally.get("offscript"))', SERVER_SRC)

    def test_status_keeps_the_two_counts_apart(self):
        """外壳修正=救回来了，脱稿=这一轮彻底废了。混成一个数就没法用。"""
        refinery_src = (ROOT / "kg_refinery.py").read_text("utf-8")
        self.assertIn('cur["offscript_responses"] = _offscript[0]', refinery_src)
        self.assertIn('"offscript_responses": offscript_total()', SERVER_SRC)
