"""Optional FalkorDB vector-index path for ingest edge candidates.

The UUID-filtered dedupe lookup stays exact. The broad invalidation lookup can
use HNSW candidates, then the original cosine formula reranks those candidates.
Only optimistic ingest drivers are marked eligible, so user-facing searches and
restored exact read dependencies retain their original queries.
"""
from __future__ import annotations

import hashlib
import logging
import os

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.edges import get_entity_edge_from_record
from graphiti_core.models.edges.edge_db_queries import get_entity_edge_return_query

from utils.ingest_workflow import current_workflow

log = logging.getLogger("kg_hub.vector_search")

_QUERY = (
    "CALL db.idx.vector.queryRelationships('RELATES_TO', 'fact_embedding', "
    "$candidate_limit, vecf32($search_vector)) YIELD relationship AS e "
    "WITH e WHERE e.group_id IN $group_ids "
    "WITH e, startNode(e) AS n, endNode(e) AS m, "
    "(2 - vec.cosineDistance(e.fact_embedding, vecf32($search_vector)))/2 AS score "
    "WHERE score > $min_score RETURN "
    + get_entity_edge_return_query(GraphProvider.FALKORDB)
    + " ORDER BY score DESC LIMIT $limit"
)


def _eligible(driver, source_node_uuid, target_node_uuid, search_filter, group_ids):
    if (driver.provider != GraphProvider.FALKORDB
            or getattr(driver, "_database", None) != "kg_hub"
            or not getattr(driver, "_kg_hub_indexed_ingest", False)):
        return False
    if source_node_uuid is not None or target_node_uuid is not None:
        return False
    if group_ids != ["kg_hub"] or search_filter.model_dump(exclude_none=True):
        return False
    try:
        percent = max(0, min(100, int(os.environ.get("KG_HUB_EDGE_VECTOR_INDEX_PERCENT", "0"))))
    except ValueError:
        return False
    if percent == 0:
        return False
    workflow = current_workflow()
    if not workflow or not workflow.get("plan", {}).get("source_obs_id"):
        return False
    sid = str(workflow["plan"]["source_obs_id"])
    bucket = int.from_bytes(hashlib.sha256(sid.encode()).digest()[:4], "big") % 100
    return bucket < percent


def install():
    """Bind Graphiti's actual search call site once; disabled by default."""
    import importlib
    search = importlib.import_module("graphiti_core.search.search")
    original = search.edge_similarity_search
    if getattr(original, "_kg_hub_indexed_edge_wrapper", False):
        return

    async def indexed_or_exact(driver, search_vector, source_node_uuid, target_node_uuid,
                               search_filter, group_ids=None, limit=10, min_score=0.6):
        if not _eligible(driver, source_node_uuid, target_node_uuid, search_filter, group_ids):
            return await original(driver, search_vector, source_node_uuid, target_node_uuid,
                                  search_filter, group_ids, limit, min_score)
        try:
            records, _, _ = await driver.execute_query(
                _QUERY, search_vector=search_vector, group_ids=group_ids,
                candidate_limit=max(1024, limit * 32), limit=limit,
                min_score=min_score, routing_="r")
        except Exception as exc:
            # Never disguise a read-set conflict or an unrelated graph error as
            # an index outage; those require the normal retry/freeze behavior.
            if not any(word in str(exc).lower() for word in ("index", "vector")):
                raise
            # An unsuccessful indexed read has no durable dependency. Fall back
            # to Graphiti's exact query, which becomes the recorded dependency.
            log.warning("[ingest:vector_fallback] reason=index_query_failed")
            return await original(driver, search_vector, source_node_uuid, target_node_uuid,
                                  search_filter, group_ids, limit, min_score)
        log.info("[ingest:vector_query] candidates=%d returned=%d", max(1024, limit * 32),
                 len(records))
        return [get_entity_edge_from_record(record, driver.provider) for record in records]

    indexed_or_exact._kg_hub_indexed_edge_wrapper = True
    search.edge_similarity_search = indexed_or_exact
