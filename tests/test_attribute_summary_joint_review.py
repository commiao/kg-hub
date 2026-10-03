"""A frozen cohort never passes with an unreviewed entity or mis-scored failure."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

from graphiti_core.nodes import EntityNode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import attribute_summary_joint_review as review


class ReviewTests(unittest.TestCase):
    def test_review_packet_requires_each_typed_and_summary_check(self):
        now = datetime.now(timezone.utc)
        file_node = EntityNode(name="x.py", group_id="g", labels=["Entity", "File"],
                               attributes={"path": "old/x.py", "project_id": "old"},
                               summary="old fact", created_at=now).model_dump(mode="json")
        bare_node = EntityNode(name="concept", group_id="g", labels=["Entity"],
                               attributes={}, summary="", created_at=now).model_dump(mode="json")
        sample = {"sd": "g", "sid": "s", "operation_id": "o", "nodes": [file_node, bare_node],
                  "episode": {"content": "Project: new\nFiles read: x.py"},
                  "previous_episodes": []}
        row = review.make_sample_review(sample, [{"sample_sid": "s", "schema_valid": True,
            "attributes": {file_node["uuid"]: {"path": "x.py", "project_id": "new"}},
            "summaries": {bare_node["uuid"]: "new fact"}}], {bare_node["uuid"]})
        file_checks = row["entities"][0]["checks"]
        self.assertEqual(file_checks["old_value_and_correction"]["verdict"], "unreviewed")
        self.assertEqual(file_checks["project_and_file_ownership"]["verdict"], "unreviewed")
        self.assertEqual(file_checks["summary_completeness"]["verdict"], "not_applicable")
        self.assertEqual(row["entities"][1]["checks"]["summary_completeness"]["verdict"], "unreviewed")

    def test_gate_counts_complete_semantic_reviews_only(self):
        entity = {"uuid": "e", "labels": ["Entity", "File"],
                  "candidate_attributes": {"path": "x.py"}, "candidate_summary": "summary",
                  "checks": {name: {"verdict": "pass", "reason": "",
                                    "candidate_excerpt": "", "source_excerpt": ""}
                             for name in ("old_value_and_correction",
                                          "project_and_file_ownership",
                                          "source_grounding", "summary_completeness")}}
        cohort = {"holdout_sha256": "hash", "sample_count": 15,
                  "reviews": [{"sid": f"s{i}", "reviewer": "reviewer", "reviewed_at": "2026-10-03",
                               "entities": [copy.deepcopy(entity)], "verdict": "pass"}
                              for i in range(15)]}
        self.assertTrue(review.score_cohort(cohort)["gate_90_percent"])
        cohort["reviews"][0]["entities"][0]["checks"]["project_and_file_ownership"] = {
            "verdict": "fail", "reason": "wrong project", "candidate_excerpt": "wrong",
            "source_excerpt": "Project: correct"}
        cohort["reviews"][0]["verdict"] = "fail"
        self.assertEqual(review.score_cohort(cohort)["passed"], 14)
        cohort["reviews"][1]["entities"][0]["checks"]["old_value_and_correction"]["verdict"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "unreviewed"):
            review.score_cohort(cohort)
        cohort["reviews"][1]["entities"][0]["checks"]["old_value_and_correction"]["verdict"] = "not_applicable"
        with self.assertRaisesRegex(ValueError, "invalid or unreviewed"):
            review.score_cohort(cohort)


if __name__ == "__main__":
    unittest.main()
