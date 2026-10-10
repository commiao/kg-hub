"""The joint prompt plan must cover upstream summary targets without paid calls."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import attribute_summary_joint_probe as joint


class JointPlanTests(unittest.TestCase):
    def sample(self):
        now = datetime.now(timezone.utc)
        nodes = [EntityNode(
            name=f"file-{i}", group_id="test", labels=["Entity", "File"],
            attributes={"path": f"/{i}.py"}, summary="existing summary", created_at=now,
        ) for i in range(9)]
        nodes[0].summary = ""
        bare = EntityNode(name="untyped", group_id="test", labels=["Entity"],
                          attributes={}, summary="", created_at=now)
        nodes.append(bare)
        episode = EpisodicNode(name="probe", group_id="test", source=EpisodeType.text,
                               source_description="test", content="Nine files and one concept.",
                               valid_at=now, created_at=now)
        return {"sd": "test", "sid": "one", "operation_id": "op-1",
                "nodes": [node.model_dump(mode="json") for node in nodes],
                "episode": episode.model_dump(mode="json"),
                "previous_episodes": [], "typed_count": 9}

    def test_summary_targets_are_computed_from_inputs_not_prior_model_answer(self):
        sample = self.sample()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE graphiti_stage_artifacts "
                           "(task_sd TEXT, task_sid TEXT, operation_id TEXT, "
                           "stage TEXT, artifact_json TEXT)")
                db.executemany("INSERT INTO graphiti_stage_artifacts VALUES (?,?,?,?,?)", [
                    ("test", "one", "op-1", "parallel_selected_round",
                     json.dumps({"round": 2})),
                    ("test", "one", "op-1:graph-round:2", "edge_phase",
                     json.dumps({"groups": [[], [], []]})),
                ])
            self.assertEqual(joint.summary_targets(path, sample),
                             {sample["nodes"][0]["uuid"], sample["nodes"][-1]["uuid"]})

    def test_joint_batches_cover_typed_and_untyped_targets_without_repair(self):
        sample = self.sample()
        targets = {sample["nodes"][0]["uuid"], sample["nodes"][-1]["uuid"]}
        requests = asyncio.run(joint.build_requests(sample, targets))
        self.assertEqual([len(request["uuids"]) for request in requests], [8, 1])
        self.assertEqual(set(requests[1]["summary_only_uuids"].values()),
                         {sample["nodes"][-1]["uuid"]})
        projections = []
        for request in requests:
            context = json.loads(request["messages"][1]["content"])
            for key, row in request["model"].model_fields.items():
                attributes = row.annotation.model_fields["attributes"].annotation
                self.assertTrue(all(field.is_required()
                                    for field in attributes.model_fields.values()))
            payload = {
                key: {"attributes": {
                          field: item["attributes"].get(field)
                          for field in request["model"].model_fields[key].annotation
                              .model_fields["attributes"].annotation.model_fields},
                      "summary": "Updated from source." if item["summary_required"] else None}
                for key, item in context["entities"].items()
            }
            projections.append(joint.validate_output(request, payload))
            non_target = next((key for key in payload
                               if key not in request["summary_targets"]), None)
            if non_target is not None:
                wrong_summary = json.loads(json.dumps(payload))
                wrong_summary[non_target]["summary"] = "Unexpected summary"
                with self.assertRaises((ValidationError, ValueError)):
                    joint.validate_output(request, wrong_summary)
            first_key = next(iter(request["model"].model_fields))
            field = next(iter(payload[first_key]["attributes"]), None)
            if field is not None:
                incomplete = json.loads(json.dumps(payload))
                del incomplete[first_key]["attributes"][field]
                with self.assertRaisesRegex(ValueError, "attribute fields are incomplete"):
                    joint.validate_output(request, incomplete)
            if request["summary_targets"]:
                bad = json.loads(json.dumps(payload))
                first = next(iter(request["summary_targets"]))
                bad[first]["summary"] = None
                with self.assertRaises(ValueError):
                    joint.validate_output(request, bad)
                bad = json.loads(json.dumps(payload))
                bad[first]["summary"] = "x" * (joint.MAX_SUMMARY_CHARS + 1)
                with self.assertRaises(ValidationError):
                    joint.validate_output(request, bad)
        self.assertEqual(sum(len(summaries) for _, summaries in projections), 2)
        self.assertEqual(sum(len(attributes) for attributes, _ in projections), 9)

    def test_execute_refuses_unapproved_paid_calls_before_transport(self):
        sample = self.sample()
        samples = [{**sample, "sid": f"sample-{index}"} for index in range(15)]
        with tempfile.TemporaryDirectory() as directory:
            holdout = Path(directory) / "holdout.json"
            holdout.write_text(json.dumps({"samples": samples}))
            with patch.object(joint, "summary_targets",
                              return_value={sample["nodes"][0]["uuid"]}), \
                 patch.object(joint.probe, "call") as paid:
                with self.assertRaisesRegex(RuntimeError, "at least 30 new calls"):
                    joint.main(["--holdout", str(holdout), "--journal", str(holdout),
                                "--save-dir", str(Path(directory) / "results"),
                                "--execute"])
                paid.assert_not_called()
                self.assertFalse((Path(directory) / "results").exists())

    def test_project_constraint_reaches_request_without_changing_file_schema(self):
        # Regression: a class mislabeled Project received a Java file path in v3.
        sample = self.sample()
        project = sample["nodes"][0]
        project.update(name="GlobalExceptionHandler", labels=["Entity", "Project"],
                       attributes={"path": None, "repo": "owner/service"})
        requests = asyncio.run(joint.build_requests(sample, {project["uuid"]}))
        fields = requests[0]["model"].model_fields
        project_schema = fields["entity_0"].annotation.model_fields["attributes"].annotation
        file_schema = fields["entity_1"].annotation.model_fields["attributes"].annotation
        description = project_schema.model_json_schema()["properties"]["path"]["description"]
        self.assertIn("Directory", description)
        self.assertIn("NOT a Project.path", description)
        self.assertIn("Preserve an existing", description)
        system = requests[0]["messages"][0]["content"]
        self.assertIn("OWN existing summary", system)
        self.assertIn("current explicit correction takes priority", system)
        self.assertIn("not automatic ownership", system)
        self.assertIn("planned, approved, locally tested", system)
        self.assertEqual(set(project_schema.model_fields), {"path", "repo"})
        self.assertTrue(all(field.is_required() for field in project_schema.model_fields.values()))
        self.assertEqual(project_schema(path=None, repo="owner/service").path, None)
        self.assertEqual(file_schema(path="src/Handler.java", project_id="service").path,
                         "src/Handler.java")
        context = json.loads(requests[0]["messages"][1]["content"])
        self.assertEqual(context["entities"]["entity_0"]["attributes"], project["attributes"])
        # Field descriptions guide the model; they are not semantic validators.
        self.assertEqual(project_schema(path="src/Handler.java", repo=None).path,
                         "src/Handler.java")

    def test_accepted_summary_survives_real_graphiti_persistence_unchanged(self):
        sample = self.sample()
        target = sample["nodes"][0]["uuid"]
        request = asyncio.run(joint.build_requests(sample, {target}))[0]
        context = json.loads(request["messages"][1]["content"])
        payload = {
            key: {"attributes": {
                name: context["entities"][key]["attributes"].get(name)
                for name in field.annotation.model_fields["attributes"].annotation.model_fields},
                "summary": None}
            for key, field in request["model"].model_fields.items()
        }
        current_fact = "当前批准修复①②③④，时区问题仍未确认。"
        text = current_fact + "旧" * (joint.MAX_SUMMARY_CHARS - len(current_fact))
        payload["entity_0"]["summary"] = text
        _, summaries = joint.validate_output(request, payload)
        self.assertEqual(joint.truncate_at_sentence(summaries[target], joint.MAX_SUMMARY_CHARS), text)
        payload["entity_0"]["summary"] = text + "溢"
        with self.assertRaises(ValidationError):
            joint.validate_output(request, payload)


if __name__ == "__main__":
    unittest.main()
