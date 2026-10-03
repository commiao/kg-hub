"""Offline plan and budget-gated probe for one-call attributes plus summaries.

The frozen holdout is never modified. Its committed final graph round identifies
which typed nodes actually required a summary; the model sees only source text,
starting attributes and summaries, and a boolean target flag. Baseline summary
answers are never supplied to the candidate prompt.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from pydantic import ConfigDict, Field, ValidationError, create_model

from tools import attribute_prompt_probe as probe


DEFAULT_HOLDOUT = Path(
    "/backup/attribute-probe/accuracy-attribute-summary-joint-blind-holdout-20261002.json"
)
DEFAULT_JOURNAL = Path("/backup/model-attempts.sqlite3")


def _read_artifact(db, sample, operation_id, stage):
    row = db.execute(
        "SELECT artifact_json FROM graphiti_stage_artifacts "
        "WHERE task_sd=? AND task_sid=? AND operation_id=? AND stage=?",
        (sample["sd"], sample["sid"], operation_id, stage),
    ).fetchone()
    if row is None:
        raise ValueError(f"missing committed {stage} artifact")
    return json.loads(row[0])


def summary_targets(journal: Path, sample: dict) -> set[str]:
    """Recompute upstream's pre-summary choice without reading old answers."""
    from graphiti_core.utils.text_utils import MAX_SUMMARY_CHARS

    with sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True) as db:
        selected = _read_artifact(db, sample, sample["operation_id"],
                                  "parallel_selected_round")
        round_op = sample["operation_id"] + f":graph-round:{selected['round']}"
        edge_stage = _read_artifact(db, sample, round_op, "edge_phase")
    groups = edge_stage.get("groups")
    if not isinstance(groups, list) or len(groups) != 3:
        raise ValueError("edge phase has no new-edge group")
    edges_by_uuid: dict[str, list[str]] = {}
    for edge in groups[2]:
        for uuid in (edge["source_node_uuid"], edge["target_node_uuid"]):
            edges_by_uuid.setdefault(uuid, []).append(edge.get("fact") or "")
    targets = set()
    for node in sample["nodes"]:
        summary_with_edges = node.get("summary") or ""
        if edges_by_uuid.get(node["uuid"]):
            facts = "\n".join(x for x in edges_by_uuid[node["uuid"]] if x)
            summary_with_edges = f"{summary_with_edges}\n{facts}".strip()
        if not summary_with_edges or len(summary_with_edges) > MAX_SUMMARY_CHARS * 2:
            targets.add(node["uuid"])
    if not targets:
        raise ValueError("frozen sample has no summary target")
    return targets


async def build_requests(sample: dict, targets: set[str]) -> list[dict]:
    """Keep the production batch size of eight and add only targeted summaries."""
    requests = await probe.capture(sample, 8)
    nodes_by_uuid = {node["uuid"]: node for node in sample["nodes"]}
    covered: set[str] = set()
    typed_uuids = {uuid for request in requests for uuid in request["uuids"]}
    extra_nodes = [node for node in sample["nodes"]
                   if node["uuid"] in targets and node["uuid"] not in typed_uuids]
    for request_index, request in enumerate(requests):
        context = json.loads(request["messages"][1]["content"])
        fields = {}
        for index, uuid in enumerate(request["uuids"]):
            key = f"entity_{index}"
            node = nodes_by_uuid[uuid]
            wanted = uuid in targets
            if wanted:
                covered.add(uuid)
            context["entities"][key]["summary"] = node.get("summary") or ""
            context["entities"][key]["summary_required"] = wanted
            attribute_schema = request["model"].model_fields[key].annotation
            row_schema = create_model(
                f"JointRecord{index}", __config__=ConfigDict(extra="forbid"),
                attributes=(attribute_schema, Field(...)),
                summary=(str | None, Field(...)),
            )
            fields[key] = (row_schema, Field(...))
        request["summary_only_uuids"] = {}
        if request_index == len(requests) - 1:
            empty_attributes = create_model(
                "NoTypedAttributes", __config__=ConfigDict(extra="forbid"))
            for index, node in enumerate(extra_nodes):
                key = f"summary_only_{index}"
                context["entities"][key] = {
                    "name": node["name"], "entity_types": node["labels"],
                    "attributes": {}, "summary": node.get("summary") or "",
                    "summary_required": True,
                }
                row_schema = create_model(
                    f"SummaryOnlyRecord{index}", __config__=ConfigDict(extra="forbid"),
                    attributes=(empty_attributes, Field(...)),
                    summary=(str | None, Field(...)),
                )
                fields[key] = (row_schema, Field(...))
                request["summary_only_uuids"][key] = node["uuid"]
                covered.add(node["uuid"])
        request["model"] = create_model(
            "AttributeSummaryJointBatch", __config__=ConfigDict(extra="forbid"),
            **fields,
        )
        request["messages"][0]["content"] += (
            "\nUpdate attributes first using only the supplied episode, previous episodes "
            "and each entity's existing attributes. Then, for each entity marked "
            "summary_required=true, write its updated summary using the just-updated "
            "attributes, its existing summary and the same source evidence. Preserve "
            "supported old facts, resolve explicit corrections, and retain conditions, "
            "exceptions and negation. Do not move facts between entities or invent "
            "implementation details. For summary_required=false return summary=null. "
            "Return every entity key with both attributes and summary fields."
        )
        request["messages"][1]["content"] = json.dumps(context, ensure_ascii=False)
        request["summary_targets"] = {key for key, item in context["entities"].items()
                                      if item["summary_required"]}
    if covered != targets:
        raise ValueError("not every computed summary target is covered")
    return requests


