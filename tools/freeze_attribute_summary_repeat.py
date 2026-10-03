"""Freeze an independent, summary-positive 15-observation holdout.

Run inside the server container against its read-only attempt journal. This
script only writes the new holdout file and never invokes a model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from graphiti_core.nodes import EntityNode
from graphiti_core.utils.text_utils import MAX_SUMMARY_CHARS
from schema import ENTITY_TYPES


def _artifact(db, sd, sid, op, stage):
    row = db.execute(
        "SELECT artifact_json FROM graphiti_stage_artifacts "
        "WHERE task_sd=? AND task_sid=? AND operation_id=? AND stage=?",
        (sd, sid, op, stage),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _target_count(db, sample):
    sd, sid, op = sample["sd"], sample["sid"], sample["operation_id"]
    selected = _artifact(db, sd, sid, op, "parallel_selected_round")
    if selected is None:
        return 0
    edge = _artifact(db, sd, sid, op + f":graph-round:{selected['round']}", "edge_phase")
    if not edge or not isinstance(edge.get("groups"), list) or len(edge["groups"]) != 3:
        return 0
    facts = {}
    for item in edge["groups"][2]:
        for uuid in (item["source_node_uuid"], item["target_node_uuid"]):
            facts.setdefault(uuid, []).append(item.get("fact") or "")
    count = 0
    for node in sample["nodes"]:
        updated = "\n".join(filter(None, [node.get("summary") or "", *facts.get(node["uuid"], [])]))
        if not updated or len(updated) > 2 * MAX_SUMMARY_CHARS:
            count += 1
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=Path("/backup/model-attempts.sqlite3"))
    parser.add_argument("--holdout-dir", type=Path, default=Path("/backup/attribute-probe"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before", default="2026-10-03T00:00:00")
    args = parser.parse_args(argv)
    excluded = set()
    source_hashes = {}
    for path in sorted(args.holdout_dir.glob("*holdout*.json")):
        raw = path.read_bytes()
        source_hashes[path.name] = hashlib.sha256(raw).hexdigest()
        holder = json.loads(raw)
        for sample in holder.get("samples", []) if isinstance(holder, dict) else holder:
            if isinstance(sample, dict):
                excluded.add((sample["sd"], sample["sid"]))
            elif isinstance(sample, list) and len(sample) >= 2:
                excluded.add((sample[0], sample[1]))
    samples = []
    with sqlite3.connect(args.journal.resolve().as_uri() + "?mode=ro", uri=True) as db:
        max_rowid = db.execute("SELECT max(rowid) FROM graphiti_stage_artifacts").fetchone()[0]
        rows = db.execute(
            "SELECT task_sd,task_sid,operation_id,artifact_json "
            "FROM graphiti_stage_artifacts WHERE rowid>? AND stage='operation_envelope' "
            "ORDER BY rowid DESC LIMIT 1000", (max_rowid - 20000,),
        )
        for sd, sid, op, raw in rows:
            if (sd, sid) in excluded or (sd, sid) in {(x["sd"], x["sid"]) for x in samples}:
                continue
            envelope = json.loads(raw)
            if envelope.get("now", "") >= args.before:
                continue
            if _artifact(db, sd, sid, op, "graph_commit_receipt") is None:
                continue
            selected = _artifact(db, sd, sid, op, "parallel_selected_round")
            if selected is None:
                continue
            round_op = op + f":graph-round:{selected['round']}"
            resolved = _artifact(db, sd, sid, round_op, "resolved_nodes")
            prepared = _artifact(db, sd, sid, round_op, "prepared_commit")
            if not resolved or not prepared:
                continue
            nodes = [EntityNode.model_validate(node) for node in resolved["nodes"]]
            typed = [node for node in nodes if any(
                label in ENTITY_TYPES and ENTITY_TYPES[label].model_fields
                for label in node.labels if label != "Entity")]
            if not 9 <= len(typed) <= 16 or len({node.group_id for node in nodes}) != 1:
                continue
            if len({node.uuid for node in typed}) != len(typed):
                continue
            sample = {"sd": sd, "sid": sid, "operation_id": op,
                      "nodes": resolved["nodes"], "episode": envelope["episode"],
                      "previous_episodes": envelope["previous_episodes"],
                      "typed_count": len(typed)}
            if len(json.dumps(sample, ensure_ascii=False)) > 150000:
                continue
            if _target_count(db, sample) == 0:
                continue
            samples.append(sample)
            if len(samples) == 15:
                break
    if len(samples) != 15:
        raise RuntimeError(f"only {len(samples)} eligible independent observations")
    payload = {"selection": {"before": args.before, "journal_max_rowid": max_rowid,
                             "excluded_holdout_sha256": source_hashes,
                             "rule": "committed final round, 9-16 typed nodes, at least one summary target"},
               "samples": samples}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"path": str(args.output), "sha256": hashlib.sha256(encoded).hexdigest(),
                      "count": len(samples), "typed_counts": [s["typed_count"] for s in samples]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
