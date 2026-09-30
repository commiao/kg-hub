"""Read-only, bounded A/B of original attribute prompts: batches of 8 vs 16.

Run via stdin in the server container. No graph writes or pipeline changes.
Dry-run selects up to three committed snapshots; --execute --sample N makes
exactly two baseline calls and one merged call, with durable replay receipts.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("KG_PROBE_APP", "/app"))
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.config import LLMConfig
from schema import ENTITY_TYPES
from utils import batched_node_attributes as attrs


def typed_nodes(nodes):
    result = []
    for node in nodes:
        label = next((x for x in node.labels if x != "Entity"), "")
        model = ENTITY_TYPES.get(label)
        if model is not None and model.model_fields:
            result.append((node, model))
    return result


async def capture(sample, batch_size):
    nodes = [EntityNode.model_validate(x) for x in sample["nodes"]]
    typed = typed_nodes(nodes)
    requests = []

    class Recorder(AnthropicClient):
        async def _generate_response(self, messages, response_model=None, max_tokens=None, model_size=None):
            offset = sum(len(x["uuids"]) for x in requests)
            count = len(response_model.model_fields)
            batch = typed[offset:offset + count]
            requests.append({"messages": [x.model_dump() for x in messages],
                             "model": response_model,
                             "uuids": [x.uuid for x, _ in batch]})
            return ({f"entity_{i}": schema().model_dump()
                     for i, (_, schema) in enumerate(batch)}, 0, 0)

    async def no_tail(clients, nodes, *args, **kwargs):
        return nodes

    with patch.object(attrs, "BATCH_SIZE", batch_size), patch.object(attrs, "_original", no_tail):
        await attrs.extract_attributes_from_nodes(
            SimpleNamespace(llm_client=Recorder(config=LLMConfig(api_key="probe", model="probe", max_tokens=4096))), nodes,
            EpisodicNode.model_validate(sample["episode"]),
            [EpisodicNode.model_validate(x) for x in sample["previous_episodes"]],
            ENTITY_TYPES)
    return requests


def select(journal, before, limit=3):
    db = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
    samples = []
    seen = set()
    try:
        rows = db.execute("SELECT task_sd,task_sid,operation_id,artifact_json "
                          "FROM graphiti_stage_artifacts WHERE stage='operation_envelope' "
                          "ORDER BY rowid DESC LIMIT 1500").fetchall()
        for sd, sid, op, raw in rows:
            envelope = json.loads(raw)
            if envelope.get("now", "") >= before or (sd, sid) in seen:
                continue
            def load(stage, operation=op):
                row = db.execute("SELECT artifact_json FROM graphiti_stage_artifacts "
                                 "WHERE task_sd=? AND task_sid=? AND operation_id=? AND stage=?",
                                 (sd, sid, operation, stage)).fetchone()
                return json.loads(row[0]) if row else None
            if load("graph_commit_receipt") is None:
                continue
            selected = load("parallel_selected_round") or {"round": 0}
            round_op = op + f":graph-round:{selected['round']}"
            resolved = load("resolved_nodes", round_op)
            prepared = load("prepared_commit", round_op)
            if not resolved or not prepared:
                continue
            nodes = [EntityNode.model_validate(x) for x in resolved["nodes"]]
            typed = typed_nodes(nodes)
            if not 9 <= len(typed) <= 16 or len({n.group_id for n in nodes}) != 1:
                continue
            if len({n.uuid for n, _ in typed}) != len(typed):
                continue
            sample = {"sid": sid, "sd": sd, "operation_id": op,
                      "nodes": resolved["nodes"], "episode": envelope["episode"],
                      "previous_episodes": envelope["previous_episodes"],
                      "typed_count": len(typed)}
            if len(json.dumps(sample, ensure_ascii=False)) > 150000:
                continue
            samples.append(sample)
            seen.add((sd, sid))
            if len(samples) == limit:
                break
    finally:
        db.close()
    return samples


def flatten(request, payload):
    validated = request["model"].model_validate(payload)
    return {uuid: getattr(validated, f"entity_{i}").model_dump()
            for i, uuid in enumerate(request["uuids"])}


def compare(baseline, merged):
    differences = []
    equal = 0
    for uuid in sorted(set(baseline) | set(merged)):
        left, right = baseline.get(uuid, {}), merged.get(uuid, {})
        for field in sorted(set(left) | set(right)):
            if field in left and field in right and left[field] == right[field]:
                equal += 1
            else:
                differences.append({"uuid": uuid, "field": field,
                                    "baseline": left.get(field), "merged": right.get(field),
                                    "missing": field not in left or field not in right})
    return {"equal_fields": equal, "different_fields": len(differences),
            "differences": differences}


def call(request, directory):
    from anthropic import Anthropic, APIStatusError
    model = os.environ["ANTHROPIC_MODEL"]
    schema = request["model"].model_json_schema()
    name = request["model"].__name__
    body = dict(model=model, max_tokens=4096, temperature=LLMConfig().temperature,
                system=request["messages"][0]["content"],
                messages=[{"role": "user", "content": request["messages"][1]["content"]}],
                tools=[{"name": name, "description": schema.get("description", f"Extract {name} information"),
                        "input_schema": schema}], tool_choice={"type": "tool", "name": name},
                extra_body={"thinking": {"type": "disabled"}})
    digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = directory / (digest + ".json")
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved.get("phase") != "completed":
            raise RuntimeError("unknown prior outcome; refusing another paid call: " + digest)
        return saved
    fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"phase": "prepared", "digest": digest}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    client = Anthropic(auth_token=os.environ["KG_HUB_MODEL_GATEWAY_TOKEN"],
                       base_url=os.environ.get("ANTHROPIC_BASE_URL", "http://model-gateway:39000"),
                       max_retries=0, timeout=240)
    start = time.monotonic()
    try:
        response = client.messages.create(**body, extra_headers={
            "Idempotency-Key": "kg-attr-ab-v1-" + digest[:48],
            "X-Model-Gateway-Scenario": "attribute_prompt_probe"})
    except APIStatusError as exc:
        # An HTTP error is not proof that no provider work occurred. Preserve
        # it and refuse automatic resubmission, just like an unknown outcome.
        with receipt.open("w") as stream:
            json.dump({"phase": "http_error", "digest": digest,
                       "status": exc.status_code, "request_id": exc.request_id}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        raise
    blocks = [x.input for x in response.content if x.type == "tool_use" and x.name == name]
    saved = {"phase": "completed", "elapsed": time.monotonic() - start,
             "usage": response.usage.model_dump(), "stop_reason": response.stop_reason,
             "payload": blocks[0] if len(blocks) == 1 else None}
    # Keep prepared receipt if interrupted before atomic replacement.
    temp = receipt.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(saved, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, receipt)
    return saved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", default="/backup/model-attempts.sqlite3")
    parser.add_argument("--before", default="2026-09-30T09:00:00+00:00")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--sample", type=int, choices=range(3), default=0)
    parser.add_argument("--save-dir", type=Path, default=Path("/tmp/kg-attribute-probe"))
    args = parser.parse_args()
    samples = select(args.journal, args.before)
    print(json.dumps({"samples": [{"index": i, "sid": s["sid"], "typed": s["typed_count"]}
                                   for i, s in enumerate(samples)]}), flush=True)
    if not args.execute:
        return
    sample = samples[args.sample]
    outputs = {}
    metrics = {}
    for label, size in (("baseline", 8), ("merged", 16)):
        requests = asyncio.run(capture(sample, size))
        assert len(requests) == (2 if size == 8 else 1)
        outputs[label] = {}
        metrics[label] = {"calls": 0, "elapsed": 0, "input_tokens": 0, "output_tokens": 0}
        for request in requests:
            saved = call(request, args.save_dir)
            outputs[label].update(flatten(request, saved["payload"]))
            metrics[label]["calls"] += 1
            metrics[label]["elapsed"] += saved["elapsed"]
            for key in ("input_tokens", "output_tokens"):
                metrics[label][key] += saved["usage"].get(key, 0)
            print(json.dumps({"sample": args.sample, "stage": label, "completed": metrics[label]["calls"]}), flush=True)
    result = {"sid": sample["sid"], "metrics": metrics,
              "comparison": compare(outputs["baseline"], outputs["merged"])}
    output = args.save_dir / (sample["sid"] + "-comparison.json")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({**result, "sample": sample, "outputs": outputs}, stream, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
