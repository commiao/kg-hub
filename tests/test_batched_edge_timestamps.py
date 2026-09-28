from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from utils.batched_edge_timestamps import install_batched_edge_timestamp_resolver


class Edge:
    def __init__(self, fact, valid_at=None, invalid_at=None):
        self.fact = fact
        self.valid_at = valid_at
        self.invalid_at = invalid_at


class ResolverBatchTests(unittest.IsolatedAsyncioTestCase):
    def _install(self, response_factory):
        calls = {"batch": 0, "single": 0, "resolver": 0}

        async def original_timestamp(llm_client, edge, episode):
            if edge.valid_at is not None or edge.invalid_at is not None:
                return
            calls["single"] += 1
            edge.valid_at = episode.valid_at

        async def original_resolver(clients, edges, episode, *args, **kwargs):
            calls["resolver"] += 1
            await asyncio.gather(*(
                edge_operations._extract_edge_timestamps(clients.llm_client, edge, episode)
                for edge in edges
            ))
            return edges

        class LLM:
            async def generate_response(self, messages, **kwargs):
                calls["batch"] += 1
                return await response_factory(messages, kwargs)

        pipeline = SimpleNamespace(resolve_extracted_edges=original_resolver)
        edge_operations = SimpleNamespace(
            _extract_edge_timestamps=original_timestamp,
            ensure_utc=lambda dt: dt.replace(tzinfo=timezone.utc)
                if dt.tzinfo is None else dt.astimezone(timezone.utc),
        )
        clients = SimpleNamespace(llm_client=LLM())
        install_batched_edge_timestamp_resolver(pipeline, edge_operations)
        return pipeline, edge_operations, clients, calls

    async def test_seventeen_facts_use_three_model_calls_and_preserve_indexed_dates(self):
        async def response(messages, kwargs):
            import json
            facts = json.loads(messages[1].content.split("FACTS JSON:\n", 1)[1])
            return {"items": [
                {"index": item["index"],
                 "valid_at": "2026-09-01T00:00:00Z" if item["index"] == 1 else None,
                 "invalid_at": None}
                for item in facts
            ]}

        pipeline, edge_operations, clients, calls = self._install(response)
        episode = SimpleNamespace(valid_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
        edges = [Edge(f"fact-{i}") for i in range(17)]
        result = await pipeline.resolve_extracted_edges(clients, edges, episode)
        self.assertEqual(len(result), 17)
        self.assertEqual(calls, {"batch": 3, "single": 0, "resolver": 1})
        self.assertIsNone(result[0].valid_at)
        self.assertEqual(result[1].valid_at, datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(result[9].valid_at, datetime(2026, 9, 1, tzinfo=timezone.utc))

    async def test_one_fact_uses_existing_single_edge_path(self):
        async def response(messages, kwargs):
            raise AssertionError("single facts are not batched")

        pipeline, edge_operations, clients, calls = self._install(response)
        episode = SimpleNamespace(valid_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
        await pipeline.resolve_extracted_edges(clients, [Edge("one fact")], episode)
        self.assertEqual(calls, {"batch": 0, "single": 1, "resolver": 1})

    async def test_incomplete_batch_fails_closed_without_per_edge_retries(self):
        async def response(messages, kwargs):
            return {"items": [{"index": 0, "valid_at": None, "invalid_at": None}]}

        pipeline, edge_operations, clients, calls = self._install(response)
        episode = SimpleNamespace(valid_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
        edges = [Edge("fact-a"), Edge("fact-b")]
        with self.assertRaisesRegex(ValueError, "indices incomplete"):
            await pipeline.resolve_extracted_edges(clients, edges, episode)
        self.assertEqual(calls, {"batch": 1, "single": 0, "resolver": 0})

    async def test_existing_timestamps_are_not_in_batch_and_missing_one_stays_single(self):
        async def response(messages, kwargs):
            raise AssertionError("only one edge needs extraction")

        pipeline, edge_operations, clients, calls = self._install(response)
        episode = SimpleNamespace(valid_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
        edges = [Edge("known", valid_at=episode.valid_at), Edge("new")]
        await pipeline.resolve_extracted_edges(clients, edges, episode)
        self.assertEqual(calls, {"batch": 0, "single": 1, "resolver": 1})

    async def test_batch_skip_marker_does_not_suppress_other_concurrent_tasks(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def response(messages, kwargs):
            import json
            facts = json.loads(messages[1].content.split("FACTS JSON:\n", 1)[1])
            entered.set()
            await release.wait()
            return {"items": [
                {"index": item["index"], "valid_at": None, "invalid_at": None}
                for item in facts
            ]}

        pipeline, edge_operations, clients, calls = self._install(response)
        episode = SimpleNamespace(valid_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
        batch_task = asyncio.create_task(
            pipeline.resolve_extracted_edges(clients, [Edge("a"), Edge("b")], episode)
        )
        await entered.wait()
        await edge_operations._extract_edge_timestamps(clients.llm_client, Edge("other"), episode)
        release.set()
        await batch_task
        self.assertEqual(calls, {"batch": 1, "single": 1, "resolver": 1})


if __name__ == "__main__":
    unittest.main()
