"""T-0285: Graphiti contradiction verdicts are logged, never applied; valid_at is bounded.

Every case runs the real Graphiti 0.29.0 resolver in a subprocess, because the
patches rebind module globals that the batch patches capture at install time.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMMON = r"""
import asyncio, json, logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import graphiti_core.graphiti as pipeline
import graphiti_core.utils.maintenance.edge_operations as ops
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EpisodeType, EpisodicNode

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
logs = []
class Grab(logging.Handler):
    def emit(self, record): logs.append(record.getMessage())
logging.getLogger("kg_hub.edge_invalidation").addHandler(Grab())
logging.getLogger("kg_hub.edge_invalidation").setLevel(logging.INFO)

def day(n): return NOW + timedelta(days=n)
def edge(fact, valid_at=None, src="s", dst="d"):
    return EntityEdge(group_id="g", source_node_uuid=src, target_node_uuid=dst, name="R",
                      fact=fact, created_at=NOW, valid_at=valid_at)
episode = EpisodicNode(name="e", group_id="g", source=EpisodeType.text, source_description="t",
                       content="c", valid_at=NOW, created_at=NOW)

async def fake_resolve(clients, extracted, episode, *a, **k):
    # Mirrors the upstream fan-out: every edge through the module-level resolver.
    return await ops.semaphore_gather(*[
        ops.resolve_extracted_edge(clients.llm_client, e, *CANDIDATES[e.fact], episode)
        for e in extracted])
ops.resolve_extracted_edges = fake_resolve
"""

# One new fact contradicting an older related edge, an older broad candidate and
# a later-dated candidate: upstream expires the two older edges and the new one.
SINGLE = COMMON + r"""
from utils import edge_invalidation_shadow as mod
mod.install(MODE)

old_related = edge("Bob is a junior engineer", valid_at=day(-300))
old_broad = edge("Bob works on payments", valid_at=day(-200), src="x", dst="y")
later = edge("Bob left the company", valid_at=day(30), src="x", dst="z")
new = edge("Bob is a senior engineer")
CANDIDATES = {new.fact: ([old_related], [old_broad, later])}

class Client:
    async def generate_response(self, messages, **kwargs):
        name = kwargs["prompt_name"]
        if name == "dedupe_edges.resolve_edge":
            return {"duplicate_facts": [], "contradicted_facts": [0, 1, 2]}
        if name == "extract_edges.extract_timestamps":
            return {"valid_at": day(-1).isoformat(), "invalid_at": None}
        raise AssertionError(name)

[(resolved, invalidated, _)] = asyncio.run(
    ops.resolve_extracted_edges(SimpleNamespace(llm_client=Client()), [new], episode))
print(json.dumps({
    "invalidated": [e.uuid for e in invalidated],
    "expired": [e.fact for e in (old_related, old_broad, later, new) if e.expired_at],
    "new_valid_at": new.valid_at.isoformat(),
    "logged": [m for m in logs if "edge_invalidation_shadow" in m],
    "uuids": [old_related.uuid, old_broad.uuid, later.uuid],
}))
"""

# Shadow installed first, then both batch patches exactly as graphiti_client does.
COMBINED = COMMON + r"""
from utils import batched_edge_dedupe, batched_edge_timestamps, edge_invalidation_shadow
edge_invalidation_shadow.install("shadow")
batched_edge_timestamps.install(100)
batched_edge_dedupe.install(100)

olds = [edge("old %d" % i, valid_at=day(-10)) for i in range(5)]
news = [edge("fact %d" % i) for i in range(6)]
CANDIDATES = {n.fact: (([olds[i]], []) if i < 5 else ([], [])) for i, n in enumerate(news)}
calls = []

class Client:
    async def generate_response(self, messages, **kwargs):
        name = kwargs["prompt_name"]; calls.append(name)
        await asyncio.sleep(0.005)
        n = messages[-1].content.count('<TASK index="')
        if name == "dedupe_edges.resolve_edge_batch":
            return {"results": [{"index": i, "duplicate_facts": [], "contradicted_facts": [0]}
                                for i in range(n)]}
        if name == "extract_edges.extract_timestamps_batch":
            return {"timestamps": [{"index": i, "valid_at": day(-1 - i).isoformat()}
                                   for i in range(6)]}
        raise AssertionError(name)

results = asyncio.run(ops.resolve_extracted_edges(
    SimpleNamespace(llm_client=Client()), news, episode))
print(json.dumps({
    "calls": calls,
    "invalidated": sum(len(r[1]) for r in results),
    "expired": sum(1 for e in olds + news if e.expired_at),
    "logged": len([m for m in logs if "edge_invalidation_shadow" in m]),
    "all_dated": all(e.valid_at is not None for e in news),
    "pipeline": pipeline.resolve_extracted_edges is ops.resolve_extracted_edges,
}))
"""

# Extraction and the timestamp prompt both date facts after their episode.
CLAMP = COMMON + r"""
from utils import edge_invalidation_shadow as mod
mod.install("shadow")

from_extraction = edge("dated by extraction", valid_at=day(30))
from_prompt = edge("dated by timestamp prompt")
already_ok = edge("dated before the episode", valid_at=day(-3))
existing_later = edge("existing later fact", valid_at=day(40))
duplicate = edge("duplicate of existing later fact")
CANDIDATES = {
    from_extraction.fact: ([], []), from_prompt.fact: ([], []), already_ok.fact: ([], []),
    duplicate.fact: ([existing_later], []),
}

