"""Read-only, bounded A/B of original attribute prompts: batches of 8 vs 16.

Run via stdin in the server container. No graph writes or pipeline changes.
Dry-run selects up to six committed snapshots; --execute --sample N makes
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

CONSOLIDATION_RULES = """
For EVERY entity key, independently check the current episode, previous episodes,
and that entity's existing attributes before answering. Do not skip an entity
because other entities have similar names or because some entities have long text.
The current episode's explicit facts take precedence over earlier conflicting facts.
Preserve each non-empty existing attribute unless explicit supplied evidence changes
or invalidates it. Lack of a new mention is not evidence for replacing it with null.
Inspect ALL supplied source sections, including Files read and Project metadata.
For a named file explicitly listed with a full path, extract that supplied path;
use its explicitly associated project identifier. Never infer an absolute path or
repository URL from a name. Do not borrow another entity's existing attributes.
For descriptions, retain supported existing facts and incorporate new relevant
facts or corrections; do not leave a description empty when the source explicitly
explains that entity. Unknown values with no existing value remain null.
Before returning, internally verify every entity key and every field: all explicit
relevant new facts considered, existing supported information retained, no invented
values and no cross-entity transfer. Return only the required structured response.
""".strip()

UPDATE_RECORDS_PROMPT = """
Update the supplied existing entity records using the supplied source messages.
Return the COMPLETE updated record for EVERY entity key using its own schema.
The existing attributes are the starting state, not examples to discard.

For each entity, work field by field in this order:
1. Start with that entity's existing field value. Copy it unchanged when the
   messages give no relevant update. An absent mention is NOT a deletion.
2. Read the current episode, including its narrative, facts, Files read and
   Project metadata, for explicit facts about this entity. Apply relevant facts.
3. Use previous episodes for additional explicit facts only when they do not
   conflict with the current episode. Current explicit corrections take priority.
4. Return null only if no value exists and no supplied source establishes one,
   or if explicit source evidence invalidates the old value without a replacement.

Keep the identity and evidence for each entity separate. Never copy a value from
another entity merely because their names are similar. Use only supplied values;
do not invent repository URLs, absolute paths, versions or project identifiers.
For files, inspect explicitly listed file paths and explicitly associated project
metadata. A source may establish a field outside the narrative paragraph.
For descriptions, retain still-valid existing facts and incorporate relevant new
facts and corrections. Preserve conditions, exceptions and negation. Replace
contradicted claims rather than retaining both. Avoid losing facts by shortening.

Examples of the update rule (unrelated to the actual records):
- Existing version is "2.4"; no message mentions version: return "2.4".
- Existing path is null; the source lists that file at "lib/alpha.py": return
  "lib/alpha.py" for that file's path.
- Existing status is "open"; current source explicitly says it is resolved:
  return "resolved".

