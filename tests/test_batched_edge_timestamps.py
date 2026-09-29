import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import graphiti_core.graphiti as pipeline
from graphiti_core.utils.maintenance import edge_operations as ops
from utils.batch_answer import batch_fallbacks_total
from utils.batched_edge_timestamps import _TimestampBatch
from utils.batched_edge_timestamps import install
from utils.graphiti_stage_names import graphiti_model_stage


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate_response(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response


class ValidatingClient(FakeClient):
    """Like the production client: the response model is enforced on return."""

    async def generate_response(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return kwargs["response_model"](**self.response).model_dump()


class RaisingClient(FakeClient):
    async def generate_response(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        raise self.response


class TimestampBatchTests(unittest.TestCase):
    def test_multiple_edges_use_one_model_call_and_keep_order(self):
        async def run():
            original_calls = []
            async def original(*args):
                original_calls.append(args)
            batch = _TimestampBatch(original)
            batch.set_expected_edges(3)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
            edges = [SimpleNamespace(fact=f"fact {i}", source_node_uuid="s",
                                     target_node_uuid="t", valid_at=None,
                                     invalid_at=None) for i in range(3)]
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

    def test_arrival_timing_keeps_chunk_membership_stable_for_replay(self):
        async def run(order):
            async def original(*args):
                original_calls.append(args[1].fact)
            original_calls = []
            batch = _TimestampBatch(original, max_items=2)
            batch.set_expected_edges(len(order))
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
            edges = {fact: SimpleNamespace(fact=fact, source_node_uuid="s",
                                             target_node_uuid="t", valid_at=None,
                                             invalid_at=None)
                     for fact, _ in order}
            client = FakeClient({"timestamps": [
                {"index": 0, "valid_at": "2026-09-01T00:00:00Z"},
                {"index": 1, "valid_at": "2026-09-02T00:00:00Z"},
            ]})
            async def submit(fact, delay):
                await asyncio.sleep(delay)
                await batch.extract(client, edges[fact], episode)
            await asyncio.gather(*(submit(fact, delay) for fact, delay in order))
            content = [call[0][-1].content for call in client.calls]
            assignments = {fact: edge.valid_at.day if edge.valid_at else None
                           for fact, edge in edges.items()}
            return content, assignments, original_calls

        async def check():
            forward = await run([("e", 0.000), ("c", 0.003), ("a", 0.006),
                                 ("d", 0.009), ("b", 0.012)])
            reverse = await run([("b", 0.000), ("d", 0.003), ("a", 0.006),
                                 ("c", 0.009), ("e", 0.012)])
            self.assertEqual(forward[0], reverse[0])
            self.assertEqual(forward[1], {"a": 1, "b": 2, "c": 1, "d": 2, "e": None})
            self.assertEqual(reverse[1], forward[1])
            self.assertEqual(forward[2], reverse[2])
        asyncio.run(check())

    def test_requests_are_bounded_and_keep_edge_assignment(self):
        async def run():
            original_calls = []
            async def original(*args):
                original_calls.append(args)
            batch = _TimestampBatch(original, max_items=2)
            batch.set_expected_edges(5)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
            edges = [SimpleNamespace(fact=f"fact {i}", source_node_uuid="s",
                                     target_node_uuid="t", valid_at=None,
                                     invalid_at=None) for i in range(5)]
            client = FakeClient({"timestamps": [
                {"index": 0, "valid_at": "2026-09-01T00:00:00Z"},
                {"index": 1, "valid_at": "2026-09-02T00:00:00Z"},
            ]})
            await asyncio.gather(*(batch.extract(client, edge, episode) for edge in edges))
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(len(original_calls), 1)
            self.assertEqual(batch.batch_requests, 2)
            self.assertEqual(batch.batched_edges, 4)
            self.assertEqual([edge.valid_at.day if edge.valid_at else None for edge in edges],
                             [1, 2, 1, 2, None])
        asyncio.run(run())

    def test_single_edge_uses_pinned_original(self):
        async def run():
            calls = []
            async def original(*args):
                calls.append(args)
            batch = _TimestampBatch(original)
            batch.set_expected_edges(1)
            edge = SimpleNamespace(fact="fact", source_node_uuid="s",
                                   target_node_uuid="t", valid_at=None,
                                   invalid_at=None)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime.now(timezone.utc))
            await batch.extract(FakeClient({}), edge, episode)
            self.assertEqual(len(calls), 1)
        asyncio.run(run())

    def test_bad_batch_fails_without_partial_mutation(self):
        async def run():
            async def original(*args):
                raise AssertionError("no individual retry after a paid batch")
            batch = _TimestampBatch(original)
            batch.set_expected_edges(2)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime.now(timezone.utc))
            edges = [SimpleNamespace(fact=f"fact {i}", source_node_uuid="s",
                                     target_node_uuid="t", valid_at=None,
                                     invalid_at=None) for i in range(2)]
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

    def test_swapped_identity_reasks_each_edge_without_assigning_batch_values(self):
        async def run():
            asked = []
            async def original(client, edge, episode):
                asked.append(edge.fact)
            batch = _TimestampBatch(original)
            batch.set_expected_edges(2)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime.now(timezone.utc))
            edges = [SimpleNamespace(fact=f"fact {i}", source_node_uuid="s",
                                     target_node_uuid="t", valid_at=None,
                                     invalid_at=None) for i in range(2)]
            client = FakeClient({"timestamps": [
                {"index": 1, "valid_at": "2026-09-01T00:00:00Z"},
                {"index": 0, "valid_at": "2026-09-02T00:00:00Z"},
            ]})
            await asyncio.gather(*(batch.extract(client, edge, episode) for edge in edges))
            self.assertEqual(sorted(asked), ["fact 0", "fact 1"])
            self.assertTrue(all(edge.valid_at is None for edge in edges))
            self.assertEqual(batch.batch_requests, 0)
        asyncio.run(run())

    def test_schema_invalid_batch_reasks_each_edge(self):
        async def run():
            asked = []
            async def original(client, edge, episode):
                asked.append(edge.fact)
            batch = _TimestampBatch(original)
            batch.set_expected_edges(2)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime.now(timezone.utc))
            edges = [SimpleNamespace(fact=f"fact {i}", source_node_uuid="s",
                                     target_node_uuid="t", valid_at=None,
                                     invalid_at=None) for i in range(2)]
            client = ValidatingClient({"timestamps": "not a list"})
            before = batch_fallbacks_total().get("edge_timestamps", 0)
            await asyncio.gather(*(batch.extract(client, edge, episode) for edge in edges))
            self.assertEqual(sorted(asked), ["fact 0", "fact 1"])
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(batch_fallbacks_total().get("edge_timestamps", 0) - before, 1)
        asyncio.run(run())

    def test_transport_failure_is_not_reasked(self):
        async def run():
            async def original(*args):
                raise AssertionError("a transport failure must not trigger per-edge calls")
            batch = _TimestampBatch(original)
            batch.set_expected_edges(2)
            episode = SimpleNamespace(uuid="episode-1", valid_at=datetime.now(timezone.utc))
            edges = [SimpleNamespace(fact=f"fact {i}", source_node_uuid="s",
                                     target_node_uuid="t", valid_at=None,
                                     invalid_at=None) for i in range(2)]
            client = RaisingClient(ConnectionError("gateway down"))
            results = await asyncio.gather(*(batch.extract(client, edge, episode) for edge in edges),
                                           return_exceptions=True)
            self.assertTrue(all(isinstance(item, ConnectionError) for item in results))
        asyncio.run(run())

    def test_installed_batch_waits_for_all_resolvers_before_chunking(self):
        async def fake_resolve_edge(llm_client, edge, episode):
            await asyncio.sleep(edge.delay)
            await ops._extract_edge_timestamps(llm_client, edge, episode)
            return edge, [], []

        async def fake_resolve(clients, extracted_edges, episode, *args, **kwargs):
            return await ops.semaphore_gather(*[
                ops.resolve_extracted_edge(clients.llm_client, edge, episode)
                for edge in extracted_edges
            ])

        async def run(order):
            episode = SimpleNamespace(
                uuid="episode-replay",
                valid_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
            )
            edges = [SimpleNamespace(
                fact=fact, source_node_uuid="s", target_node_uuid="t",
                valid_at=None, invalid_at=None, delay=delay,
            ) for fact, delay in order]
            client = FakeClient({"timestamps": [
                {"index": 0, "valid_at": "2026-09-01T00:00:00Z"},
                {"index": 1, "valid_at": "2026-09-02T00:00:00Z"},
                {"index": 2, "valid_at": "2026-09-03T00:00:00Z"},
            ]})
            result = await ops.resolve_extracted_edges(
                SimpleNamespace(llm_client=client), edges, episode)
            return result, client.calls

        async def check():
            original_gather = ops.semaphore_gather
            with (patch.object(ops, "resolve_extracted_edges", fake_resolve),
                  patch.object(ops, "resolve_extracted_edge", fake_resolve_edge),
                  patch.object(ops, "semaphore_gather", original_gather),
                  patch.object(pipeline, "resolve_extracted_edges", fake_resolve)):
                install(100)
                first = await run([("a", 0.000), ("b", 0.020), ("c", 0.040)])
                second = await run([("c", 0.000), ("b", 0.020), ("a", 0.040)])
            first_payloads = [call[0][-1].content for call in first[1]]
            second_payloads = [call[0][-1].content for call in second[1]]
            self.assertEqual(first_payloads, second_payloads)
            self.assertEqual(len(first[1]), 1)
            self.assertEqual(first[1][0][1]["prompt_name"],
                             "extract_edges.extract_timestamps_batch")
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
