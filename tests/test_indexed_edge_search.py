"""The index canary changes only broad ingest edge searches."""
import asyncio
import importlib
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.search.search_filters import SearchFilters

from utils import indexed_edge_search as indexed


class IndexedEdgeSearchTests(unittest.TestCase):
    def test_canary_uses_index_and_exact_rerank_only_for_broad_ingest_search(self):
        search = importlib.import_module("graphiti_core.search.search")
        prior = search.edge_similarity_search

        async def exact(*args, **kwargs):
            return ["exact"]

        driver = SimpleNamespace(provider=GraphProvider.FALKORDB,
                                 _database="kg_hub",
                                 _kg_hub_indexed_ingest=True,
                                 execute_query=AsyncMock(return_value=([{"uuid": "edge"}], [], None)))
        workflow = {"plan": {"source_obs_id": "obs-123"}}
        search.edge_similarity_search = exact
        try:
            indexed.install()
            with patch.dict(os.environ, {"KG_HUB_EDGE_VECTOR_INDEX_PERCENT": "100"}), \
                 patch.object(indexed, "current_workflow", return_value=workflow), \
                 patch.object(indexed, "get_entity_edge_from_record", side_effect=lambda row, _: row["uuid"]):
                call = search.edge_similarity_search
                result = asyncio.run(call(driver, [1.0], None, None, SearchFilters(),
                                          ["kg_hub"], 20, 0.6))
                self.assertEqual(result, ["edge"])
                query = driver.execute_query.await_args.args[0]
                self.assertIn("db.idx.vector.queryRelationships", query)
                self.assertIn("vec.cosineDistance(e.fact_embedding", query)
                self.assertEqual(driver.execute_query.await_args.kwargs["candidate_limit"], 1024)

                result = asyncio.run(call(driver, [1.0], None, None,
                                          SearchFilters(edge_uuids=["old"]), ["kg_hub"], 20, 0.6))
                self.assertEqual(result, ["exact"])
                self.assertEqual(driver.execute_query.await_count, 1)

            with patch.dict(os.environ, {"KG_HUB_EDGE_VECTOR_INDEX_PERCENT": "0"}), \
                 patch.object(indexed, "current_workflow", return_value=workflow):
                self.assertEqual(asyncio.run(call(driver, [1.0], None, None,
                                                  SearchFilters(), ["kg_hub"], 20, 0.6)),
                                 ["exact"])
        finally:
            search.edge_similarity_search = prior


    def test_index_failure_falls_back_to_exact_read(self):
        search = importlib.import_module("graphiti_core.search.search")
        prior = search.edge_similarity_search

        async def exact(*args, **kwargs):
            return ["exact"]

        driver = SimpleNamespace(provider=GraphProvider.FALKORDB,
                                 _database="kg_hub",
                                 _kg_hub_indexed_ingest=True,
                                 execute_query=AsyncMock(side_effect=RuntimeError("index unavailable")))
        search.edge_similarity_search = exact
        try:
            indexed.install()
            with patch.dict(os.environ, {"KG_HUB_EDGE_VECTOR_INDEX_PERCENT": "100"}), \
                 patch.object(indexed, "current_workflow", return_value={"plan": {"source_obs_id": "obs-123"}}):
                result = asyncio.run(search.edge_similarity_search(
                    driver, [1.0], None, None, SearchFilters(), ["kg_hub"], 20, 0.6))
                self.assertEqual(result, ["exact"])
                driver.execute_query.side_effect = RuntimeError("graph changed within an unfinished resolution stage")
                with self.assertRaisesRegex(RuntimeError, "graph changed"):
                    asyncio.run(search.edge_similarity_search(
                        driver, [1.0], None, None, SearchFilters(), ["kg_hub"], 20, 0.6))
        finally:
            search.edge_similarity_search = prior


if __name__ == "__main__":
    unittest.main()
