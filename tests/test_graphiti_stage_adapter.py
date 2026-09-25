"""Stage snapshots use actual pinned Graphiti node types and resolver helpers."""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from utils.graphiti_stage_adapter import (
    StageArtifactStore, extract_nodes_with_snapshot, load_extracted_nodes,
    resolve_nodes_with_candidate_snapshot,
    extract_and_resolve_edges_with_snapshot, extract_attributes_with_snapshot,
    commit_episode_with_receipt,
)

HAS_GRAPHITI = importlib.util.find_spec("graphiti_core") is not None


@unittest.skipUnless(HAS_GRAPHITI, "requires pinned Graphiti environment")
class CandidateSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_edge_attribute_and_commit_receipts_replay(self):
        from graphiti_core import Graphiti
        from graphiti_core.edges import EntityEdge, EpisodicEdge
        from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType
        from graphiti_core.utils.maintenance import node_operations as ops

        with tempfile.TemporaryDirectory() as temp:
            store = StageArtifactStore(Path(temp) / "stages.sqlite3")
            graphiti = Graphiti.__new__(Graphiti)
            graphiti.clients = SimpleNamespace(llm_client=object())
            now = datetime(2026, 9, 26, tzinfo=timezone.utc)
            episode = EpisodicNode(
                name="episode", group_id="kg_hub", source=EpisodeType.text,
                content="body", source_description="source", valid_at=now)
            node = EntityNode(name="Person", group_id="kg_hub")
            edge = EntityEdge(name="KNOWS", fact="fact", group_id="kg_hub",
                              source_node_uuid=node.uuid, target_node_uuid=node.uuid,
                              created_at=now)
            episode_edge = EpisodicEdge(group_id="kg_hub",
                source_node_uuid=episode.uuid, target_node_uuid=node.uuid, created_at=now)
            calls = {"edge": 0, "attribute": 0, "commit": 0}

            async def edges(*_args):
                calls["edge"] += 1
                return [edge], [], [edge]

            async def attributes(*_args, **_kwargs):
                calls["attribute"] += 1
                return [node]

            async def commit(*_args):
                calls["commit"] += 1
                return [episode_edge], episode

            graphiti._extract_and_resolve_edges = edges
            graphiti._process_episode_data = commit
            identity = dict(store=store, task_sd="source", task_sid="sid",
                            operation_id="op", input_digest="input")
            with patch.object(ops, "extract_attributes_from_nodes", attributes):
                for _ in range(2):
                    resolved, invalidated, new = await extract_and_resolve_edges_with_snapshot(
                        graphiti, episode, [node], [], {}, "kg_hub", None,
                        [node], {node.uuid: node.uuid}, None, **identity)
                    self.assertEqual(resolved[0].uuid, edge.uuid)
                    self.assertEqual(invalidated, [])
                    hydrated = await extract_attributes_with_snapshot(
                        graphiti, [node], episode, [], None, new, **identity)
                    receipt_edges, receipt_episode = await commit_episode_with_receipt(
                        graphiti, episode, hydrated, resolved, now, "kg_hub",
                        None, None, {node.uuid: [0]}, **identity)
                    self.assertEqual(receipt_edges[0].uuid, episode_edge.uuid)
                    self.assertEqual(receipt_episode.uuid, episode.uuid)
            self.assertEqual(calls, {"edge": 1, "attribute": 1, "commit": 1})

    async def test_interrupted_stage_and_uncertain_commit_freeze(self):
        from graphiti_core import Graphiti
        from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType

        with tempfile.TemporaryDirectory() as temp:
            store = StageArtifactStore(Path(temp) / "stages.sqlite3")
            graphiti = Graphiti.__new__(Graphiti)
            now = datetime(2026, 9, 26, tzinfo=timezone.utc)
            episode = EpisodicNode(name="e", group_id="kg_hub",
                source=EpisodeType.text, content="body",
                source_description="source", valid_at=now)
            node = EntityNode(name="Person", group_id="kg_hub")
            calls = []

            async def failed_edges(*_args):
                calls.append("edge")
                raise TimeoutError("edge model result absent")

            async def uncertain_commit(*_args):
                calls.append("commit")
                raise TimeoutError("graph write outcome unknown")

            graphiti._extract_and_resolve_edges = failed_edges
            graphiti._process_episode_data = uncertain_commit
            identity = dict(store=store, task_sd="source", task_sid="sid",
                            operation_id="op", input_digest="input")
            async def edges():
                return await extract_and_resolve_edges_with_snapshot(
                    graphiti, episode, [node], [], {}, "kg_hub", None,
                    [node], {node.uuid: node.uuid}, None, **identity)
            with self.assertRaises(TimeoutError):
                await edges()
            with self.assertRaisesRegex(RuntimeError, "freeze"):
                await edges()
            async def commit():
                return await commit_episode_with_receipt(
                    graphiti, episode, [node], [], now, "kg_hub", None,
                    None, {node.uuid: [0]}, **identity)
            with self.assertRaises(TimeoutError):
                await commit()
            with self.assertRaisesRegex(RuntimeError, "freeze"):
                await commit()
            self.assertEqual(calls, ["edge", "commit"])

    async def test_real_extraction_restores_generated_node_uuid(self):
        from graphiti_core.nodes import EpisodicNode, EpisodeType
        from graphiti_core.prompts.extract_nodes import ExtractedEntity
        from graphiti_core.utils.maintenance import node_operations as ops

        with tempfile.TemporaryDirectory() as temp:
            store = StageArtifactStore(Path(temp) / "stages.sqlite3")
            episode = EpisodicNode(
                name="episode", group_id="kg_hub", source=EpisodeType.text,
                content="body", source_description="source",
                valid_at=datetime(2026, 9, 26, tzinfo=timezone.utc))
            calls = []

            async def model_answer(*_args):
                calls.append(1)
                return [ExtractedEntity(name="Person", entity_type_id=0)]

            async def run():
                return await extract_nodes_with_snapshot(
                    SimpleNamespace(llm_client=object()), episode, [], None,
                    None, None, store=store, task_sd="source", task_sid="sid",
                    operation_id="op", input_digest="input")

            with patch.object(ops, "_extract_nodes_single", model_answer):
                first_nodes, first_map = await run()
                second_nodes, second_map = await run()
            self.assertEqual(len(calls), 1)
            self.assertEqual(first_nodes[0].uuid, second_nodes[0].uuid)
            self.assertEqual(first_map, second_map)

    async def test_interrupted_resolution_reuses_candidates_and_node_uuids(self):
        from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType
        from graphiti_core.utils.maintenance import node_operations as ops

        with tempfile.TemporaryDirectory() as temp:
            store = StageArtifactStore(Path(temp) / "stages.sqlite3")
            episode = EpisodicNode(
                name="episode", group_id="kg_hub", source=EpisodeType.text,
                content="body", source_description="source",
                valid_at=datetime(2026, 9, 26, tzinfo=timezone.utc))
            extracted = EntityNode(name="Person", group_id="kg_hub")
            candidate_a = EntityNode(name="Different A", group_id="kg_hub")
            candidate_b = EntityNode(name="Different B", group_id="kg_hub")
            clients = SimpleNamespace(llm_client=object())
            reads = []
            first = True

            async def collect(*args):
                reads.append(1)
                return [[candidate_a if first else candidate_b]]

            async def resolve_llm(_client, nodes, indexes, state, *_args):
                if first:
                    raise TimeoutError("model result absent")
                selected = indexes.existing_nodes[0]
                state.resolved_nodes[0] = selected
                state.uuid_map[nodes[0].uuid] = selected.uuid

            async def run(nodes):
                return await resolve_nodes_with_candidate_snapshot(
                    clients, nodes, episode, [], None, store=store,
                    task_sd="source", task_sid="sid", operation_id="op",
                    input_digest="input")

            with patch.object(ops, "_collect_candidate_nodes", collect), \
                    patch.object(ops, "_resolve_with_llm", resolve_llm), \
                    patch.object(ops, "_resolve_with_similarity"):
                with self.assertRaises(TimeoutError):
                    await run([extracted])
                self.assertEqual(len(reads), 1)
                restored = load_extracted_nodes(
                    store, task_sd="source", task_sid="sid",
                    operation_id="op", input_digest="input")
                self.assertEqual(restored[0].uuid, extracted.uuid)
                first = False
                nodes, mapping, _ = await run(restored)
                self.assertEqual(nodes[0].uuid, candidate_a.uuid)
                self.assertEqual(mapping[extracted.uuid], candidate_a.uuid)
                self.assertEqual(len(reads), 1)
                replayed, _, _ = await run([extracted])
                self.assertEqual(replayed[0].uuid, candidate_a.uuid)
                self.assertEqual(len(reads), 1)
                with self.assertRaisesRegex(RuntimeError, "UUID/input drift"):
                    await run([EntityNode(name="Person", group_id="kg_hub")])

    async def test_stage_input_digest_is_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StageArtifactStore(Path(temp) / "stages.sqlite3")
            self.assertEqual(store.save_or_load("sd", "sid", "op", "hash", "stage", [1]), [1])
            self.assertEqual(store.save_or_load("sd", "sid", "op", "hash", "stage"), [1])
            with self.assertRaisesRegex(RuntimeError, "input drift"):
                store.save_or_load("sd", "sid", "op", "changed", "stage")
            store.begin_graph_commit("sd", "sid", "op", "hash")
            with self.assertRaisesRegex(RuntimeError, "freeze"):
                store.begin_graph_commit("sd", "sid", "op", "hash")


if __name__ == "__main__":
    unittest.main()
