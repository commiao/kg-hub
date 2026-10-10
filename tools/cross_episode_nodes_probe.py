"""Isolated two-observation entity extraction comparison; never writes a graph.

Each item embeds its own unmodified Graphiti text-extraction user prompt.
Graphiti text extraction uses the current text; prior episodes are not present
in that original prompt. The batch wrapper changes the output shape
and requires source identity; it does not claim semantic equivalence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from graphiti_core.nodes import EpisodicNode
from graphiti_core.prompts import prompt_library
from graphiti_core.prompts.extract_nodes import ExtractedEntity
from graphiti_core.utils.maintenance.node_operations import _build_entity_types_context
from schema import ENTITY_TYPES

DEV_IDS = ("a410cad84c26ced4", "e5495f3cb3d24406")
CAMPAIGN = "cross-episode-nodes-20261002"


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_obs_id: str
    extracted_entities: list[ExtractedEntity]


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Item]


def load_pair(snapshot: Path, ids: tuple[str, str] = DEV_IDS) -> list[dict]:
    raw = json.loads(snapshot.read_text())
    selected = [s for row in raw["samples"] if (s := row.get("sample", row))["sid"] in ids]
    if len(selected) != len(ids):
        raise ValueError("missing or duplicate frozen source")
    items = {s["sid"]: s for s in selected}
    if set(items) != set(ids):
        raise ValueError("missing or duplicate frozen source")
    pair = [items[sid] for sid in ids]
    if any(EpisodicNode.model_validate(s["episode"]).source.value != "text" for s in pair):
        raise ValueError("development pair must use the original text extraction prompt")
    return pair


def build_request(pair: list[dict]) -> dict:
    if len(pair) != 2 or len({s["sid"] for s in pair}) != 2:
        raise ValueError("two distinct observations required")
    entity_types = _build_entity_types_context(ENTITY_TYPES)
    original = []
    for sample in pair:
        episode = EpisodicNode.model_validate(sample["episode"])
        previous = [EpisodicNode.model_validate(ep) for ep in sample["previous_episodes"]]
        context = {
            "episode_content": episode.content,
            "episode_timestamp": episode.valid_at.isoformat(),
            "previous_episodes": [{"content": ep.content,
                                   "timestamp": ep.valid_at.isoformat() if ep.valid_at else None}
                                  for ep in previous],
            "custom_extraction_instructions": "",
            "entity_types": entity_types,
            "source_description": episode.source_description,
        }
        messages = [message.model_dump() for message in prompt_library.extract_nodes.extract_text(context)]
        if len(messages) != 2:
            raise RuntimeError("unexpected Graphiti text prompt shape")
        original.append((sample["sid"], messages))
    if original[0][1][0]["content"] != original[1][1][0]["content"]:
        raise RuntimeError("original system prompts differ")
    system = original[0][1][0]["content"] + (
        "\nProcess each observation as an independent entity extraction task. "
        "Use only that item's original prompt and source text. "
        "Return exactly one item for each source_obs_id. In each item, "
        "episode_indices must be [0], as its task contains one episode. "
        "Do not transfer entities or context between items.")
    user = {"items": [{"source_obs_id": sid, "original_user_prompt": messages[1]["content"]}
                      for sid, messages in original]}
    return {"messages": [{"content": system}, {"content": json.dumps(user, ensure_ascii=False)}],
            "model": Batch, "temperature": 0.0, "source_ids": [s["sid"] for s in pair],
            "original_messages": original, "entity_type_count": len(entity_types)}


def validate(request: dict, payload: dict) -> dict[str, list[dict]]:
    batch = Batch.model_validate(payload)
    ids = request["source_ids"]
    if len(batch.items) != len(ids) or {item.source_obs_id for item in batch.items} != set(ids):
        raise ValueError("missing, duplicate, or unknown source identity")
    result = {}
    for item in batch.items:
        for entity in item.extracted_entities:
            if (not entity.name.strip() or type(entity.entity_type_id) is not int
                    or not 0 <= entity.entity_type_id < request["entity_type_count"]
                    or entity.episode_indices != [0]):
                raise ValueError("invalid entity name, type, or episode attribution")
        result[item.source_obs_id] = [entity.model_dump() for entity in item.extracted_entities]
    return result


def plan(request: dict) -> dict:
    body = {"messages": request["messages"], "schema": request["model"].model_json_schema(),
            "temperature": request["temperature"]}
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"campaign": CAMPAIGN, "source_ids": request["source_ids"],
            "input_sha256": digest, "original_prompt_chars": [len(x[1][1]["content"])
                                                     for x in request["original_messages"]],
            "combined_prompt_chars": len(request["messages"][1]["content"]),
            "max_new_calls": 1, "model": "qwen3.8-flash", "thinking": "disabled"}


def freeze(path: Path, value: dict) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if path.exists():
        if path.read_text() != encoded:
            raise RuntimeError("frozen experiment evidence changed")
        return
    import os
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path,
                        default=Path("/backup/attribute-probe/accuracy-holdout-recovered-20261002.json"))
    parser.add_argument("--directory", type=Path, default=Path("/backup/cross-episode-probe"))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    request = build_request(load_pair(args.snapshot))
    frozen = plan(request)
    if not args.execute:
        print(json.dumps(frozen, ensure_ascii=False))
        return
    args.directory.mkdir(parents=True, exist_ok=True)
    target = args.directory / "plan.json"
    encoded = json.dumps(frozen, ensure_ascii=False, sort_keys=True)
    if target.exists() and target.read_text() != encoded:
        raise RuntimeError("frozen plan changed")
    if not target.exists():
        freeze(target, frozen)
    from tools import attribute_prompt_probe as transport
    saved = transport.call(request, args.directory, structured_output=True)
    result = {"source_ids": request["source_ids"], "input_sha256": frozen["input_sha256"],
              "body_digest": saved["body_digest"], "elapsed": saved["elapsed"],
              "usage": saved["usage"], "stop_reason": saved["stop_reason"],
              "semantic_review": "pending"}
    try:
        result["entities_by_source"] = validate(request, saved["payload"])
        result["schema_valid"] = True
    except Exception as exc:
        result["schema_valid"] = False
        result["validation_error"] = type(exc).__name__ + ": " + str(exc)[:600]
    result_path = args.directory / "result.json"
    if result_path.exists():
        if json.loads(result_path.read_text()) != result:
            raise RuntimeError("frozen result changed")
    else:
        freeze(result_path, result)
    print(json.dumps({k: v for k, v in result.items() if k != "entities_by_source"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
