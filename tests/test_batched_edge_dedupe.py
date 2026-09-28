import asyncio
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EpisodeType, EpisodicNode
from graphiti_core.prompts import prompt_library
from graphiti_core.utils.maintenance import edge_operations as ops

from utils.batched_edge_dedupe import (
    BATCH_PROMPT, RESOLVE_PROMPT, _DedupeBatch, _task_digest,
)
from utils.graphiti_stage_names import graphiti_model_stage

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


def resolve_messages(new_fact, existing=(), candidates=()):
    return prompt_library.dedupe_edges.resolve_edge({
        "existing_edges": [{"idx": i, "fact": f} for i, f in enumerate(existing)],
        "edge_invalidation_candidates": [
            {"idx": len(existing) + i, "fact": f} for i, f in enumerate(candidates)],
        "new_edge": new_fact,
    })


class FakeClient:
    """Answers batch requests by looking each TASK's new fact up in ``answers``."""

    def __init__(self, answers=None, response=None):
        self.answers = answers or {}
        self.response = response
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def generate_response(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if self.response is not None:
                return self.response
            name = kwargs.get("prompt_name")
            if name == BATCH_PROMPT:
                body = messages[-1].content
                tasks = body.split('<TASK index="')[1:]
                return {"results": [
                    {"index": i, **self.answers[t.split("<NEW FACT>")[1].split("</NEW FACT>")[0].strip()]}
                    for i, t in enumerate(tasks)]}
            if name == RESOLVE_PROMPT:
                fact = messages[-1].content.split("<NEW FACT>")[1].split("</NEW FACT>")[0].strip()
                return self.answers[fact]
            if name == "extract_edges.extract_timestamps":
                return {"valid_at": None, "invalid_at": None}
            return {}
        finally:
            self.active -= 1


def run(coro):
    return asyncio.run(coro)


class DedupeBatchTests(unittest.TestCase):
    def test_multiple_edges_use_one_model_call_and_map_back_by_task(self):
        async def go():
            answers = {
                "A likes tea": {"duplicate_facts": [0], "contradicted_facts": []},
                "B owns a car": {"duplicate_facts": [], "contradicted_facts": [1]},
                "C left Acme": {"duplicate_facts": [], "contradicted_facts": []},
            }
            client = FakeClient(answers)
            batch = _DedupeBatch(client, limit=2, max_items=12, delay=0)
            msgs = {f: resolve_messages(f, ["x", "y"]) for f in answers}
            results = await asyncio.gather(*(
                batch.proxy.generate_response(m, prompt_name=RESOLVE_PROMPT)
                for m in msgs.values()))
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(client.calls[0][1]["prompt_name"], BATCH_PROMPT)
            self.assertEqual(results, list(answers.values()))
            self.assertEqual((batch.batch_requests, batch.batched_edges), (1, 3))
        run(go())

    def test_request_bytes_do_not_depend_on_arrival_order(self):
        async def go(order):
            answers = {f: {"duplicate_facts": [], "contradicted_facts": []}
                       for f in ("f1", "f2", "f3")}
            client = FakeClient(answers)
            batch = _DedupeBatch(client, limit=2, max_items=12, delay=0)
            await asyncio.gather(*(batch.proxy.generate_response(
                resolve_messages(f, ["x"]), prompt_name=RESOLVE_PROMPT) for f in order))
            return client.calls[0][0][-1].content
        self.assertEqual(run(go(["f1", "f2", "f3"])), run(go(["f3", "f1", "f2"])))

    def test_episode_boundary_stabilizes_batches_across_arrival_windows(self):
        async def go(order):
            facts = ("f1", "f2", "f3")
            answers = {f: {"duplicate_facts": [], "contradicted_facts": []}
                       for f in facts}
            client = FakeClient(answers)
            batch = _DedupeBatch(client, limit=2, max_items=12, delay=0)
            batch.set_expected_edges(facts)

            async def submit(fact, delay):
                await asyncio.sleep(delay)
                return await batch.resolve(
                    resolve_messages(fact, ["x"]), {"prompt_name": RESOLVE_PROMPT},
                    edge_id=fact)

            results = await asyncio.gather(*(submit(fact, delay) for fact, delay in order))
            self.assertEqual(results, [answers[fact] for fact, _ in order])
            self.assertEqual(len(client.calls), 1)
            return client.calls[0][0][-1].content

        first = run(go([("f1", 0), ("f2", 0.02), ("f3", 0.04)]))
        replay = run(go([("f3", 0), ("f1", 0.02), ("f2", 0.04)]))
        self.assertEqual(first, replay)

    def test_episode_boundary_waits_for_resolvers_without_dedupe_prompt(self):
        async def go():
            answers = {f: {"duplicate_facts": [], "contradicted_facts": []}
                       for f in ("f1", "f2")}
            client = FakeClient(answers)
            batch = _DedupeBatch(client, limit=2, max_items=12, delay=0)
            batch.set_expected_edges({"f1", "f2"})
            async def resolve_requests():
                return await asyncio.gather(*(batch.resolve(
                    resolve_messages(f, ["x"]), {"prompt_name": RESOLVE_PROMPT},
                    edge_id=f) for f in answers))

            # The fast-path/no-candidate edge is outside the set of model calls;
            # once the two eligible requests arrive, they form a stable batch.
            pending = asyncio.create_task(resolve_requests())
            await asyncio.sleep(0)
            self.assertFalse(client.calls)
            batch.edge_finished("no-candidates")
            first, second = await pending
            self.assertEqual((first, second), (answers["f1"], answers["f2"]))
            self.assertEqual(len(client.calls), 1)
        run(go())

    def test_single_edge_uses_upstream_prompt_unchanged(self):
        async def go():
            answer = {"duplicate_facts": [], "contradicted_facts": []}
            client = FakeClient({"only": answer})
            batch = _DedupeBatch(client, limit=2, max_items=12, delay=0)
            messages = resolve_messages("only", ["x"])
            self.assertEqual(await batch.proxy.generate_response(
                messages, prompt_name=RESOLVE_PROMPT), answer)
            self.assertIs(client.calls[0][0], messages)
            self.assertEqual(client.calls[0][1]["prompt_name"], RESOLVE_PROMPT)
        run(go())

    def test_swapped_identity_fails_every_edge_without_individual_retry(self):
        async def go():
            client = FakeClient(response={"results": [
                {"index": 1, "duplicate_facts": [], "contradicted_facts": []},
                {"index": 0, "duplicate_facts": [], "contradicted_facts": []}]})
            batch = _DedupeBatch(client, limit=2, max_items=12, delay=0)
            results = await asyncio.gather(*(batch.proxy.generate_response(
                resolve_messages(f, ["x"]), prompt_name=RESOLVE_PROMPT)
                for f in ("a", "b")), return_exceptions=True)
            self.assertTrue(all(isinstance(r, RuntimeError) for r in results))
            self.assertEqual(len(client.calls), 1)
        run(go())

    def test_failed_chunk_stops_paying_for_later_chunks(self):
        async def go():
            client = FakeClient(response={"results": []})
            batch = _DedupeBatch(client, limit=2, max_items=2, delay=0)
            results = await asyncio.gather(*(batch.proxy.generate_response(
                resolve_messages(f, ["x"]), prompt_name=RESOLVE_PROMPT)
                for f in ("a", "b", "c", "d", "e")), return_exceptions=True)
            self.assertTrue(all(isinstance(r, RuntimeError) for r in results))
            self.assertEqual(len(client.calls), 1)
        run(go())

    def test_chunks_respect_max_items(self):
        async def go():
            answers = {f: {"duplicate_facts": [], "contradicted_facts": []}
                       for f in ("a", "b", "c", "d", "e")}
            client = FakeClient(answers)
            batch = _DedupeBatch(client, limit=2, max_items=2, delay=0)
            results = await asyncio.gather(*(batch.proxy.generate_response(
                resolve_messages(f, ["x"]), prompt_name=RESOLVE_PROMPT) for f in answers))
            self.assertEqual(results, list(answers.values()))
            self.assertEqual([c[1]["prompt_name"] for c in client.calls],
                             [BATCH_PROMPT, BATCH_PROMPT, RESOLVE_PROMPT])
        run(go())

    def test_other_prompts_keep_the_per_episode_call_limit(self):
        async def go():
            client = FakeClient()
            batch = _DedupeBatch(client, limit=2, max_items=12, delay=0)
            await asyncio.gather(*(batch.proxy.generate_response(
                resolve_messages(str(i)), prompt_name="extract_edges.extract_timestamps")
                for i in range(8)))
            self.assertEqual(len(client.calls), 8)
            self.assertLessEqual(client.max_active, 2)
        run(go())

    def test_upstream_resolver_consumes_batched_answers(self):
        """Real Graphiti resolver: duplicate reuse and contradiction still upstream-owned."""
        async def go():
            episode = EpisodicNode(name="e", group_id="g", source=EpisodeType.text,
                                   source_description="t", content="c",
                                   valid_at=NOW, created_at=NOW)

            def edge(fact, src="s", dst="d", valid_at=None):
                return EntityEdge(group_id="g", source_node_uuid=src, target_node_uuid=dst,
                                  name="REL", fact=fact, created_at=NOW, valid_at=valid_at)

            old_same = edge("Alice works at Acme")
            old_title = edge("Bob is a junior engineer", valid_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
            new_dup = edge("Alice is employed by Acme")
            new_update = edge("Bob is a senior engineer", valid_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            client = FakeClient({
                "Alice is employed by Acme": {"duplicate_facts": [0], "contradicted_facts": []},
                "Bob is a senior engineer": {"duplicate_facts": [], "contradicted_facts": [0]},
            })
            batch = _DedupeBatch(client, limit=2, max_items=12, delay=0)
            (dup_res, dup_inv, _), (upd_res, upd_inv, _) = await asyncio.gather(
                ops.resolve_extracted_edge(batch.proxy, new_dup, [old_same], [], episode),
                ops.resolve_extracted_edge(batch.proxy, new_update, [old_title], [], episode),
            )
            self.assertEqual([c[1]["prompt_name"] for c in client.calls], [BATCH_PROMPT])
            self.assertIs(dup_res, old_same)
            self.assertIn(episode.uuid, old_same.episodes)
            self.assertIs(upd_res, new_update)
            self.assertEqual(upd_inv, [old_title])
            self.assertEqual(old_title.invalid_at, new_update.valid_at)
        run(go())

    def test_batch_prompt_is_part_of_replayable_edge_phase(self):
        self.assertEqual(graphiti_model_stage(BATCH_PROMPT), "edge_phase")

    def test_task_digest_distinguishes_content(self):
        self.assertNotEqual(_task_digest(resolve_messages("a")), _task_digest(resolve_messages("b")))


INSTALL_PROBE = r"""
import asyncio, json
from types import SimpleNamespace
import graphiti_core.graphiti as pipeline
import graphiti_core.utils.maintenance.edge_operations as ops
from utils import batched_edge_dedupe as mod

async def fake_resolve_edge(llm_client, edge, *a, **k):
    state["active"] += 1; state["max"] = max(state["max"], state["active"])
    await asyncio.sleep(0.01)
    state["active"] -= 1
    state["clients"].add(id(llm_client))
    return edge, [], []

async def fake_resolve(clients, extracted_edges, episode, *a, **k):
    await ops.semaphore_gather(*[ops.resolve_extracted_edge(clients.llm_client, e) for e in extracted_edges])
    await ops.semaphore_gather(*[asyncio.sleep(0.01) for _ in range(6)])
    return [], [], []

state = {"active": 0, "max": 0, "clients": set()}
ops.resolve_extracted_edge = fake_resolve_edge
ops.resolve_extracted_edges = fake_resolve
mod.install(100)
mod.install(100)
clients = SimpleNamespace(llm_client=object())
asyncio.run(ops.resolve_extracted_edges(clients, list(range(6)), SimpleNamespace(uuid="ep")))
out = {"max": state["max"], "one_proxy": len(state["clients"]) == 1,
       "pipeline": pipeline.resolve_extracted_edges is ops.resolve_extracted_edges}
state["max"] = 0
asyncio.run(ops.semaphore_gather(*[ops.resolve_extracted_edge(None, i) for i in range(6)]))
out["max_outside_batch"] = state["max"]
print(json.dumps(out))
"""


COMBINED_PROBE = r"""
import asyncio, json
from datetime import datetime, timezone
from types import SimpleNamespace
import graphiti_core.utils.maintenance.edge_operations as ops
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EpisodeType, EpisodicNode
from utils import batched_edge_dedupe, batched_edge_timestamps

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
calls = []

class Client:
    async def generate_response(self, messages, **kwargs):
        name = kwargs["prompt_name"]; calls.append(name)
        await asyncio.sleep(0.005)
        n = messages[-1].content.count('<TASK index="')
        if name == "dedupe_edges.resolve_edge_batch":
            return {"results": [{"index": i, "duplicate_facts": [], "contradicted_facts": []} for i in range(n)]}
        if name == "extract_edges.extract_timestamps_batch":
            return {"timestamps": [{"index": i, "valid_at": "2026-09-0%dT00:00:00Z" % (i + 1)} for i in range(6)]}
        raise AssertionError(name)

async def fake_resolve(clients, extracted_edges, episode, *a, **k):
    old = EntityEdge(group_id="g", source_node_uuid="s", target_node_uuid="d", name="R", fact="old", created_at=NOW)
    return await ops.semaphore_gather(*[
        ops.resolve_extracted_edge(clients.llm_client, e,
                                   [old] if e.fact != "fact 5" else [], [], episode)
        for e in extracted_edges
    ])

ops.resolve_extracted_edges = fake_resolve
batched_edge_timestamps.install(100)
batched_edge_dedupe.install(100)
episode = EpisodicNode(name="e", group_id="g", source=EpisodeType.text, source_description="t",
                       content="c", valid_at=NOW, created_at=NOW)
edges = [EntityEdge(group_id="g", source_node_uuid="s", target_node_uuid="d", name="R",
                    fact="fact %d" % i, created_at=NOW) for i in range(6)]
asyncio.run(ops.resolve_extracted_edges(SimpleNamespace(llm_client=Client()), edges, episode))
print(json.dumps({"calls": calls, "all_dated": all(e.valid_at is not None for e in edges)}))
"""


class InstallTests(unittest.TestCase):
    def test_with_timestamp_batch_an_episode_needs_one_call_per_kind(self):
        proc = subprocess.run(
            [sys.executable, "-c", COMBINED_PROBE], cwd=ROOT, capture_output=True,
            text=True, env={"SEMAPHORE_LIMIT": "2", "PATH": ""}, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["calls"], ["dedupe_edges.resolve_edge_batch",
                                        "extract_edges.extract_timestamps_batch"])
        self.assertTrue(out["all_dated"])

    def test_install_widens_only_the_per_edge_gather_inside_a_batch(self):
        proc = subprocess.run(
            [sys.executable, "-c", INSTALL_PROBE], cwd=ROOT, capture_output=True,
            text=True, env={"SEMAPHORE_LIMIT": "2", "PATH": ""}, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["max"], 6)
        self.assertTrue(out["one_proxy"])
        self.assertTrue(out["pipeline"])
        self.assertEqual(out["max_outside_batch"], 2)

    def test_zero_percent_installs_nothing(self):
        probe = ("import graphiti_core.utils.maintenance.edge_operations as ops\n"
                 "before = ops.resolve_extracted_edges\n"
                 "from utils import batched_edge_dedupe as mod\n"
                 "mod.install(0)\n"
                 "print(ops.resolve_extracted_edges is before)\n")
        proc = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "True", proc.stderr)


if __name__ == "__main__":
    unittest.main()
