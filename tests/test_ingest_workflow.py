"""Exercise the production stage adapter with durable journals and local I/O."""

import ast
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import model_gateway_client as client
from utils.ingest_workflow import workflow_context, verify_task_plan
from utils.model_attempt_journal import ModelAttemptJournal, NeedsReconciliation


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_split_resume_reuses_plan_and_parent_then_completes_remaining_children(self):
        tree = ast.parse(Path("kg_hub_server.py").read_text())
        funcs = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name in {"_predigest_extract", "_bare_episode_node"}]
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            body = SimpleNamespace(name="parent", episode_body="document",
                source_description="source", source_obs_id="one", provenance=None,
                model_usage_scenario=None)
            ref = datetime(2026, 9, 27, tzinfo=timezone.utc)
            parents, calls, statuses = [], [], []

            async def query(query, **params):
                if "MERGE (e:Episodic" in query:
                    parents.append(params["u"])
                    self.assertIn("ON CREATE SET", query)
                if "RETURN count(e) AS c" in query:
                    return [{"c": 0}], None, None
                return [], None, None

            async def child(_graph, name, *args, **kwargs):
                calls.append(name)
                if calls == ["parent--obs-01"]:
                    raise TimeoutError("outage")
                return SimpleNamespace(nodes=[], edges=[], episode=SimpleNamespace(uuid=name))

            async def update(*args, **kwargs):
                statuses.append(args[3])

            import uuid
            from utils.model_attempt_journal import NeedsReconciliation
            ns = dict(datetime=datetime, timezone=timezone, uuidlib=uuid, GROUP_ID="kg_hub",
                classify_provenance=lambda _: "firsthand", logger=Mock(),
                PREDIGEST_PROMPT="{max_obs} {body}", MAX_OBS=2,
                _llm_complete=AsyncMock(return_value="two observations"),
                parse_observations=lambda _: [{"type": "fact"}, {"type": "fact"}],
                _tag_schema_fields=AsyncMock(), _locked_add_episode=child,
                obs_to_episode_body=lambda *_: "fact", update_ingested_key_status=update,
                NeedsReconciliation=NeedsReconciliation)
            module = ast.Module(body=[ast.ImportFrom(module="__future__",
                names=[ast.alias(name="annotations")], level=0), *funcs], type_ignores=[])
            exec(compile(ast.fix_missing_locations(module), "kg_hub_server.py", "exec"), ns)
            graph = SimpleNamespace(driver=SimpleNamespace(execute_query=query))
            for run in range(2):
                with workflow_context(body, ref, "original-epoch", "split", lambda: journal):
                    await ns["_predigest_extract"](graph, body, ref, "split", ref, "original-epoch")
            self.assertEqual(statuses, ["needs_reconciliation", "ok"])
            self.assertEqual(calls, ["parent--obs-01", "parent--obs-01", "parent--obs-02"])
            self.assertEqual(len(set(parents)), 1)
            ns["_llm_complete"].assert_awaited_once()

    async def test_manual_resume_skips_saved_stage_and_reaches_business_commit(self):
        from anthropic.types import Message
        from graphiti_core.nodes import EpisodeType
        from graphiti_core.utils.maintenance import node_operations as ops

        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            paid = []

            async def provider(**kwargs):
                prompt = kwargs["messages"][0]["content"]
                paid.append(prompt)
                if prompt == "edge" and paid.count(prompt) == 1:
                    raise TimeoutError("model timed out")
                return Message.model_validate({
                    "id": "local", "type": "message", "role": "assistant",
                    "model": "kg_hub.entity_extract",
                    "content": [{"type": "text", "text": "result"}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1}})

            sdk = SimpleNamespace(messages=SimpleNamespace(create=provider))
            client.install_gateway_request_contract(sdk)

            async def model(prompt):
                return await sdk.messages.create(model="kg_hub.entity_extract",
                    messages=[{"role": "user", "content": prompt}])

            async def extract(*args, **kwargs):
                await model("extract")
                return [], {}

            async def edges(*args, **kwargs):
                await model("edge")
                return [], [], []

            async def attributes(*args, **kwargs):
                await model("attributes")
                return []

            committed = []

            async def commit(episode, *args):
                committed.append(episode)
                return [], episode

            async def query(query, **kwargs):
                if "MATCH (e:Episodic" in query and committed:
                    e = committed[0]
                    return [dict(uuid=e.uuid, group_id=e.group_id, name=e.name,
                                 source_description=e.source_description)], None, None
                return [], None, None

            driver = SimpleNamespace(_database="kg_hub", execute_query=query)
            graph = SimpleNamespace(driver=driver, clients=SimpleNamespace(driver=driver),
                retrieve_episodes=AsyncMock(return_value=[]),
                _extract_and_resolve_edges=edges, _process_episode_data=commit)
            tree = ast.parse(Path("kg_hub_server.py").read_text())
            fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == "_optional_checkpointed_add_episode")
            ns = dict(EpisodeType=EpisodeType, GROUP_ID="kg_hub", ENTITY_TYPES=None,
                      EDGE_TYPES=None, EDGE_TYPE_MAP=None, datetime=datetime,
                      _parallel_ingest_enabled=lambda: False)
            exec(compile(ast.Module(body=[fn], type_ignores=[]), "kg_hub_server.py", "exec"), ns)
            body = SimpleNamespace(name="episode", episode_body="content",
                                   source_description="source", source_obs_id="one")
            ref = datetime(2026, 9, 27, tzinfo=timezone.utc)

            async def run():
                with workflow_context(body, ref, "epoch", "episode", lambda: journal):
                    return await ns[fn.name](graph, body.name, body.episode_body,
                                            "source", ref, "operation", "source", "one")

            with patch.dict("os.environ", {"KG_HUB_MODEL_GATEWAY_TOKEN": "test-token"}), \
                    patch.object(client, "journal_from_backup_env", return_value=journal), \
                    patch.object(client, "query_gateway_attempt_status", return_value={
                        "version": 1, "phase": "failed", "provider_call_started": True}), \
                    patch.object(ops, "extract_nodes", new=extract), \
                    patch.object(ops, "extract_attributes_from_nodes", new=attributes), \
                    client.model_business_task("source", "one"), \
                    client.model_operation("ingest.episode", "operation"):
                journal.begin_task_execution("source", "one", "original")
                with self.assertRaises(NeedsReconciliation):
                    await run()
                journal.finish_task_execution("source", "one", "original", state="failed")
                row = dict(source_description="source", source_obs_id="one")
                self.assertFalse(await verify_task_plan(driver, journal, row,
                                                       observation_body=lambda *_: ""))
                failed = next(r for r in journal.find_task("source", "one")
                              if r["stage"] == "edge_phase")
                grant = journal.authorize_retry("source", "one", failed["step_id"],
                                                failed["request_digest"], deadline_seconds=180,
                                                expected_stage="edge_phase")
                journal.begin_task_execution("source", "one", "manual", manual_command_id="manual")
                with client.model_manual_resume("source", "one", failed["step_id"], grant,
                                                stage="edge_phase", execution_id="manual"):
                    result = await run()
                self.assertTrue(await verify_task_plan(driver, journal, row,
                                                      observation_body=lambda *_: ""))
                self.assertEqual(row["episode_uuid"], result.episode.uuid)
                self.assertEqual(paid, ["extract", "edge", "edge", "attributes"])
                self.assertEqual(len(committed), 1)
                graph.retrieve_episodes.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
