import asyncio
from datetime import datetime, timezone
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import httpx
from anthropic import InternalServerError

from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType
from tools import attribute_prompt_probe as probe


class AttributePromptProbeTests(unittest.TestCase):
    def sample(self):
        now = datetime.now(timezone.utc)
        nodes = [EntityNode(name="same name", group_id="test", labels=["Entity", "File"],
                            attributes={"path": f"/{i}.py"}, created_at=now)
                 for i in range(9)]
        episode = EpisodicNode(name="probe", group_id="test", source=EpisodeType.text,
                              source_description="test", content="Nine files.",
                              valid_at=now, created_at=now)
        return {"nodes": [n.model_dump() for n in nodes], "episode": episode.model_dump(),
                "previous_episodes": []}

    def test_original_context_schema_language_and_uuid_mapping(self):
        sample = self.sample()
        baseline = asyncio.run(probe.capture(sample, 8))
        merged = asyncio.run(probe.capture(sample, 16))
        self.assertEqual([len(r["uuids"]) for r in baseline], [8, 1])
        self.assertEqual(merged[0]["uuids"], baseline[0]["uuids"] + baseline[1]["uuids"])
        self.assertEqual(baseline[0]["messages"][0], merged[0]["messages"][0])
        self.assertIn("NEVER hallucinate", merged[0]["messages"][0]["content"])
        def body(r):
            return json.loads(r["messages"][1]["content"].split("\n\nRespond with")[0])
        self.assertEqual(body(baseline[0])["episode_content"], body(merged[0])["episode_content"])
        self.assertEqual(body(baseline[1])["entities"]["entity_0"],
                         body(merged[0])["entities"]["entity_8"])
        self.assertEqual(baseline[1]["model"].model_fields["entity_0"].annotation,
                         merged[0]["model"].model_fields["entity_8"].annotation)
        payload = {f"entity_{i}": {"path": f"/{i}.py", "project_id": None} for i in range(9)}
        mapped = probe.flatten(merged[0], payload)
        self.assertEqual(mapped[merged[0]["uuids"][8]]["path"], "/8.py")
        self.assertEqual(sample["nodes"][0]["attributes"], {"path": "/0.py"})

    def test_differences_and_missing_are_not_silently_equal(self):
        result = probe.compare({"a": {"path": "x", "repo": None}}, {"a": {"path": None}})
        self.assertEqual(result["different_fields"], 2)
        self.assertEqual(result["equal_fields"], 0)
        self.assertTrue(result["differences"][1]["missing"])

    def test_equal_bad_results_do_not_hide_existing_value_loss(self):
        sample = self.sample()
        uuid = sample["nodes"][0]["uuid"]
        result = {uuid: {"path": None, "project_id": None}}
        self.assertEqual(probe.compare(result, result)["different_fields"], 0)
        self.assertEqual(probe.lost_existing_values(sample, result), [{"uuid": uuid, "field": "path"}])
        self.assertEqual(sample["nodes"][0]["attributes"]["path"], "/0.py")

    def test_unknown_receipt_prevents_paid_reissue(self):
        sample = self.sample()
        request = asyncio.run(probe.capture(sample, 16))[0]
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"ANTHROPIC_MODEL": "probe", "KG_HUB_MODEL_GATEWAY_TOKEN": "fake"}):
            with patch("anthropic.Anthropic", side_effect=RuntimeError("simulated pre-send crash")):
                with self.assertRaisesRegex(RuntimeError, "simulated"):
                    probe.call(request, Path(folder))
            with patch("anthropic.Anthropic") as client:
                with self.assertRaisesRegex(RuntimeError, "unknown prior outcome"):
                    probe.call(request, Path(folder))
                client.assert_not_called()

    def test_completed_receipt_reuses_result_without_model_call(self):
        request = asyncio.run(probe.capture(self.sample(), 16))[0]
        response = SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="EntityAttributeBatch", input={})],
                                   usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 1, "output_tokens": 2}),
                                   stop_reason="tool_use")
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
                "ANTHROPIC_MODEL": "probe", "KG_HUB_MODEL_GATEWAY_TOKEN": "fake"}):
            with patch("anthropic.Anthropic") as client:
                client.return_value.messages.create.return_value = response
                first = probe.call(request, Path(folder))
                second = probe.call(request, Path(folder))
                self.assertEqual(first, second)
                client.return_value.messages.create.assert_called_once()

    def test_audited_http_error_resume_keeps_exact_key_body_and_prior_evidence(self):
        request = asyncio.run(probe.capture(self.sample(), 16))[0]
        body = {"request_id": "test-request", "error": {"code": "configuration_error"}}
        error = InternalServerError("rejected", response=httpx.Response(
            503, request=httpx.Request("POST", "http://test/v1/messages")), body=body)
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
                "ANTHROPIC_MODEL": "probe", "KG_HUB_MODEL_GATEWAY_TOKEN": "fake"}):
            with patch("anthropic.Anthropic") as client:
                create = client.return_value.messages.create
                create.side_effect = error
                with self.assertRaises(InternalServerError):
                    probe.call(request, Path(folder))
                receipt = next(Path(folder).glob("*.json"))
                with self.assertRaisesRegex(RuntimeError, "unknown prior outcome"):
                    probe.call(request, Path(folder), "wrong-digest")
                with self.assertRaises(InternalServerError):
                    probe.call(request, Path(folder), receipt.stem)
                self.assertEqual(create.call_args_list[0], create.call_args_list[1])
                saved = json.loads(receipt.read_text())
                self.assertEqual(saved["prior_error"]["request_id"], "test-request")
                with self.assertRaisesRegex(RuntimeError, "unknown prior outcome"):
                    probe.call(request, Path(folder), receipt.stem)
                self.assertEqual(create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
