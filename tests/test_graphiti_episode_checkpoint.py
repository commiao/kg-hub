"""A resumed episode uses the exact previous-episode context it first saw."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from utils.graphiti_episode_checkpoint import add_episode_with_context_checkpoint
from utils.model_attempt_journal import ModelAttemptJournal


class Graphiti:
    def __init__(self):
        self.previous = ["episode-1"]
        self.calls = []
        self.retrieves = 0

    async def retrieve_episodes(self, *args, **kwargs):
        self.retrieves += 1
        return [type("Episode", (), {"uuid": value})() for value in self.previous]

    async def add_episode(self, *, previous_episode_uuids=None, **kwargs):
        self.calls.append((previous_episode_uuids, kwargs))
        return "saved"


class IncompatibleGraphiti(Graphiti):
    async def add_episode(self, *, name):
        raise AssertionError("must fail before calling incompatible Graphiti")


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_frozen_context(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            graphiti = Graphiti()
            kwargs = dict(name="name", episode_body="body", source_description="source",
                          reference_time=datetime(2026, 9, 26, tzinfo=timezone.utc),
                          group_id="kg_hub", source="text")
            for _ in range(2):
                result = await add_episode_with_context_checkpoint(
                    graphiti, journal, task_sd="source", task_sid="id-1",
                    operation_id="operation", relevant_schema_limit=5, **kwargs)
                self.assertEqual(result, "saved")
                graphiti.previous = ["episode-2"]
            self.assertEqual(graphiti.retrieves, 1)
            self.assertEqual([call[0] for call in graphiti.calls],
                             [["episode-1"], ["episode-1"]])
            with self.assertRaises(RuntimeError):
                await add_episode_with_context_checkpoint(
                    graphiti, journal, task_sd="source", task_sid="id-1",
                    operation_id="operation", relevant_schema_limit=5,
                    **{**kwargs, "episode_body": "changed"})

    async def test_incompatible_graphiti_fails_before_model_or_graph_write(self):
        with tempfile.TemporaryDirectory() as temp:
            graphiti = IncompatibleGraphiti()
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            with self.assertRaisesRegex(RuntimeError, "lacks previous_episode_uuids"):
                await add_episode_with_context_checkpoint(
                    graphiti, journal, task_sd="source", task_sid="id-1",
                    operation_id="operation", relevant_schema_limit=5,
                    name="name", episode_body="body", source_description="source",
                    reference_time=datetime.now(timezone.utc), group_id="kg_hub",
                    source="text")
            self.assertEqual(graphiti.retrieves, 0)


if __name__ == "__main__":
    unittest.main()
