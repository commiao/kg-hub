import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from utils.batched_edge_timestamps import _TimestampBatch
from utils.graphiti_stage_names import graphiti_model_stage


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate_response(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response


class TimestampBatchTests(unittest.TestCase):
    def test_multiple_edges_use_one_model_call_and_keep_order(self):
        async def run():
            original_calls = []
            async def original(*args):
                original_calls.append(args)
            batch = _TimestampBatch(original, delay=0)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
            edges = [SimpleNamespace(fact=f"fact {i}", valid_at=None, invalid_at=None) for i in range(3)]
            client = FakeClient({"timestamps": [
                {"index": 0, "valid_at": "2026-09-01T00:00:00Z", "invalid_at": None},
                {"index": 1, "valid_at": None, "invalid_at": "2026-09-02T00:00:00Z"},
                {"index": 2, "valid_at": "2026-09-03T00:00:00Z", "invalid_at": None},
            ]})
            await asyncio.gather(*(batch.extract(client, edge, episode) for edge in edges))
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(original_calls, [])
            self.assertEqual([e.valid_at.day if e.valid_at else None for e in edges], [1, None, 3])
            self.assertEqual(edges[1].invalid_at.day, 2)
            self.assertEqual(client.calls[0][1]["prompt_name"], "extract_edges.extract_timestamps_batch")
        asyncio.run(run())

    def test_single_edge_uses_pinned_original(self):
        async def run():
            calls = []
            async def original(*args):
                calls.append(args)
            batch = _TimestampBatch(original, delay=0)
            edge = SimpleNamespace(fact="fact", valid_at=None, invalid_at=None)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime.now(timezone.utc))
            await batch.extract(FakeClient({}), edge, episode)
            self.assertEqual(len(calls), 1)
        asyncio.run(run())

    def test_bad_batch_fails_without_partial_mutation(self):
        async def run():
            async def original(*args):
                raise AssertionError("no individual retry after a paid batch")
            batch = _TimestampBatch(original, delay=0)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime.now(timezone.utc))
            edges = [SimpleNamespace(fact=f"fact {i}", valid_at=None, invalid_at=None) for i in range(2)]
            client = FakeClient({"timestamps": [
                {"index": 0, "valid_at": "2026-09-01T00:00:00Z"},
                {"index": 1, "valid_at": "not-a-date"},
            ]})
            results = await asyncio.gather(*(batch.extract(client, edge, episode) for edge in edges), return_exceptions=True)
            self.assertTrue(all(isinstance(item, ValueError) for item in results))
            self.assertTrue(all(edge.valid_at is None for edge in edges))
            self.assertEqual(len(client.calls), 1)
        asyncio.run(run())

    def test_prompt_is_part_of_replayable_edge_phase(self):
        self.assertEqual(graphiti_model_stage("extract_edges.extract_timestamps_batch"), "edge_phase")

    def test_swapped_identity_does_not_assign_wrong_edge(self):
        async def run():
            async def original(*args):
                raise AssertionError("no individual retry after a paid batch")
            batch = _TimestampBatch(original, delay=0)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime.now(timezone.utc))
            edges = [SimpleNamespace(fact=f"fact {i}", valid_at=None, invalid_at=None) for i in range(2)]
            client = FakeClient({"timestamps": [
                {"index": 1, "valid_at": "2026-09-01T00:00:00Z"},
                {"index": 0, "valid_at": "2026-09-02T00:00:00Z"},
            ]})
            results = await asyncio.gather(*(batch.extract(client, edge, episode) for edge in edges), return_exceptions=True)
            self.assertTrue(all(isinstance(item, RuntimeError) for item in results))
            self.assertTrue(all(edge.valid_at is None for edge in edges))
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
