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
from pydantic import ValidationError

from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType
from tools import attribute_prompt_probe as probe


class AttributePromptProbeTests(unittest.TestCase):
    def delta_fixture(self):
        sample = self.sample()
        sample["nodes"] = [EntityNode(name="quota", group_id="test", labels=["Entity", "Concept"],
                                      attributes={"description": "Old hypothesis. Unrelated valid fact."}).model_dump()]
        return probe.delta_request(asyncio.run(probe.capture(sample, 16))[0])

    def test_delta_preserves_unchanged_and_applies_explicit_edits_atomically(self):
        request = self.delta_fixture()
        uuid = request["uuids"][0]
        old = request["starting_records"]["entity_0"]["description"]
        self.assertEqual(probe.apply_delta(request, {"entity_0": []})[uuid]["description"], old)
        output = probe.apply_delta(request, {"entity_0": [
            {"field": "description", "op": "replace", "old": "Old hypothesis.", "value": "Corrected fact."},
            {"field": "description", "op": "append", "old": None, "value": "Full condition AND exception."}]})
        self.assertEqual(output[uuid]["description"], "Corrected fact. Unrelated valid fact.\n\nFull condition AND exception.")
        self.assertEqual(request["starting_records"]["entity_0"]["description"], old)

    def test_delta_rejects_missing_entities_wrong_fields_and_unsafe_replacement(self):
        request = self.delta_fixture()
        cases = [{}, {"entity_0": [{"field": "path", "op": "set", "old": None, "value": "invented"}]},
                 {"entity_0": [{"field": "description", "op": "set", "old": None, "value": "lose old"}]},
                 {"entity_0": [{"field": "description", "op": "replace", "old": "not present", "value": "new"}]}]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                probe.apply_delta(request, payload)
        request["starting_records"]["entity_0"]["description"] = "repeat repeat"
        with self.assertRaisesRegex(ValueError, "exactly one"):
            probe.apply_delta(request, {"entity_0": [{"field": "description", "op": "replace", "old": "repeat", "value": "new"}]})

    def test_candidate_only_does_not_send_baseline_requests(self):
        sample = {**self.sample(), "sid": "fixture", "typed_count": 9}
        sample["nodes"] = [EntityNode.model_validate(n).model_dump(mode="json") for n in sample["nodes"]]
        sample["episode"] = EpisodicNode.model_validate(sample["episode"]).model_dump(mode="json")
        saved = {"elapsed": 1, "usage": {}, "stop_reason": "end_turn",
                 "payload": {f"entity_{i}": {"path": f"/{i}.py", "project_id": None} for i in range(9)}}
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "snapshot.json"
            snapshot.write_text(json.dumps({"sample": sample}))
            with patch("sys.argv", ["probe", "--execute", "--candidate-only", "--snapshot", str(snapshot),
                                    "--save-dir", folder]), patch.object(probe, "call", return_value=saved) as call, patch("builtins.print"):
                probe.main()
            self.assertEqual(call.call_count, 1)
            result = json.loads((Path(folder) / "fixture-candidate-only-comparison.json").read_text())
            self.assertEqual(result["comparison"], {"not_run": "candidate-only"})
            self.assertEqual(set(result["outputs"]), {"merged"})

    def test_source_quotes_keep_whole_joint_conditions_and_ignore_metadata(self):
        sample = self.sample()
        sample["nodes"] = [EntityNode(name=name, group_id="test", labels=["Entity", "Concept"],
                                      attributes={"description": "existing fact"}).model_dump()
                           for name in ("quota_mode", "userCost")]
        joint = "quotaMode==null AND userCost==0 -> allow; otherwise deny"
        sample["episode"]["content"] = "Key facts:\n- " + joint + "\n- quotaMode==1 -> allow\n\nProject: unrelated"
        request = asyncio.run(probe.capture(sample, 16))[0]
        probe.require_complete_fields(request)
        source = request["messages"][1]["content"]
        probe.add_source_quotes(request)
        self.assertEqual(request["required_source_quotes"]["entity_0"], [joint, "quotaMode==1 -> allow"])
        self.assertEqual(request["required_source_quotes"]["entity_1"], [joint])
        self.assertEqual(source, request["messages"][1]["content"])
        output = {u: {"description": joint} for u in request["uuids"]}
        self.assertEqual(probe.missing_source_quotes(request, output),
                         [{"uuid": request["uuids"][0], "quote": "quotaMode==1 -> allow"}])

    def test_stability_stops_on_unknown_outcome_instead_of_advancing_trial(self):
        sample = {**self.sample(), "sid": "fixture"}
        sample["nodes"] = [EntityNode.model_validate(n).model_dump(mode="json") for n in sample["nodes"]]
        sample["episode"] = EpisodicNode.model_validate(sample["episode"]).model_dump(mode="json")
        saved = {"body_digest": "fixed-body", "elapsed": 1, "usage": {}, "stop_reason": "end_turn",
                 "payload": {f"entity_{i}": {"path": f"/{i}.py", "project_id": None} for i in range(9)}}
        with tempfile.TemporaryDirectory() as folder, patch.object(probe, "call",
                side_effect=[saved, RuntimeError("unknown outcome")]) as call, patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "unknown outcome"):
                probe.stability(sample, Path(folder))
            self.assertEqual(call.call_count, 2)
            recorded = json.loads((Path(folder) / "fixture-stability-20261001.json").read_text())
            self.assertEqual([r["trial"] for r in recorded["trials"]], [1])

    def test_independent_trials_same_body_distinct_stable_keys_and_completed_control(self):
        request = asyncio.run(probe.capture(self.sample(), 16))[0]
        response = SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="EntityAttributeBatch", input={})],
                                   usage=SimpleNamespace(model_dump=lambda: {}), stop_reason="end_turn")
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
                "ANTHROPIC_MODEL": "probe", "KG_HUB_MODEL_GATEWAY_TOKEN": "fake"}):
            with patch("anthropic.Anthropic") as client:
                create = client.return_value.messages.create
                create.return_value = response
                with self.assertRaisesRegex(RuntimeError, "completed identical control"):
                    probe.call(request, Path(folder), trial_id="r1")
                create.assert_not_called()
                base = probe.call(request, Path(folder))
                one = probe.call(request, Path(folder), trial_id="r1")
                two = probe.call(request, Path(folder), trial_id="r2")
                self.assertEqual(one, probe.call(request, Path(folder), trial_id="r1"))
                self.assertEqual(create.call_count, 3)
                self.assertEqual(base["body_digest"], one["body_digest"])
                self.assertEqual(one["body_digest"], two["body_digest"])
                bodies = [dict(c.kwargs) for c in create.call_args_list]
                headers = [body.pop("extra_headers") for body in bodies]
                self.assertEqual(bodies[0], bodies[1])
                self.assertEqual(bodies[1], bodies[2])
                self.assertEqual(len({h["Idempotency-Key"] for h in headers}), 3)

    def test_compact_and_history_ablation_keep_current_source_and_entity_values(self):
        request = asyncio.run(probe.capture(self.sample(), 16))[0]
        body = json.loads(request["messages"][1]["content"])
        body["previous_episodes"] = ["Unrelated previous record"]
        request["messages"][1]["content"] = json.dumps(body)
        probe.add_output_template(request, seeded=True, compact=True)
        self.assertEqual(json.loads(request["messages"][1]["content"]), body)
        probe.omit_history(request)
        changed = json.loads(request["messages"][1]["content"])
        self.assertEqual(changed, {**body, "previous_episodes": []})
        self.assertEqual(body["previous_episodes"], ["Unrelated previous record"])

    def test_template_keeps_schema_and_source_and_duplicate_name_slots_separate(self):
        request = asyncio.run(probe.capture(self.sample(), 16))[0]
        control = asyncio.run(probe.capture(self.sample(), 16))[0]
        probe.require_complete_fields(control)
        user = request["messages"][1]["content"]
        probe.add_output_template(request)
        self.assertEqual(request["model"].model_json_schema(), control["model"].model_json_schema())
        self.assertEqual(request["messages"][1]["content"], user)
        sp = request["messages"][0]["content"]
        for i in range(9):
            self.assertIn(f'"entity_{i}": {{"path": null, "project_id": null}}', sp)
        self.assertNotIn("/0.py", sp)

    def test_seeded_template_uses_existing_values_without_changing_source(self):
        request = asyncio.run(probe.capture(self.sample(), 16))[0]
        user = request["messages"][1]["content"]
        probe.add_output_template(request, seeded=True)
        self.assertEqual(request["messages"][1]["content"], user)
        sp = request["messages"][0]["content"]
        for i in range(9):
            self.assertIn(f'"entity_{i}": {{"path": "/{i}.py", "project_id": null}}', sp)

    def test_complete_contract_rejects_missing_but_accepts_explicit_null(self):
        request = asyncio.run(probe.capture(self.sample(), 16))[0]
        original = request["model"]
        user_content = request["messages"][1]["content"]
        probe.require_complete_fields(request)
        payload = {f"entity_{i}": {"path": f"/{i}.py", "project_id": None}
                   for i in range(9)}
        self.assertEqual(len(probe.flatten(request, payload)), 9)
        del payload["entity_0"]["project_id"]
        with self.assertRaises(ValidationError):
            probe.flatten(request, payload)
        self.assertIsNone(original.model_validate(payload).entity_0.project_id)
        self.assertEqual(user_content, request["messages"][1]["content"])
        schema = request["model"].model_json_schema()
        self.assertEqual(set(schema["$defs"]["File"]["required"]), {"path", "project_id"})

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

    def test_raw_omission_is_distinct_from_explicit_null_before_defaults(self):
        request = asyncio.run(probe.capture(self.sample(), 16))[0]
        payload = {f"entity_{i}": {"path": f"/{i}.py", "project_id": None}
                   for i in range(9)}
        del payload["entity_0"]["project_id"]
        omitted = probe.missing_fields(request, payload)
        self.assertEqual(omitted, [{"uuid": request["uuids"][0], "field": "project_id"}])
        normalized = probe.flatten(request, payload)
        # Both normalize to null; comparing only normalized results loses evidence.
        self.assertIsNone(normalized[request["uuids"][0]]["project_id"])
        self.assertIsNone(normalized[request["uuids"][1]]["project_id"])
        self.assertNotIn("project_id", payload["entity_0"])

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

    def test_replay_only_missing_receipt_never_calls_provider(self):
        request = asyncio.run(probe.capture(self.sample(), 16))[0]
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"ANTHROPIC_MODEL": "probe"}):
            with patch("anthropic.Anthropic") as client:
                with self.assertRaisesRegex(RuntimeError, "replay-only"):
                    probe.call(request, Path(folder), replay_only=True)
                client.assert_not_called()
                self.assertEqual(list(Path(folder).iterdir()), [])

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
