"""Identity and source-evidence checks for the non-writing joint extraction pilot."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.joint_extraction_probe import select_pair, validate_result


class JointExtractionProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.backup = root / "ingest-backup.jsonl"
        self.journal = root / "model-attempts.sqlite3"
        with sqlite3.connect(self.journal) as db:
            db.execute("CREATE TABLE graphiti_stage_artifacts ("
                       "task_sd TEXT, task_sid TEXT, stage TEXT)")
        self.rows = [
            {"source_description": f"claude-mem obs id={i} project=alpha type=note",
             "source_obs_id": f"source-{i}", "reference_time": "2026-09-30T00:00:00Z",
             "episode_body": (f"Source {i} says A supports B. " * 12)}
            for i in (1, 2)
        ]
        self.backup.write_text("".join(json.dumps(row) + "\n" for row in self.rows))

    def test_selects_only_committed_sources_in_one_project(self) -> None:
        with sqlite3.connect(self.journal) as db:
            for row in self.rows:
                db.execute("INSERT INTO graphiti_stage_artifacts VALUES (?,?,?)",
                           (row["source_description"], row["source_obs_id"],
                            "graph_commit_receipt"))
        picked = select_pair(self.backup, self.journal)
        self.assertEqual({r["source_obs_id"] for r in picked}, {"source-1", "source-2"})

    def test_rejects_missing_or_duplicate_source(self) -> None:
        one = {"source_obs_id": "source-1", "entities": [
            {"name": "A", "summary": "A is in source 1."}], "facts": []}
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_result({"items": [one]}, self.rows)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_result({"items": [one, one]}, self.rows)

    def test_counts_unsupported_evidence_without_accepting_wrong_source(self) -> None:
        result = {"items": [
            {"source_obs_id": "source-1", "entities": [
                {"name": "A", "summary": "A supports B."},
                {"name": "B", "summary": "B is supported by A."}],
             "facts": [{"subject": "A", "relation": "supports", "object": "B",
                        "evidence": "Source 1 says A supports B."}]},
            {"source_obs_id": "source-2", "entities": [
                {"name": "A", "summary": "A supports B."},
                {"name": "B", "summary": "B is supported by A."}],
             "facts": [{"subject": "A", "relation": "supports", "object": "B",
                        "evidence": "Source 1 says A supports B."}]},
        ]}
        measured = validate_result(result, self.rows)
        self.assertEqual(measured, {"sources": 2, "entities": 4,
                                    "facts": 2, "evidence_miss": 1,
                                    "summary_chars": 66,
                                    "key_fact_total": 0, "key_fact_quoted": 0})

    def test_reports_literal_key_fact_quote_coverage(self) -> None:
        rows = [{**self.rows[0], "episode_body": (
            "Key facts:\n- Alpha supports beta in the new release.\n"
            "- Gamma was removed on Tuesday.\n\nProject: alpha")}, self.rows[1]]
        result = {"items": [
            {"source_obs_id": "source-1", "entities": [
                {"name": "Alpha", "summary": "Alpha supports beta."}],
             "facts": [{"subject": "Alpha", "relation": "supports", "object": "beta",
                        "evidence": "Alpha supports beta"}]},
            {"source_obs_id": "source-2", "entities": [], "facts": []},
        ]}
        measured = validate_result(result, rows)
        self.assertEqual((measured["key_fact_quoted"], measured["key_fact_total"]),
                         (1, 2))


if __name__ == "__main__":
    unittest.main()
