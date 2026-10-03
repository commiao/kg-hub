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
            payload = {
                key: {"attributes": {
                          field: item["attributes"].get(field)
                          for field in request["model"].model_fields[key].annotation
                              .model_fields["attributes"].annotation.model_fields},
                      "summary": "Updated from source." if item["summary_required"] else None}
                for key, item in context["entities"].items()
            }
            projections.append(joint.validate_output(request, payload))
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


if __name__ == "__main__":
    unittest.main()