class Client:
    async def generate_response(self, messages, **kwargs):
        name = kwargs["prompt_name"]
        if name == "extract_edges.extract_timestamps":
            return {"valid_at": day(5).isoformat(), "invalid_at": None}
        if name == "dedupe_edges.resolve_edge":
            return {"duplicate_facts": [0], "contradicted_facts": []}
        raise AssertionError(name)

results = asyncio.run(ops.resolve_extracted_edges(SimpleNamespace(llm_client=Client()),
    [from_extraction, from_prompt, already_ok, duplicate], episode))
print(json.dumps({
    "extraction": from_extraction.valid_at.isoformat(),
    "prompt": from_prompt.valid_at.isoformat(),
    "ok": already_ok.valid_at.isoformat(),
    "duplicate_resolved_to_existing": results[3][0] is existing_later,
    "existing": existing_later.valid_at.isoformat(),
    "clamp_logs": len([m for m in logs if "edge_valid_at_clamp" in m]),
}))
"""


def run_probe(source, **env):
    proc = subprocess.run(
        [sys.executable, "-c", source], cwd=ROOT, capture_output=True, text=True,
        env={"SEMAPHORE_LIMIT": "2", "PATH": "", **env}, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


class ShadowTests(unittest.TestCase):
    def test_upstream_mode_is_the_control_and_expires_all_three(self):
        out = run_probe(SINGLE.replace("MODE", '"upstream"'))
        self.assertEqual(len(out["invalidated"]), 2)
        self.assertEqual(sorted(out["expired"]), sorted([
            "Bob is a junior engineer", "Bob works on payments", "Bob is a senior engineer"]))

    def test_shadow_mode_expires_nothing_and_logs_every_verdict(self):
        out = run_probe(SINGLE.replace("MODE", '"shadow"'))
        self.assertEqual(out["invalidated"], [])
        self.assertEqual(out["expired"], [])
        self.assertEqual(len(out["logged"]), 1)
        for uuid in out["uuids"]:
            self.assertIn(uuid, out["logged"][0])

    def test_shadow_keeps_both_batches_to_one_call_each(self):
        out = run_probe(COMBINED)
        self.assertEqual(out["calls"], ["dedupe_edges.resolve_edge_batch",
                                        "extract_edges.extract_timestamps_batch"])
        self.assertEqual(out["invalidated"], 0)
        self.assertEqual(out["expired"], 0)
        self.assertEqual(out["logged"], 5)
        self.assertTrue(out["all_dated"])
        self.assertTrue(out["pipeline"])

    def test_valid_at_never_follows_the_episode(self):
        out = run_probe(CLAMP)
        self.assertEqual(out["extraction"], "2026-09-28T00:00:00+00:00")
        self.assertEqual(out["prompt"], "2026-09-28T00:00:00+00:00")
        self.assertEqual(out["ok"], "2026-09-25T00:00:00+00:00")
        # A duplicate resolves to the stored edge, whose dates are not ours to change.
        self.assertTrue(out["duplicate_resolved_to_existing"])
        self.assertEqual(out["existing"], "2026-11-07T00:00:00+00:00")
        self.assertEqual(out["clamp_logs"], 2)


class InstallTests(unittest.TestCase):
    def probe(self, body):
        proc = subprocess.run([sys.executable, "-c", body], cwd=ROOT, capture_output=True,
                              text=True, timeout=60)
        return proc

    def test_refuses_to_sit_outside_the_batch_patches(self):
        proc = self.probe("from utils import batched_edge_dedupe, edge_invalidation_shadow\n"
                          "batched_edge_dedupe.install(100)\n"
                          "edge_invalidation_shadow.install('shadow')\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("before the batch patches", proc.stderr)

    def test_upstream_installs_nothing_and_shadow_installs_once(self):
        proc = self.probe(
            "import graphiti_core.utils.maintenance.edge_operations as ops\n"
            "from utils import edge_invalidation_shadow as mod\n"
            "before = (ops.resolve_extracted_edge, ops.resolve_extracted_edges)\n"
            "mod.install('upstream')\n"
            "same = before == (ops.resolve_extracted_edge, ops.resolve_extracted_edges)\n"
            "mod.install('shadow'); first = ops.resolve_extracted_edge\n"
            "mod.install('shadow')\n"
            "print(same, ops.resolve_extracted_edge is first, first is not before[0])\n")
        self.assertEqual(proc.stdout.strip(), "True True True", proc.stderr)

    def test_unknown_mode_is_rejected(self):
        proc = self.probe("from utils import edge_invalidation_shadow as mod\nmod.install('off')\n")
        self.assertIn("ValueError", proc.stderr)

    def test_graphiti_client_installs_shadow_by_default_before_batches(self):
        # Import order is behavior here: the client must load cleanly with both
        # batch patches enabled, and the default mode must be shadow.
        proc = subprocess.run(
            [sys.executable, "-c",
             "import graphiti_client, graphiti_core.utils.maintenance.edge_operations as ops\n"
             "from utils import edge_invalidation_shadow as mod\n"
             "print(mod._installed, ops.resolve_extracted_edge.__name__)\n"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
            env={"PATH": "", "KG_HUB_EDGE_TIMESTAMP_BATCH_PERCENT": "100",
                 "KG_HUB_EDGE_DEDUPE_BATCH_PERCENT": "100", "KG_HUB_ENV_FILE": "/dev/null"})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "True batched_resolve_edge")


if __name__ == "__main__":
    unittest.main()
