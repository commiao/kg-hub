"""T-0285: the previous-episode window must not be answered from Episodic.valid_at's index.

FalkorDB itself is not available here; the defect was reproduced read-only on the
production graph (2026-10-14): ``e.valid_at <= $reference_time`` returned all 20,042
episodes, while ``toString(e.valid_at) <= $reference_time`` returned the ten before
the reference time in ~20 ms. These tests pin the query shape and the call path.
"""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.graphiti import Graphiti
from graphiti_core.nodes import EpisodeType
from graphiti_core.utils.maintenance import graph_data_operations as ops

from utils import falkordb_episode_window as mod

UPSTREAM = ops.retrieve_episodes
mod.install()
FIRST = ops.retrieve_episodes
mod.install()

NOW = datetime(2026, 6, 9, 12, tzinfo=timezone.utc)


class FakeDriver:
    def __init__(self, provider=GraphProvider.FALKORDB, rows=()):
        self.provider = provider
        self.graph_operations_interface = None
        self.rows = list(rows)
        self.calls = []

    async def execute_query(self, query, **params):
        self.calls.append((query, params))
        return self.rows, None, None


def row(uuid, valid_at):
    return {"uuid": uuid, "name": uuid, "group_id": "kg_hub", "created_at": valid_at,
            "source": "text", "source_description": "t", "content": "c",
            "valid_at": valid_at, "entity_edges": []}


def run(coro):
    return asyncio.run(coro)


class EpisodeWindowTests(unittest.TestCase):
    def test_filter_bypasses_the_index_and_reference_time_is_utc(self):
        driver = FakeDriver(rows=[row("b", NOW), row("a", NOW - timedelta(days=1))])
        local = NOW.astimezone(timezone(timedelta(hours=8)))
        episodes = run(ops.retrieve_episodes(driver, local, 10, ["kg_hub"], EpisodeType.text))
        query, params = driver.calls[0]
        self.assertIn("WHERE toString(e.valid_at) <= $reference_time", query)
        self.assertNotIn("WHERE e.valid_at <=", query)
        self.assertIn("ORDER BY e.valid_at DESC", query)
        self.assertEqual(params["reference_time"], "2026-06-09T12:00:00+00:00")
        self.assertEqual(params["group_ids"], ["kg_hub"])
        self.assertEqual(params["source"], "text")
        self.assertEqual(params["num_episodes"], 10)
        # Upstream returns the window oldest first.
        self.assertEqual([e.uuid for e in episodes], ["a", "b"])

    def test_graphiti_add_episode_path_uses_the_fixed_window(self):
        # add_episode, the stage adapter and the checkpoint path all go through
        # Graphiti.retrieve_episodes, which calls the name bound in its module.
        driver = FakeDriver()
        stub = SimpleNamespace(clients=SimpleNamespace(driver=driver))
        run(Graphiti.retrieve_episodes(stub, NOW, last_n=10, group_ids=["kg_hub"],
                                       source=EpisodeType.text))
        self.assertIn("toString(e.valid_at)", driver.calls[0][0])

    def test_other_providers_and_sagas_keep_the_upstream_query(self):
        for driver, saga in ((FakeDriver(provider=GraphProvider.NEO4J), None),
                             (FakeDriver(), "s")):
            run(ops.retrieve_episodes(driver, NOW, 10, ["kg_hub"], EpisodeType.text, saga))
            self.assertNotIn("toString(e.valid_at)", driver.calls[0][0])
            self.assertIn("e.valid_at <= $reference_time", driver.calls[0][0])

    def test_install_is_idempotent(self):
        self.assertIsNot(FIRST, UPSTREAM)
        self.assertIs(ops.retrieve_episodes, FIRST)


if __name__ == "__main__":
    unittest.main()
