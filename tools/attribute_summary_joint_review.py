"""Prepare and score source-level reviews of frozen joint extraction outputs.

This tool never calls a model. A schema-valid result is only a review input;
every entity and required semantic check starts unreviewed and fails closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from tools.attribute_summary_joint_probe import summary_targets
from tools.attribute_prompt_probe import typed_nodes
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.text_utils import MAX_SUMMARY_CHARS, truncate_at_sentence


REVIEWED = {"pass", "fail"}
NOT_APPLICABLE = "not_applicable"


def _check(required: bool) -> dict:
    return {"verdict": "unreviewed" if required else NOT_APPLICABLE,
            "source_excerpt": "", "candidate_excerpt": "", "reason": ""}


def _packet_hash(row: dict) -> str:
    """Seal source and candidate fields while leaving review decisions editable."""
    immutable = {key: row[key] for key in (
        "sd", "sid", "operation_id", "episode_content", "previous_episodes")}
    immutable["entities"] = [{key: entity[key] for key in (
        "uuid", "name", "labels", "old_attributes", "candidate_attributes",
        "old_summary", "candidate_summary", "candidate_raw_summary")}
        for entity in row["entities"]]
    return hashlib.sha256(json.dumps(immutable, ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


def make_sample_review(sample: dict, results: list[dict], targets: set[str]) -> dict:
    typed = {node.uuid for node, _ in typed_nodes(
        [EntityNode.model_validate(item) for item in sample["nodes"]])}
    attributes, summaries, raw_summaries = {}, {}, {}
    for result in results:
        if result.get("schema_valid") is not True or result.get("sample_sid") != sample["sid"]:
            raise ValueError("missing or invalid candidate batch")
        for key, value in result["attributes"].items():
            if key in attributes:
                raise ValueError("duplicate typed entity output")
            attributes[key] = value
        if set(result["summaries"]) != set(result["persisted_summaries"]):
            raise ValueError("persisted summary target set changed")
        for key, value in result["summaries"].items():
            if key in summaries:
                raise ValueError("duplicate summary output")
            persisted = result["persisted_summaries"][key]
            if persisted != truncate_at_sentence(value, MAX_SUMMARY_CHARS):
                raise ValueError("persisted summary differs from Graphiti truncation")
            summaries[key] = persisted
            raw_summaries[key] = value
    if set(attributes) != typed or set(summaries) != targets:
        raise ValueError("candidate output does not cover exact typed and summary targets")
    entities = []
    for node in sample["nodes"]:
        uuid = node["uuid"]
        labels = node["labels"]
        is_typed, has_summary = uuid in typed, uuid in targets
        entities.append({
            "uuid": uuid, "name": node["name"], "labels": labels,
            "old_attributes": node.get("attributes", {}),
            "candidate_attributes": attributes.get(uuid),
            "old_summary": node.get("summary") or "",
            "candidate_summary": summaries.get(uuid),
            "candidate_raw_summary": raw_summaries.get(uuid),
            "checks": {
                "old_value_and_correction": _check(is_typed),
                "project_and_file_ownership": _check(is_typed and bool({"File", "Project"} & set(labels))),
                "source_grounding": _check(is_typed or has_summary),
                "summary_completeness": _check(has_summary),
            },
        })
    row = {"sd": sample["sd"], "sid": sample["sid"],
           "operation_id": sample["operation_id"],
           "episode_content": sample["episode"].get("content", ""),
           "previous_episodes": sample["previous_episodes"],
           "entities": entities,
           "reviewer": "", "reviewed_at": "", "verdict": "unreviewed"}
    row["packet_sha256"] = _packet_hash(row)
    return row


def make_cohort_review(holdout: Path, result_dir: Path, journal: Path) -> dict:
    raw = holdout.read_bytes()
    samples = json.loads(raw)["samples"]
    if len(samples) != 15:
        raise ValueError("expected 15 frozen observations")
    digest = hashlib.sha256(raw).hexdigest()
    reviews = []
    for sample in samples:
        results = []
        for batch in (0, 1):
            path = result_dir / f"{sample['sid']}-batch-{batch}-result.json"
            result = json.loads(path.read_text())
            if result.get("input_hash") != digest or result.get("batch_index") != batch:
                raise ValueError("candidate batch belongs to another frozen plan")
            results.append(result)
        reviews.append(make_sample_review(sample, results, summary_targets(journal, sample)))
    return {"holdout_sha256": digest, "sample_count": 15, "reviews": reviews}


def score_cohort(review: dict) -> dict:
    if review.get("sample_count") != 15 or len(review.get("reviews", [])) != 15:
        raise ValueError("cohort is incomplete")
    if len({row["sid"] for row in review["reviews"]}) != 15:
        raise ValueError("cohort contains duplicate observation identities")
    passed = 0
    details = []
    for row in review["reviews"]:
        if row.get("packet_sha256") != _packet_hash(row):
            raise ValueError(f"observation {row['sid']} source or candidate packet changed")
        if not row.get("reviewer") or not row.get("reviewed_at"):
            raise ValueError(f"observation {row['sid']} lacks reviewer or time")
        failed = []
        for entity in row["entities"]:
            is_typed = entity["candidate_attributes"] is not None
            has_summary = entity["candidate_summary"] is not None
            required = {
                "old_value_and_correction": is_typed,
                "project_and_file_ownership": is_typed and bool(
                    {"File", "Project"} & set(entity["labels"])),
                "source_grounding": is_typed or has_summary,
                "summary_completeness": has_summary,
            }
            if set(entity["checks"]) != set(required):
                raise ValueError("entity semantic checklist is incomplete")
            for name, check in entity["checks"].items():
                verdict = check["verdict"]
                allowed = REVIEWED if required[name] else {NOT_APPLICABLE}
                if verdict not in allowed:
                    raise ValueError(f"observation {row['sid']} has invalid or unreviewed {name}")
                if verdict == "fail":
                    if not check.get("reason") or not check.get("candidate_excerpt"):
                        raise ValueError("failed check requires reason and candidate excerpt")
                    failed.append({"uuid": entity["uuid"], "check": name,
                                   "source_excerpt": check.get("source_excerpt", ""),
                                   "candidate_excerpt": check["candidate_excerpt"],
                                   "reason": check["reason"]})
        expected = "fail" if failed else "pass"
        if row.get("verdict") != expected:
            raise ValueError(f"observation {row['sid']} verdict disagrees with entity checks")
        passed += expected == "pass"
        details.append({"sid": row["sid"], "verdict": expected, "findings": failed})
    return {"holdout_sha256": review["holdout_sha256"], "passed": passed,
            "total": 15, "pass_rate": passed / 15, "gate_90_percent": passed >= 14,
            "observations": details}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--holdout", type=Path, required=True)
    prepare.add_argument("--result-dir", type=Path, required=True)
    prepare.add_argument("--journal", type=Path, default=Path("/backup/model-attempts.sqlite3"))
    prepare.add_argument("--output", type=Path, required=True)
    score = sub.add_parser("score")
    score.add_argument("--review", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        payload = make_cohort_review(args.holdout, args.result_dir, args.journal)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"output": str(args.output), "sample_count": 15,
                          "holdout_sha256": payload["holdout_sha256"]}))
    else:
        print(json.dumps(score_cohort(json.loads(args.review.read_text())),
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
