"""Stage names for the pinned Graphiti 0.29.0 paid prompt boundaries."""

from __future__ import annotations


def graphiti_model_stage(prompt_name: str | None) -> str | None:
    """Map exact Graphiti prompt names to their replayable business stage."""
    if not prompt_name:
        return None
    if prompt_name in {
        "extract_nodes.extract_message", "extract_nodes.extract_text",
        "extract_nodes.extract_json",
    }:
        return "node_extraction"
    if prompt_name == "dedupe_nodes.nodes":
        return "node_resolution"
    if prompt_name in {
        "extract_edges.edge", "extract_edges.extract_timestamps",
        "extract_edges.extract_timestamps_batch",
        "extract_edges.extract_attributes", "dedupe_edges.resolve_edge",
        "dedupe_edges.resolve_edge_batch",
    }:
        return "edge_phase"
    if prompt_name in {
        "extract_nodes.extract_attributes",
        "extract_nodes.extract_summaries_batch",
        "extract_nodes.extract_entity_summaries_from_episodes",
    }:
        return "attribute_phase"
    # Unknown names stay untagged; a later replay gate will fail closed.
    return None
