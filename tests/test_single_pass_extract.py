"""网关单次抽取（KG_HUB_INGEST_MODE=single_pass）：提示组装、一次调用、解析与容错、落图参数、
来源标记、解析失败路径、开关默认值。

前半部分只依赖 utils/single_pass_extract（假网关客户端）；后半部分要导入 kg_hub_server，
本机没有 graphiti_core 时整段跳过（与 test_ingest_extracted_handler 同一做法）。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import contextlib
import inspect
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from pydantic import ValidationError

import model_gateway_client as mgc
from utils import single_pass_extract as sp
from utils.extracted_ingest import WriteResult, normalize_extraction, write_extraction

try:
    import graphiti_core  # noqa: F401
    import kg_hub_server as srv
    from graphiti_client import SingleAttemptAnthropicClient
    HAVE = True
except Exception:  # noqa: BLE001
    HAVE = False

REF = datetime(2026, 10, 19, 1, 2, 3, tzinfo=timezone.utc)
GOOD = {"entities": [{"name": "kg-hub", "type": "Project", "summary": "中央知识图"},
                     {"name": "FalkorDB", "type": "Tool", "summary": "图库"}],
        "edges": [{"source": "kg-hub", "name": "depends_on", "target": "FalkorDB",
                   "fact": "kg-hub 把图存在 FalkorDB"}]}


class FakeLLM:
    """只记录调用、按脚本回答的网关客户端替身（generate_response 契约同 graphiti LLMClient）。"""

    def __init__(self, answer=None, error=None):
        self.answer = answer if answer is not None else GOOD
        self.error = error
        self.calls = []

    async def generate_response(self, messages, response_model=None, max_tokens=None,
                                model_size=None, group_id=None, prompt_name=None):
        self.calls.append({"messages": messages, "response_model": response_model,
                           "prompt_name": prompt_name, "max_tokens": max_tokens,
                           "stage": mgc._model_stage.get()})
        if self.error is not None:
            raise self.error
        return self.answer


def _extract(llm, **kw):
    return asyncio.run(sp.extract_single_pass(
        llm, name=kw.get("name", "claude-mem-obs-7"), episode_body=kw.get("body", "正文 body"),
        source_description=kw.get("sd", "claude-mem obs id=7"), reference_time=REF))


class PromptTest(unittest.TestCase):
    def test_prompt_file_has_version_and_rules(self):
        text, version = sp.load_prompt()
        self.assertRegex(version, r"^agent-v\d+$")
        self.assertIn("实体类型", text)
        self.assertIn("relates_to", text)

    def test_messages_system_is_prompt_plus_single_call_addendum(self):
        msgs = sp.build_messages("PROMPT 正文", name="n1", episode_body="BODY", source_description="sd1",
                                 reference_time=REF)
        self.assertEqual([m.role for m in msgs], ["system", "user"])
        self.assertTrue(msgs[0].content.startswith("PROMPT 正文\n\n"))
        self.assertIn("不要 `items` 包装", msgs[0].content)
        for needle in ("观测名称：n1", "来源：sd1", "参考时间：2026-10-19T01:02:03+00:00", "观测正文：\nBODY"):
            self.assertIn(needle, msgs[1].content)

    def test_resolve_ingest_mode_defaults_to_graphiti_and_rejects_typos(self):
        self.assertEqual(sp.resolve_ingest_mode(None), "graphiti")
        self.assertEqual(sp.resolve_ingest_mode(""), "graphiti")
        self.assertEqual(sp.resolve_ingest_mode(" Single_Pass "), "single_pass")
        with self.assertRaisesRegex(RuntimeError, "KG_HUB_INGEST_MODE"):
            sp.resolve_ingest_mode("singlepass")
        with mock.patch.dict(_os.environ, {}, clear=False):
            _os.environ.pop("KG_HUB_INGEST_MODE", None)
            self.assertEqual(sp.ingest_mode_from_env(), "graphiti")


class ExtractTest(unittest.TestCase):
    def test_one_call_with_tool_schema_and_single_pass_stage(self):
        llm = FakeLLM()
        r = _extract(llm)
        self.assertEqual(len(llm.calls), 1)
        call = llm.calls[0]
        self.assertIs(call["response_model"], sp.SinglePassExtraction)
        self.assertEqual(call["prompt_name"], "single_pass.extract")
        self.assertEqual(call["stage"], "single_pass")     # 账本里与 graphiti 的五个阶段分开
        self.assertEqual(call["messages"][0].role, "system")
        self.assertIn("正文 body", call["messages"][1].content)
        schema = sp.SinglePassExtraction.model_json_schema()
        self.assertEqual(set(schema["properties"]), {"entities", "edges"})
        self.assertEqual(schema["required"], ["entities"])
        self.assertEqual([e.name for e in r.entities], ["kg-hub", "FalkorDB"])
        self.assertEqual((r.edges[0].source, r.edges[0].name, r.edges[0].target), ("kg-hub", "depends_on", "FalkorDB"))
        self.assertEqual(r.prompt_version, sp.load_prompt()[1])
        self.assertIsNone(mgc._model_stage.get())          # stage 不外泄到调用方

    def test_coercion_keeps_paid_answer_usable(self):
        raw = {"items": [{"id": 29995, "entities": [
            {"name": "  kg-hub ", "type": "Project"},               # 缺 summary
            {"name": "x" * 300, "type": "", "summary": None},       # 超长名、空类型
            {"name": "", "type": "Tool"}, "garbage",                # 空名、非对象
        ], "edges": [
            {"source": "kg-hub", "name": "references", "target": "x" * 300},            # 缺 fact
            {"source": "kg-hub", "name": "references"},                                  # 缺 target
        ]}]}
        parsed = sp.SinglePassExtraction.model_validate(raw)
        self.assertEqual([e.name for e in parsed.entities], ["kg-hub", "x" * 200])
        self.assertEqual(parsed.entities[0].summary, "")
        self.assertEqual(parsed.entities[1].type, "Concept")
        self.assertEqual(len(parsed.edges), 1)
        self.assertEqual(parsed.edges[0].fact, f"kg-hub references {'x' * 200}")
        many = {"entities": [{"name": f"e{i}", "type": "Tool"} for i in range(80)], "edges": []}
        self.assertEqual(len(sp.SinglePassExtraction.model_validate(many).entities), 60)
        self.assertEqual(sp.SinglePassExtraction.model_validate({"entities": []}).entities, [])

    def test_shape_errors_still_fail_validation(self):
        for bad in ({"edges": []}, {"entities": "kg-hub"}, {"items": []}, {"result": {"entities": []}}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                sp.SinglePassExtraction.model_validate(bad)

    def test_parse_failure_propagates_without_second_call(self):
        llm = FakeLLM(error=ValueError("Could not extract structured data"))
        with self.assertRaisesRegex(ValueError, "structured data"):
            _extract(llm)
        self.assertEqual(len(llm.calls), 1)

    @unittest.skipUnless(HAVE, "需要 graphiti_core（容器或 venv）")
    def test_real_client_rejects_unusable_answer_once(self):
        """走真正的 SingleAttemptAnthropicClient：形状错误 → reject_model_result → 抛出，不追问。"""
        calls, rejected = [], []

        async def answers_garbage(*args):
            calls.append(args)
            return {"result": "not the schema"}, 11, 5

        async def fake_reject(sink):
            rejected.append(sink)
            return True

        client = object.__new__(SingleAttemptAnthropicClient)
        client.max_tokens = 123
        client._generate_response = answers_garbage
        client.token_tracker = mock.Mock()
        with mock.patch("graphiti_client.reject_model_result", fake_reject), \
                self.assertRaises(ValidationError):
            _extract(client)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(rejected), 1)
        self.assertIs(calls[0][1], sp.SinglePassExtraction)


class WriteProvenanceTest(unittest.TestCase):
    def test_write_extraction_stamps_extraction_mode_on_episode_nodes_and_edges(self):
        class Driver:
            def __init__(self):
                self.calls = []

            async def execute_query(self, cypher, **params):
                self.calls.append((cypher, params))
                if cypher.startswith("MATCH (n:Entity) WHERE n.group_id") or "cosineDistance" in cypher \
                        or cypher.startswith("MATCH (a:Entity {uuid: $src})"):
                    return [], None, None
                return [{"uuid": params.get("uuid") or "x"}], None, None

        class Embedder:
            async def create_batch(self, texts):
                return [[0.1, 0.2] for _ in texts]

        n = normalize_extraction(sp.SinglePassExtraction.model_validate(GOOD).entities,
                                 sp.SinglePassExtraction.model_validate(GOOD).edges,
                                 {"Project", "Tool"}, {"depends_on"})
        d = Driver()
        asyncio.run(write_extraction(d, Embedder(), n, name="claude-mem-obs-7", episode_body="b",
                                     source_description="sd", reference_time=REF,
                                     extractor_model="gateway:kg_hub.entity_extract", group_id="kg_hub",
                                     prompt_version="agent-v3", extraction_mode="gateway_single"))
        ep = [p for c, p in d.calls if c.startswith("MERGE (e:Episodic")][0]
        self.assertEqual((ep["extraction_mode"], ep["extractor_model"], ep["prompt_version"]),
                         ("gateway_single", "gateway:kg_hub.entity_extract", "agent-v3"))
        self.assertIn("extraction_mode: $extraction_mode", [c for c, _ in d.calls if c.startswith("MERGE (e:Episodic")][0])
        nodes = [p["entity_data"] for c, p in d.calls if c.startswith("MERGE (n:Entity")]
        self.assertEqual({x["extraction_mode"] for x in nodes}, {"gateway_single"})
        edge = [p["edge_data"] for c, p in d.calls if c.startswith("MATCH (source:Entity")][0]
        self.assertEqual(edge["extraction_mode"], "gateway_single")

    def test_default_extraction_mode_stays_agent(self):
        self.assertEqual(inspect.signature(write_extraction).parameters["extraction_mode"].default, "agent")


def _body(**over):
    fields = dict(name="claude-mem-obs-7", episode_body="正文 body", source_description="claude-mem obs id=7",
                  reference_time="2026-10-19T01:02:03Z", source_obs_id="h7")
    fields.update(over)
    return srv.IngestBody(**fields)


@unittest.skipUnless(HAVE, "需要 graphiti_core（容器或 venv）")
class ServerModeTest(unittest.TestCase):
    """kg_hub_server 的接线：默认模式、分流、落图参数、来源标记、终态写法。"""

    def setUp(self):
        self.status_calls = []
        self.write_calls = []

        async def fake_status(graphiti, sd, sid, status, **kw):
            self.status_calls.append((sd, sid, status, kw))

        async def fake_write(driver, embedder, extraction, **kw):
            self.write_calls.append((extraction, kw))
            return WriteResult(episode_uuid="ep-7", nodes_new=2, nodes_reused=1, edges_new=1,
                               edges_reused=0, resolved={})

        @contextlib.asynccontextmanager
        async def no_lock(**kw):
            yield

        async def no_tags(*a, **k):
            return None

        class G:
            driver = mock.Mock()
            embedder = mock.Mock()
            llm_client = FakeLLM()

        self.g = G()
        self.patches = [
            mock.patch.object(srv, "INGEST_MODE", "single_pass"),
            mock.patch.object(srv, "update_ingested_key_status", fake_status),
            mock.patch.object(srv, "write_extraction", fake_write),
            mock.patch.object(srv, "_ingest_execution_lock", no_lock),
            mock.patch.object(srv, "_post_ingest_tags", no_tags),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_default_switch_is_graphiti_and_prompt_not_loaded(self):
        for p in self.patches:
            p.stop()
        self.patches = []
        self.assertEqual(srv.INGEST_MODE, "graphiti")
        self.assertIsNone(srv.SINGLE_PASS_PROMPT)
        self.assertFalse(srv._use_single_pass(_body()))
        req = mock.Mock()
        health = json.loads(asyncio.run(srv.health(req)).body)
        self.assertEqual(health["ingest_mode"], "graphiti")

    def test_single_pass_writes_with_gateway_provenance(self):
        asyncio.run(srv._do_extract_inner(self.g, _body(), REF, "epoch-1"))
        self.assertEqual(len(self.g.llm_client.calls), 1)
        self.assertEqual(self.g.llm_client.calls[0]["stage"], "single_pass")
        self.assertEqual(len(self.write_calls), 1)
        extraction, kw = self.write_calls[0]
        self.assertEqual([e.name for e in extraction.entities], ["kg-hub", "FalkorDB"])
        self.assertEqual([e.name for e in extraction.edges], ["depends_on"])
        self.assertEqual(kw["extraction_mode"], "gateway_single")
        self.assertEqual(kw["extractor_model"], srv.GATEWAY_MODEL_LABEL)
        self.assertEqual(kw["prompt_version"], sp.load_prompt()[1])
        self.assertEqual((kw["name"], kw["episode_body"], kw["source_description"], kw["group_id"], kw["reference_time"]),
                         ("claude-mem-obs-7", "正文 body", "claude-mem obs id=7", "kg_hub", REF))
        self.assertEqual(self.status_calls, [("claude-mem obs id=7", "h7", "ok",
                                              {"episode_uuid": "ep-7", "nodes": 3, "edges": 1})])

    def test_empty_extraction_still_records_episode_with_zero_counts(self):
        self.g.llm_client = FakeLLM(answer={"entities": [], "edges": []})
        asyncio.run(srv._do_extract_inner(self.g, _body(), REF, "epoch-1"))
        extraction, _ = self.write_calls[0]
        self.assertEqual((extraction.entities, extraction.edges), ([], []))
        self.assertEqual(self.status_calls[0][2], "ok")

    def test_parse_failure_marks_error_without_writing(self):
        self.g.llm_client = FakeLLM(error=ValueError("Could not extract structured data"))
        asyncio.run(srv._do_extract_inner(self.g, _body(), REF, "epoch-1"))
        self.assertEqual(self.write_calls, [])
        sd, sid, status, kw = self.status_calls[0]
        self.assertEqual(status, "error")
        self.assertIn("ValueError", kw["error_message"])

    def test_unknown_model_outcome_is_held_like_graphiti_mode(self):
        self.g.llm_client = FakeLLM(error=srv.NeedsReconciliation("step" * 8, "unknown", None))
        asyncio.run(srv._do_extract_inner(self.g, _body(), REF, "epoch-1"))
        sd, sid, status, kw = self.status_calls[0]
        self.assertEqual((status, kw["error_kind"]), ("needs_reconciliation", "model_outcome_unknown"))

    def test_predigest_routes_keep_graphiti_path(self):
        with mock.patch.object(srv, "PREDIGEST_ENABLED", True), \
                mock.patch.object(srv, "predigest_route", lambda name, body: "catalog"):
            self.assertFalse(srv._use_single_pass(_body()))
        with mock.patch.object(srv, "PREDIGEST_ENABLED", True), \
                mock.patch.object(srv, "predigest_route", lambda name, body: None):
            self.assertTrue(srv._use_single_pass(_body()))

    def test_worker_skips_stage_checkpoint_plan_in_single_pass(self):
        src = inspect.getsource(srv._run_recorded_extract)
        self.assertIn("if _use_single_pass(body):", src)
        self.assertIn("bind_workflow(None)", src)
        self.assertIn("if _use_single_pass(body):", inspect.getsource(srv._do_extract_inner))


if __name__ == "__main__":
    unittest.main()