Before submitting, check EVERY field of EVERY record against its starting value
and the source. Any removed nonempty value needs explicit invalidating evidence.
Check that explicit new facts are covered and that no entity borrowed another's
attributes. Return only the required structured response, with all entity keys.
""".strip()


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


def select(journal, before, limit=6):
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


def missing_fields(request, payload):
    """Audit raw omission before nullable schema defaults hide it as null.

    This does not fill fields or accept an incomplete answer as quality-passing.
    Invalid payloads still fail the original validation in flatten().
    """
    missing = []
    for i, uuid in enumerate(request["uuids"]):
        key = f"entity_{i}"
        fields = request["model"].model_fields[key].annotation.model_fields
        record = payload.get(key, {})
        if not isinstance(record, dict):
            continue
        for field in fields:
            if field not in record:
                missing.append({"uuid": uuid, "field": field})
    return missing


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


def lost_existing_values(sample, output):
    """Report losses even when baseline and candidate make the SAME mistake.

    A reported loss needs source review (explicit invalidation may be legitimate).
    This audit neither repairs model output nor certifies semantic quality.
    """
    losses = []
    for node in sample["nodes"]:
        result = output.get(node["uuid"])
        if result is None:
            continue
        for field, old in node.get("attributes", {}).items():
            if field in result and old not in (None, "") and result[field] in (None, ""):
                losses.append({"uuid": node["uuid"], "field": field})
    return losses


def call(request, directory, resume_rejected_digest=None):
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
    prior_error = None
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved.get("phase") == "completed":
            return saved
        if (saved.get("phase") == "http_error" and saved.get("status") == 503
                and resume_rejected_digest == digest and not saved.get("prior_error")):
            # Explicit operator recovery only after checking the gateway ledger,
            # HTTP-start index AND independent witness for this exact identity.
            # Keep the old evidence and the EXACT original key/body. Never
            # recover a prepared/unknown result, or automatically loop on 503.
            prior_error = saved
        else:
            raise RuntimeError("unknown prior outcome; refusing another paid call: " + digest)
    fd = os.open(receipt, os.O_WRONLY | (os.O_TRUNC if prior_error else os.O_CREAT | os.O_EXCL), 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"phase": "prepared", "digest": digest, "prior_error": prior_error}, stream)
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
                       "status": exc.status_code, "request_id": (exc.body or {}).get("request_id") or exc.request_id,
                       "error_code": (exc.body or {}).get("error", {}).get("code"),
                       "prior_error": prior_error}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        raise
    blocks = [x.input for x in response.content if x.type == "tool_use" and x.name == name]
    saved = {"phase": "completed", "elapsed": time.monotonic() - start, "prior_error": prior_error,
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
    parser.add_argument("--sample", type=int, choices=range(6), default=0)
    parser.add_argument("--resume-rejected-digest", help="Exact SHA256; requires manual no-provider audit across all three gateway ledgers")
    parser.add_argument("--strengthened", action="store_true", help="Add per-entity coverage and preservation instructions only to merged prompt")
    parser.add_argument("--update-records", action="store_true", help="Replace merged system prompt with full-record update instructions")
    parser.add_argument("--save-dir", type=Path, default=Path("/tmp/kg-attribute-probe"))
    args = parser.parse_args()
    if args.strengthened and args.update_records:
        parser.error("choose only one prompt variant")
    samples = select(args.journal, args.before)
    print(json.dumps({"samples": [{"index": i, "sid": s["sid"], "typed": s["typed_count"]}
                                   for i, s in enumerate(samples)]}), flush=True)
    if not args.execute:
        return
    sample = samples[args.sample]
    outputs = {}
    metrics = {}
    omissions = {}
    for label, size in (("baseline", 8), ("merged", 16)):
        requests = asyncio.run(capture(sample, size))
        if label == "merged" and args.strengthened:
            requests[0]["messages"][0]["content"] += "\n\n" + CONSOLIDATION_RULES
        if label == "merged" and args.update_records:
            requests[0]["messages"][0]["content"] = UPDATE_RECORDS_PROMPT
        assert len(requests) == (2 if size == 8 else 1)
        outputs[label] = {}
        omissions[label] = []
        metrics[label] = {"calls": 0, "elapsed": 0, "input_tokens": 0, "output_tokens": 0}
        for request in requests:
            saved = call(request, args.save_dir, args.resume_rejected_digest)
            outputs[label].update(flatten(request, saved["payload"]))
            omissions[label].extend(missing_fields(request, saved["payload"]))
            metrics[label]["calls"] += 1
            metrics[label]["elapsed"] += saved["elapsed"]
            for key in ("input_tokens", "output_tokens"):
                metrics[label][key] += saved["usage"].get(key, 0)
            print(json.dumps({"sample": args.sample, "stage": label, "completed": metrics[label]["calls"]}), flush=True)
    result = {"sid": sample["sid"], "strengthened": args.strengthened, "update_records": args.update_records, "metrics": metrics,
              "omitted_fields": omissions,
              "existing_value_losses": {label: lost_existing_values(sample, output)
                                        for label, output in outputs.items()},
              "comparison": compare(outputs["baseline"], outputs["merged"])}
    suffix = "-update-records" if args.update_records else "-strengthened" if args.strengthened else ""
    output = args.save_dir / (sample["sid"] + suffix + "-comparison.json")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({**result, "sample": sample, "outputs": outputs}, stream, ensure_ascii=False)
    # Detailed values stay in the private artifact for source-based review.
    print(json.dumps({**result, "comparison": {k: v for k, v in result["comparison"].items()
                                               if k != "differences"}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