def validate_output(request: dict, payload: dict) -> tuple[dict, dict]:
    """Project without repairing answers; semantic review remains separate."""
    if not isinstance(payload, dict) or set(payload) != set(request["model"].model_fields):
        raise ValueError("response entity keys are incomplete or unexpected")
    for key, field in request["model"].model_fields.items():
        row = payload[key]
        if not isinstance(row, dict) or set(row) != {"attributes", "summary"}:
            raise ValueError("response row fields are incomplete or unexpected")
        expected = set(field.annotation.model_fields["attributes"].annotation.model_fields)
        if not isinstance(row["attributes"], dict) or set(row["attributes"]) != expected:
            raise ValueError("attribute fields are incomplete or unexpected")
    result = request["model"].model_validate(payload).model_dump()
    attributes, summaries = {}, {}
    for index, uuid in enumerate(request["uuids"]):
        key = f"entity_{index}"
        item = result[key]
        summary = item["summary"]
        if key in request["summary_targets"]:
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("required summary is empty")
            summaries[uuid] = summary
        elif summary is not None:
            raise ValueError("non-target summary must be null")
        attributes[uuid] = item["attributes"]
    for key, uuid in request.get("summary_only_uuids", {}).items():
        item = result[key]
        if item["attributes"] != {} or not isinstance(item["summary"], str) \
                or not item["summary"].strip():
            raise ValueError("summary-only target is incomplete")
        summaries[uuid] = item["summary"]
    return attributes, summaries


def _freeze(path: Path, value: dict) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if path.exists():
        if path.read_text() != encoded:
            raise RuntimeError("frozen experiment plan changed")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdout", type=Path, default=DEFAULT_HOLDOUT)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--save-dir", type=Path,
                        default=Path("/backup/attribute-probe/attribute-summary-joint-v1"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-new-calls", type=int, default=0,
                        help="Explicitly authorized additional paid calls for this run")
    parser.add_argument("--sample-index", type=int,
                        help="Development screen only; omit for the frozen 15-sample holdout")
    args = parser.parse_args(argv)
    raw = args.holdout.read_bytes()
    holdout = json.loads(raw)
    samples = holdout["samples"]
    if len(samples) != 15 or len({(s["sd"], s["sid"]) for s in samples}) != 15:
        raise ValueError("expected 15 distinct frozen observations")
    if args.sample_index is not None:
        samples = [samples[args.sample_index]]
    planned = []
    target_counts = []
    for sample in samples:
        targets = summary_targets(args.journal, sample)
        requests = asyncio.run(build_requests(sample, targets))
        planned.append((sample, requests))
        target_counts.append(len(targets))
    calls = sum(len(requests) for _, requests in planned)
    manifest = {"holdout_sha256": hashlib.sha256(raw).hexdigest(),
                "sample_count": len(samples), "candidate_calls": calls,
                "summary_target_counts": target_counts,
                "candidate_batches": [len(reqs) for _, reqs in planned],
                "prompt_schema_sha256": [hashlib.sha256(json.dumps(
                    {"messages": request["messages"],
                     "schema": request["model"].model_json_schema()},
                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    for _, reqs in planned for request in reqs]}
    if not args.execute:
        print(json.dumps({k: v for k, v in manifest.items()
                          if k != "prompt_schema_sha256"}, ensure_ascii=False))
        return
    if args.approved_new_calls < calls:
        raise RuntimeError(f"explicit approved budget of at least {calls} new calls required")
    args.save_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _freeze(args.save_dir / "plan.json", manifest)
    for sample_index, (sample, requests) in enumerate(planned):
        for index, request in enumerate(requests):
            saved = probe.call(request, args.save_dir, structured_output=True)
            record = {"input_hash": manifest["holdout_sha256"],
                      "sample_index": sample_index, "batch_index": index,
                      "sample_sid": sample["sid"],
                      "body_digest": saved["body_digest"],
                      "elapsed": saved["elapsed"],
                      "usage": saved["usage"], "schema_valid": False,
                      "semantic_review": "pending"}
            try:
                attributes, summaries = validate_output(request, saved["payload"])
                record["schema_valid"] = True
                record["attribute_count"] = len(attributes)
                record["summary_count"] = len(summaries)
                record["attributes"] = attributes
                record["summaries"] = summaries
                record["existing_value_losses"] = probe.lost_existing_values(sample, attributes)
            except (ValidationError, ValueError, TypeError) as exc:
                record["structural_error"] = type(exc).__name__
            _freeze(args.save_dir / (sample["sid"] + f"-batch-{index}-result.json"), record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            if not record["schema_valid"]:
                raise RuntimeError("joint answer failed structure; stop before further paid calls")


if __name__ == "__main__":
    main()
