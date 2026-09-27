"""Manual recovery must select only the input tied to a durable checkpoint."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from model_gateway_client import stable_operation_id
from utils.graphiti_episode_checkpoint import _input_digest
from utils.ingest_recovery import recover_checkpointed_input
from utils.model_attempt_journal import ModelAttemptJournal


class IngestRecoveryTests(unittest.TestCase):
    def test_conflicting_backup_entries_do_not_select_newest_by_guess(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ingest.jsonl"
            journal = ModelAttemptJournal(Path(temp) / "model-attempts.sqlite3")
            epoch = "2026-09-26T00:00:00+00:00"
            reference = datetime(2026, 9, 25, tzinfo=timezone.utc)
            entries = [
                {"source_description": "source", "source_obs_id": "id-1",
                 "name": "episode", "episode_body": "original",
                 "reference_time": reference.isoformat()},
                {"source_description": "source", "source_obs_id": "id-1",
                 "name": "episode", "episode_body": "later-different",
                 "reference_time": reference.isoformat()},
            ]
            path.write_text("\n".join(json.dumps(row) for row in entries) + "\n")
            op = stable_operation_id("source", "id-1", "episode", "original", epoch)
            digest = _input_digest({"name": "episode", "episode_body": "original",
                "source_description": "source", "reference_time": reference,
                "group_id": "kg_hub"})
            journal.save_episode_context("source", "id-1", op, digest, [])
            recovered = recover_checkpointed_input(
                path, journal, source_description="source", source_obs_id="id-1",
                episode_name="episode", created_at=epoch,
                stable_operation_id=stable_operation_id,
                group_id="kg_hub", source_type="text")
            self.assertEqual(recovered["episode_body"], "original")

    def test_without_checkpoint_recovery_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ingest.jsonl"
            path.write_text(json.dumps({"source_description": "source",
                "source_obs_id": "id-1", "name": "episode",
                "episode_body": "body",
                "reference_time": "2026-09-25T00:00:00+00:00"}) + "\n")
            journal = ModelAttemptJournal(Path(temp) / "model-attempts.sqlite3")
            with self.assertRaisesRegex(RuntimeError, "no exact checkpointed"):
                recover_checkpointed_input(
                    path, journal, source_description="source", source_obs_id="id-1",
                    episode_name="episode", created_at="epoch",
                    stable_operation_id=stable_operation_id,
                    group_id="kg_hub", source_type="text")


if __name__ == "__main__":
    unittest.main()
