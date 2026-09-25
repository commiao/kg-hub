"""Pinned Graphiti orchestration replays paid steps before one human retry.

Run with graphiti-core==0.29.0 and anthropic==0.102.0 installed. The model and
graph I/O functions are replaced locally; this test never reaches a provider.
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from utils.graphiti_episode_checkpoint import add_episode_with_context_checkpoint
from utils.model_attempt_journal import ModelAttemptJournal, NeedsReconciliation
import model_gateway_client as client_module


HAS_GRAPHITI = importlib.util.find_spec("graphiti_core") is not None


class Span:
    def add_attributes(self, values):
        pass

    def set_status(self, *args):
        pass

    def record_exception(self, *args):
        pass


class Tracer:
    @contextmanager
    def start_span(self, name):
        yield Span()


@unittest.skipUnless(HAS_GRAPHITI, "requires isolated pinned Graphiti environment")
class PinnedGraphitiResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_cached_step_replays_and_only_failed_step_retries(self):
        module = importlib.import_module("graphiti_core.graphiti")
        from anthropic.types import Message
        from graphiti_core import Graphiti
        from graphiti_core.nodes import EpisodeType

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ingest.jsonl"
            journal = ModelAttemptJournal(path.with_name("model-attempts.sqlite3"))
            paid_prompts = []
            changed_first_prompt = False

            async def paid_call(*args, **kwargs):
                prompt = kwargs["messages"][0]["content"]
                paid_prompts.append(prompt)
                if prompt == "resolve-step" and paid_prompts.count(prompt) == 1:
                    raise TimeoutError("no model response")
                return Message.model_validate({
                    "id": "msg_local", "type": "message", "role": "assistant",
                    "model": "kg_hub.entity_extract",
                    "content": [{"type": "text", "text": "saved"}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                })

            sdk = SimpleNamespace(messages=SimpleNamespace(create=paid_call))
            client_module.install_gateway_request_contract(sdk)

            g = Graphiti.__new__(Graphiti)
            g.driver = SimpleNamespace(_database="kg_hub", provider="falkordb")
            g.clients = SimpleNamespace(driver=g.driver)
            g.tracer = Tracer()
            g.max_coroutines = 1
            previous_reads = []

            async def retrieve_previous(*args, **kwargs):
                previous_reads.append(1)
                return [SimpleNamespace(uuid="previous-episode")]

            g.retrieve_episodes = retrieve_previous

            async def pinned_previous(driver, uuids):
                self.assertEqual(uuids, ["previous-episode"])
                return []

            async def extract_nodes(*args, **kwargs):
                await sdk.messages.create(
                    model="kg_hub.entity_extract",
                    messages=[{"role": "user", "content":
                               "changed-extract" if changed_first_prompt else "extract-step"}])
                return [], {}

            async def resolve_nodes(*args, **kwargs):
                await sdk.messages.create(
                    model="kg_hub.entity_extract",
                    messages=[{"role": "user", "content": "resolve-step"}])
                return [], {}, []

            async def resolve_edges(*args, **kwargs):
                await sdk.messages.create(
                    model="kg_hub.entity_extract",
                    messages=[{"role": "user", "content": "new-downstream-step"}])
                return [], [], []

            async def attributes(*args, **kwargs):
                return []

            async def save(episode, *args, **kwargs):
                return [], episode

            g._extract_and_resolve_edges = resolve_edges
            g._process_episode_data = save
            kwargs = dict(name="test", episode_body="body", source_description="source",
                          reference_time=datetime(2026, 9, 26, tzinfo=timezone.utc),
                          source=EpisodeType.text, group_id="kg_hub")

            async def run():
                return await add_episode_with_context_checkpoint(
                    g, journal, task_sd="source", task_sid="id-1",
                    operation_id="operation", relevant_schema_limit=3, **kwargs)

            env = {"KG_HUB_INGEST_BACKUP_PATH": str(path),
                   "KG_HUB_MODEL_GATEWAY_TOKEN": "test-token"}
            with patch.dict("os.environ", env), patch.object(
                    client_module.breakers, "assert_closed"), patch.object(
                    client_module, "query_gateway_attempt_status",
                    return_value={"version": 1, "phase": "failed",
                                  "provider_call_started": True}), patch.object(
                    module.EpisodicNode, "get_by_uuids", new=pinned_previous), patch.object(
                    module, "extract_nodes", new=extract_nodes), patch.object(
                    module, "resolve_extracted_nodes", new=resolve_nodes), patch.object(
                    module, "extract_attributes_from_nodes", new=attributes):
                with client_module.model_business_task("source", "id-1"), \
                        client_module.model_operation("ingest.episode", "operation"):
                    with self.assertRaises(NeedsReconciliation):
                        await run()
                attempts = journal.find_task("source", "id-1")
                failed = next(row for row in attempts if row["phase"] == "failed")
                grant = journal.authorize_retry(
                    "source", "id-1", failed["step_id"], failed["request_digest"],
                    deadline_seconds=180)
                changed_first_prompt = True
                with client_module.model_business_task("source", "id-1"), \
                        client_module.model_operation("ingest.episode", "operation"), \
                        client_module.model_manual_resume("source", "id-1",
                                                          failed["step_id"], grant):
                    with self.assertRaises(NeedsReconciliation):
                        await run()
                self.assertEqual(paid_prompts, ["extract-step", "resolve-step"])
                changed_first_prompt = False
                with client_module.model_business_task("source", "id-1"), \
                        client_module.model_operation("ingest.episode", "operation"), \
                        client_module.model_manual_resume("source", "id-1",
                                                          failed["step_id"], grant):
                    result = await run()
                self.assertEqual(result.episode.name, "test")
                self.assertEqual(paid_prompts,
                                 ["extract-step", "resolve-step", "resolve-step",
                                  "new-downstream-step"])
                self.assertEqual(len(previous_reads), 1)


if __name__ == "__main__":
    unittest.main()
