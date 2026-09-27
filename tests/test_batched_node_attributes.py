"""Exercise the Graphiti entry point, including its unchanged summary tail."""
import json
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, ValidationError
import graphiti_client  # installs the production adapter
import graphiti_core.graphiti as pipeline
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.maintenance import node_operations as upstream


class FileAttrs(BaseModel):
    path: str | None = None


class ToolAttrs(BaseModel):
    version: str | None = None


class BatchAttributesTests(unittest.IsolatedAsyncioTestCase):
    def nodes(self, count):
        return [EntityNode(name="same-name", group_id="kg_hub",
                           labels=["Entity", "File"], attributes={"old": "kept"})
                for _ in range(count)]

    async def run_pipeline(self, nodes, generate, **kwargs):
        summaries, embeddings = AsyncMock(), AsyncMock()
        clients = SimpleNamespace(llm_client=SimpleNamespace(generate_response=generate),
                                  embedder=object())
        with patch.object(upstream, "_extract_entity_summaries_batch", summaries), \
             patch.object(upstream, "create_entity_node_embeddings", embeddings):
            result = await pipeline.extract_attributes_from_nodes(
                clients, nodes, entity_types={"File": FileAttrs, "Tool": ToolAttrs},
                **kwargs,
            )
        self.assertIs(result, nodes)
        summaries.assert_awaited_once()
        embeddings.assert_awaited_once_with(clients.embedder, nodes)
        return summaries

    async def test_seventeen_entities_use_three_calls_and_keep_summaries(self):
        nodes = self.nodes(17)
        async def generate(messages, response_model, **kwargs):
            if response_model is FileAttrs:  # upstream baseline
                return {"path": "/baseline"}
            data = json.loads(messages[1].content)
            return {key: {"path": f"/{key}"} for key in data["entities"]}
        generate = AsyncMock(side_effect=generate)
        summaries = await self.run_pipeline(nodes, generate, skip_fact_appending=True)
        self.assertEqual(generate.await_count, 3)
        self.assertEqual([len(c.kwargs["response_model"].model_fields)
                          for c in generate.await_args_list], [8, 8, 1])
        self.assertTrue(summaries.call_args.kwargs["skip_fact_appending"])
        self.assertEqual(nodes[0].attributes, {"old": "kept", "path": "/entity_0"})
        self.assertEqual(nodes[1].attributes["path"], "/entity_1")

    async def test_checkpointed_original_and_recovery_keep_batching(self):
        from utils.graphiti_stage_adapter import extract_attributes_with_snapshot
        from utils.graphiti_stage_adapter import StageArtifactStore
        nodes = self.nodes(9)
        async def generate(messages, **kwargs):
            return {key: {"path": "/saved"}
                    for key in json.loads(messages[1].content)["entities"]}
        generate = AsyncMock(side_effect=generate)
        clients = SimpleNamespace(llm_client=SimpleNamespace(generate_response=generate),
                                  embedder=object())
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(upstream, "_extract_entity_summaries_batch", AsyncMock()), \
                patch.object(upstream, "create_entity_node_embeddings", AsyncMock()):
            store = StageArtifactStore(Path(temp) / "stages.sqlite3")
            inputs = [node.model_copy(deep=True) for node in nodes]
            kwargs = dict(store=store, task_sd="source", task_sid="task",
                          operation_id="operation", input_digest="input")
            first = await extract_attributes_with_snapshot(
                SimpleNamespace(clients=clients), nodes, None, [], {"File": FileAttrs}, [], **kwargs)
            second = await extract_attributes_with_snapshot(
                SimpleNamespace(clients=clients), inputs, None, [], {"File": FileAttrs}, [], **kwargs)
            self.assertEqual(generate.await_count, 2)
            self.assertEqual([node.attributes for node in first], [node.attributes for node in second])

    async def test_mixed_types_and_duplicate_names_map_by_required_keys(self):
        nodes = self.nodes(2)
        nodes[1].labels = ["Entity", "Tool"]
        generate = AsyncMock(return_value={"entity_0": {"path": "/a"},
                                           "entity_1": {"version": "1.0"}})
        await self.run_pipeline(nodes, generate)
        self.assertEqual(nodes[0].attributes["path"], "/a")
        self.assertEqual(nodes[1].attributes["version"], "1.0")
        self.assertNotIn("path", nodes[1].attributes)

    async def test_missing_entity_fails_without_mutation_or_paid_fallback(self):
        nodes = self.nodes(2)
        generate = AsyncMock(return_value={"entity_0": {"path": "/a"}})
        with self.assertRaises(ValidationError):
            await self.run_pipeline(nodes, generate)
        self.assertEqual(generate.await_count, 1)
        self.assertEqual([n.attributes for n in nodes], [{"old": "kept"}] * 2)

    async def test_untyped_nodes_need_no_attribute_call(self):
        nodes = self.nodes(1)
        nodes[0].labels = ["Entity"]
        generate = AsyncMock()
        await self.run_pipeline(nodes, generate)
        generate.assert_not_awaited()

    async def test_later_batch_failure_leaves_all_nodes_unchanged(self):
        nodes = self.nodes(9)
        generate = AsyncMock(side_effect=[
            {f"entity_{i}": {"path": "/a"} for i in range(8)}, {},
        ])
        with self.assertRaises(ValidationError):
            await self.run_pipeline(nodes, generate)
        self.assertEqual(generate.await_count, 2)
        self.assertTrue(all(n.attributes == {"old": "kept"} for n in nodes))


if __name__ == "__main__":
    unittest.main()
