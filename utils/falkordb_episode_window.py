"""Make Graphiti 0.29.0's "previous episodes" window honor reference_time on FalkorDB.

add_episode feeds the extraction prompts the last RELEVANT_SCHEMA_LIMIT episodes
before the new episode's reference_time (``WHERE e.valid_at <= $reference_time``).
Episodic.valid_at is stored as an ISO string and carries a RANGE index; when
FalkorDB answers that predicate from the index it returns every episode, so the
window was always the globally newest ten (T-0285, 2026-10-14: the same query
with reference_time 06-09 and 09-25 both returned 10-12..10-14 episodes).

While a backlog was ingested, the extractor therefore saw *later* episodes as
PREVIOUS_MESSAGES and, as the prompt asks it to use "the timestamp of the episode
the fact originates from", copied their timestamps into new edges' valid_at.
23,061 edges ended up dated after their own episode, and those inflated dates
are the invalid_at clusters seen on expired edges.

Comparing ``toString(e.valid_at)`` keeps the planner off that index; the filter
and ordering are otherwise the upstream query. reference_time is normalized to
UTC so the string comparison matches the stored ``+00:00`` values (32 legacy
episodes carry ``+08:00`` and may sort up to eight hours off).
"""
from __future__ import annotations

from importlib.metadata import version

QUERY = """
MATCH (e:Episodic)
WHERE toString(e.valid_at) <= $reference_time
{filters}
RETURN
{returns}
ORDER BY e.valid_at DESC
LIMIT $num_episodes
"""


def install():
    """Rebind Graphiti's episode-window helper for FalkorDB, once."""
    if version("graphiti-core") != "0.29.0":
        raise RuntimeError("unsupported Graphiti version for the episode window fix")
    import graphiti_core.graphiti as pipeline
    from graphiti_core.driver.driver import GraphProvider
    from graphiti_core.models.nodes.node_db_queries import EPISODIC_NODE_RETURN
    from graphiti_core.nodes import get_episodic_node_from_record
    from graphiti_core.utils.datetime_utils import ensure_utc
    from graphiti_core.utils.maintenance import graph_data_operations as ops

    if getattr(ops.retrieve_episodes, "_kg_episode_window", False):
        return
    original = ops.retrieve_episodes

    async def retrieve_episodes(driver, reference_time, last_n=ops.EPISODE_WINDOW_LEN,
                                group_ids=None, source=None, saga=None):
        if (saga is not None or driver.provider != GraphProvider.FALKORDB
                or driver.graph_operations_interface):
            return await original(driver, reference_time, last_n, group_ids, source, saga)
        filters, params = [], {}
        if group_ids:
            filters.append("AND e.group_id IN $group_ids")
            params["group_ids"] = group_ids
        if source is not None:
            filters.append("AND e.source = $source")
            params["source"] = source.name
        records, _, _ = await driver.execute_query(
            QUERY.format(filters="\n".join(filters), returns=EPISODIC_NODE_RETURN),
            reference_time=ensure_utc(reference_time).isoformat(),
            num_episodes=last_n, **params)
        episodes = [get_episodic_node_from_record(record) for record in records]
        return list(reversed(episodes))  # chronological, as upstream returns

    retrieve_episodes._kg_episode_window = True
    ops.retrieve_episodes = retrieve_episodes
    # Graphiti.retrieve_episodes calls the name it imported into its own module.
    pipeline.retrieve_episodes = retrieve_episodes
