"""Frozen benchmark references and native-run evidence (no model or graph calls).

Hashes are integrity receipts for controlled local artifacts, not signatures.
Legacy c8 data remains readable; it is never relabelled as a newer ruleset.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

RUN_EVIDENCE = "bench-native-run-v1"
RUN_EVIDENCE_V2 = "bench-native-run-v2"
REFERENCE_VERSION = "bench-reference-v1"
CORPUS_FILES = ("archived.json", "edges.jsonl", "vec-minilm.npy")


from llm_eval.evidence import digest


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def need(ok: bool, message: str):
    if not ok:
        raise SystemExit("bench_reference_precondition: " + message)


def read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("bench_reference_precondition: unreadable artifact " + path.name) from exc


def write(path: Path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=1, allow_nan=False)


def rules_version(text: str) -> str:
    match = re.search(r"prompt_version\s*=\s*(agent-v\d+)", text[:500])
    need(match is not None, "rules snapshot has no version")
    return match.group(1)


def part_name(value: str) -> str:
    need(isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value)), "unsafe part ID")
    return value


def result_items(d: Path) -> dict[int, dict]:
    """Strict native results: duplicate IDs must not disappear through dict overwrite."""
    out = {}
    for f in sorted(d.glob("results-*.json")):
        rows = read(f).get("items")
        need(isinstance(rows, list), "native results must contain items")
        for item in rows:
            need(isinstance(item, dict) and type(item.get("id")) is int, "invalid native result ID")
            need(item["id"] not in out, "duplicate native result ID")
            out[item["id"]] = item
    return out


def collect_inputs(p: Path, m: dict, ids: list[int]) -> dict:
    need(len(ids) == len(set(ids)) and bool(ids), "selected IDs must be nonempty and unique")
    wanted, seen, parts = set(ids), set(), []
    for part in m["parts"]:
        name = part_name(part["part"])
        batch = read(p / f"batch-{name}.json")
        selected = [it for it in batch["items"] if int(it["id"]) in wanted]
        if not selected:
            continue
        for it in selected:
            ident = int(it["id"])
            need(ident not in seen, "an observation occurs in multiple parts")
            seen.add(ident)
        parts.append({"part": part["part"], "items": selected, "lookahead": batch.get("lookahead") or []})
    need(seen == wanted, "selected observations are not all in source bench")
    return {"parts": parts}


def query_text(items: list[dict], lookahead: list) -> str:
    obs = [{"id": i["id"], "name": i.get("name"), "source_description": i.get("source_description"),
            "episode_body": i["episode_body"]} for i in items]
    return ("按系统提示里的规则抽取下面每一条观测，只输出一个 JSON 对象 {\"items\": [...]}，每条带原样的 id。\n\n"
            f"lookahead（之后观测的标题，用于规则 19）：{json.dumps(lookahead, ensure_ascii=False)}\n\n"
            f"观测：{json.dumps(obs, ensure_ascii=False)}")


def input_contract(p: Path, m: dict, ids: list[int], *, snapshot: dict | None = None) -> dict:
    """Bind only the selected inputs, preserving part/group and lookahead context."""
    from kg_hub_eval.business.schema import EDGE_TYPES, ENTITY_TYPES
    snapshot = snapshot if snapshot is not None else collect_inputs(p, m, ids)
    parts = [{"part": part["part"], "ids": [int(it["id"]) for it in part["items"]],
              "content_sha256": digest({"items": part["items"], "lookahead": part["lookahead"]})}
             for part in snapshot["parts"]]
    questions = {}
    for line in (p / "questions.tsv").read_text(encoding="utf-8").splitlines():
        ident, zh, en = line.split("\t")
        need(int(ident) not in questions, "duplicate question ID")
        questions[int(ident)] = [zh, en]
    return {"version": "bench-input-v1", "selected_ids": ids, "parts": parts,
            "questions_sha256": digest([[i, questions.get(i)] for i in ids]),
            "background_sha256": {name: sha(p / name) for name in ("background.json", "background-vec.npy")},
            "corpus_sha256": {name: sha(Path(m["corpus"]) / name) for name in CORPUS_FILES},
            "schema_sha256": digest({"entities": sorted(ENTITY_TYPES), "edges": sorted(EDGE_TYPES)})}


def seal_run(d: Path, metadata: dict) -> dict:
    files = sorted([*d.glob("results-*.json"), *d.glob("work/*.json"),
                    d / "rules.md", d / "system-prompt.txt", d / "input-contract.json", d / "input-data.json"])
    out = dict(metadata)
    v2 = metadata.get("native_evidence_version") == 2
    if v2:
        files.append(d / "preflight.json")
    out["evidence"] = {"version": RUN_EVIDENCE_V2 if v2 else RUN_EVIDENCE, "metadata_sha256": digest(metadata),
                       "files": {str(f.relative_to(d)): sha(f) for f in files}}
    return out


def verify_run(d: Path, metadata: dict, *, native: bool = True) -> dict:
    evidence = metadata.get("evidence") or {}
    need(evidence.get("version") in {RUN_EVIDENCE, RUN_EVIDENCE_V2}, "run lacks native integrity evidence; rerun with current CLI")
    v2 = evidence.get("version") == RUN_EVIDENCE_V2
    if v2:
        need(metadata.get("native_evidence_version") == 2 and metadata.get("status") == "complete" and
             not (d / "run-aborted.json").exists(), "infrastructure/canceled run is not a quality result")
    bare = {k: v for k, v in metadata.items() if k != "evidence"}
    need(digest(bare) == evidence.get("metadata_sha256"), "run metadata changed")
    if native:
        need(metadata.get("execution_mode") == "native_adapter", "reference/candidate must be a native adapter run")
    files = evidence.get("files") or {}
    need(all(name in files for name in ("rules.md", "system-prompt.txt", "input-contract.json", "input-data.json")),
         "run evidence is incomplete")
    actual = {str(f.relative_to(d)) for f in [*d.glob("results-*.json"), *d.glob("work/*.json")]}
    need(actual == {name for name in files if name.startswith(("results-", "work/"))}, "run artifact set changed")
    for name, expected in files.items():
        f = d / name
        need(not Path(name).is_absolute() and ".." not in Path(name).parts and f.is_file() and not f.is_symlink(), "unsafe/missing evidence file")
        need(sha(f) == expected, "run artifact changed: " + name)
    if v2:
        from llm_eval.executors import runtime as cli_runtime
        preflight = metadata.get("preflight") or {}
        need(preflight.get("path") == "preflight.json" and "preflight.json" in files and
             preflight.get("sha256") == sha(d / "preflight.json"), "preflight evidence missing/changed")
        probe = read(d / "preflight.json")
        need(preflight.get("cost") == probe.get("cost"), "preflight cost differs")
        if metadata.get("execution_mode") == "native_adapter":
            try:
                cli_runtime.validate_probe_integrity(probe, executor=metadata["executor"], model=metadata["model"])
            except ValueError as exc:
                need(False, str(exc))
    rule_text = (d / "rules.md").read_text(encoding="utf-8")
    need(sha(d / "rules.md") == metadata.get("prompt_sha256"), "rules snapshot hash differs from run")
    need(rules_version(rule_text) == str(metadata.get("prompt_version", "")).split("+")[0],
         "rules snapshot version differs from run")
    need(sha(d / "system-prompt.txt") == metadata.get("system_prompt_sha256"), "system prompt differs from run")
    contract = read(d / "input-contract.json")
    need(digest(contract) == metadata.get("input_contract_sha256"), "run input contract changed")
    need(contract.get("selected_ids") == metadata.get("ids"), "run IDs differ from input contract")
    snapshot = read(d / "input-data.json")
    need(digest(snapshot) == metadata.get("input_data_sha256"), "actual input snapshot changed")
    part_map = {part["part"]: part for part in snapshot["parts"]}
    need(len(part_map) == len(snapshot["parts"]) and
         [{"part": part["part"], "ids": [int(it["id"]) for it in part["items"]],
           "content_sha256": digest({"items": part["items"], "lookahead": part["lookahead"]})}
          for part in snapshot["parts"]] == contract["parts"], "actual inputs differ from input contract")
    calls = [read(d / name) for name in files if name.startswith("work/")]
    need(calls and len(calls) == metadata.get("calls"), "attempt evidence is incomplete")
    for call in calls:
        need(call.get("part") in part_map, "attempt part missing from frozen inputs")
        part = part_map[call["part"]]
        ids = call["ids"]
        subset = [item for item in part["items"] if int(item["id"]) in set(ids)]
        need([int(item["id"]) for item in subset] == ids, "attempt IDs/order differ from frozen part")
        need(hashlib.sha256(query_text(subset, part["lookahead"]).encode()).hexdigest() == call.get("query_sha256"),
             "actual attempt query differs from frozen inputs")
    results = result_items(d)
    need(digest(results) == metadata.get("result_items_sha256"), "native result contents differ from run metadata")
    need(set(results).issubset(set(metadata["ids"])), "native results contain foreign observations")
    if v2:
        need(set(metadata.get("failed", [])) == set(metadata["ids"]) - set(results), "missing output IDs differ")
        for call in calls:
            need(call.get("native_evidence_version") == 2 and call.get("outcome") in
                 {"generated", "output_error", "service_error"}, "attempt outcome missing")
            if call["outcome"] == "service_error":
                classified = cli_runtime.classify_failure({"error": call.get("response_error"),
                                                            "status_code": call.get("status_code")})
                need(classified["kind"] in cli_runtime.TRANSIENT_KINDS and
                     classified["kind"] == call.get("error_kind"), "unrecognized/terminal service error in completed run")
            else:
                need(not call.get("response_error"), "generated attempt contains a provider error")
    if metadata.get("executor") == "claude":
        seen = sorted({model for call in calls for model in (call.get("actual_models") or [])})
        need(seen == metadata.get("actual_models"), "Claude identity metadata differs from call evidence")
        need(all(call.get("actual_models") == [metadata["model"]] or
                 (v2 and call.get("outcome") == "service_error" and not call.get("actual_models"))
                 for call in calls), "Claude actual model evidence missing/mismatched")
    return contract


def selected_ids(rows: list, m: dict, limit, legacy_select) -> list[int]:
    need(limit is None or type(limit) is int and limit > 0, "limit must be a positive observation count")
    order = m.get("selection_order")
    if order is None:
        return legacy_select(rows, limit)
    sample = [row[0] for row in rows]
    need(len(order) == len(set(order)) and set(order) == set(sample), "frozen selection order does not cover sample")
    return order[:limit] if limit else list(order)


def baseline_label(m: dict) -> str:
    return "基线合计" if m.get("reference_version") else "基线：三模型各自那部分"


def comparison_label(part: dict) -> str:
    """Keep original sample strata even when all outputs share one reference model."""
    if "comparison_stratum" in part:
        return f"{part['baseline_model']}参考｜原样本组{part['comparison_stratum']}"
    return part["baseline_model"]


def verify_reference(p: Path, m: dict) -> dict | None:
    if "reference_version" not in m:
        return None
    need(m["reference_version"] == REFERENCE_VERSION, "unknown reference version")
    seal = m.get("integrity") or {}
    bare = {k: v for k, v in m.items() if k != "integrity"}
    need(digest(bare) == seal.get("manifest_sha256"), "reference manifest changed")
    saved = read(p / "reference-run.json")
    expected_files = {"questions.tsv", "background.json", "background-vec.npy", "rules.md", "reference-run.json",
                      "reference-input-contract.json"}
    expected_files |= {f"{prefix}-{part['part']}.json" for prefix in ("batch", "baseline") for part in m["parts"]}
    expected_files |= {"reference-native/" + name for name in (saved.get("evidence") or {}).get("files", {})}
    files = seal.get("files") or {}
    need(set(files) == expected_files, "reference evidence file list is incomplete")
    for name, expected in files.items():
        need(sha(p / name) == expected, "reference artifact changed: " + name)
    ref = m.get("reference") or {}
    contract = input_contract(p, m, m["selection_order"])
    need(contract == read(p / "reference-input-contract.json"), "frozen reference input no longer matches source")
    need(digest(contract) == ref.get("input_contract_sha256"), "reference input digest mismatch")
    need(ref.get("run_metadata_sha256") == digest(saved), "reference run metadata changed")
    verify_run(p / "reference-native", saved)
    need(digest(collect_inputs(p, m, m["selection_order"])) == saved.get("input_data_sha256"),
         "reference observations differ from actual native input snapshot")
    need((saved.get("evidence") or {}).get("version") in {RUN_EVIDENCE, RUN_EVIDENCE_V2} and
         saved["evidence"].get("metadata_sha256") == digest({k: v for k, v in saved.items() if k != "evidence"}),
         "reference native metadata seal is invalid")
    need(ref.get("prompt_version") == saved.get("prompt_version") and
         ref.get("prompt_sha256") == saved.get("prompt_sha256") == sha(p / "rules.md"), "reference rules changed")
    need(rules_version((p / "rules.md").read_text()) == ref["prompt_version"].split("+")[0], "reference version relabelled")
    need(saved.get("executor") == "claude" and saved.get("actual_models") == [saved.get("model")],
         "reference has no verified Claude model identity")
    need(ref.get("model") == saved.get("model") and
         all(part["baseline_model"] == ref["model"] for part in m["parts"]), "reference model relabelled")
    need({part["part"]: part.get("comparison_stratum") for part in m["parts"]} == saved.get("comparison_strata"),
         "reference sample comparison strata changed")
    results = {}
    for part in m["parts"]:
        baseline = read(p / f"baseline-{part_name(part['part'])}.json")
        need(baseline.get("model") == ref["model"] and baseline.get("prompt_version") == ref["prompt_version"] and
             baseline.get("prompt_sha256") == ref["prompt_sha256"], "reference part relabelled")
        for item in baseline["items"]:
            need(item["id"] not in results, "duplicate reference result")
            results[item["id"]] = item
    need(set(results) == set(m["selection_order"]) and digest(results) == saved.get("result_items_sha256"),
         "reference results differ from sealed native output")
    return ref


def freeze_reference(p: Path, m: dict, run_dir: Path, bench_id: str, root: Path, *, current_version: str,
                     current_sha: str, current_system_sha: str, expected_ids: list[int]) -> Path:
    need(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", bench_id)), "invalid new bench ID")
    out = root / bench_id
    need(not out.exists(), "frozen reference already exists; choose a fresh bench ID")
    run = read(run_dir / "run.json")
    contract = verify_run(run_dir, run)
    need(run.get("bench") == m["bench_id"], "reference run belongs to another bench")
    need(not run.get("failed") and len(run.get("ids", [])) >= 30, "reference requires at least 30 complete observations")
    need(run.get("prompt_version") == current_version and run.get("prompt_sha256") == current_sha,
         "reference run is not from the current rules version/hash")
    need(run.get("system_prompt_sha256") == current_system_sha, "reference system prompt is not current")
    need(run.get("executor") == "claude" and run.get("actual_models") == [run.get("model")],
         "reference requires verified native Claude model identity")
    need(contract == input_contract(p, m, run["ids"]), "reference source inputs changed after execution")
    need(run["ids"] == expected_ids, "reference selection order is not the official source selection")
    results = result_items(run_dir)
    need(set(results) == set(run["ids"]), "reference results have missing or extra observations")
    wanted, parts = set(run["ids"]), []
    # Build after every precondition has passed; never overwrite old samples/runs.
    # Build off to the side. The final directory is reserved exclusively and its
    # manifest is installed last; failed construction cannot look like a bench.
    root.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="." + bench_id + "-", dir=root)
    built = Path(temporary.name)
    for part in m["parts"]:
        batch = read(p / f"batch-{part['part']}.json")
        items = [it for it in batch["items"] if int(it["id"]) in wanted]
        if not items:
            continue
        batch["items"] = items
        write(built / f"batch-{part['part']}.json", batch)
        write(built / f"baseline-{part['part']}.json", {"model": run["model"], "prompt_version": run["prompt_version"],
              "prompt_sha256": run["prompt_sha256"], "items": [results[int(it["id"])] for it in items]})
        parts.append({"part": part["part"], "baseline_model": run["model"], "items": len(items),
                      "comparison_stratum": part.get("comparison_stratum", part["baseline_model"])})
    for name in ("background.json", "background-vec.npy"):
        shutil.copyfile(p / name, built / name)
    # Keep the question file byte-for-byte: unused question rows do not enter retrieval.
    shutil.copyfile(p / "questions.tsv", built / "questions.tsv")
    shutil.copyfile(run_dir / "rules.md", built / "rules.md")
    write(built / "reference-run.json", run)
    write(built / "reference-input-contract.json", contract)
    for name in run["evidence"]["files"]:
        copied = built / "reference-native" / name
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run_dir / name, copied)
    manifest = {**m, "bench_id": bench_id, "reference_version": REFERENCE_VERSION, "parts": parts,
                "items": len(run["ids"]), "selection_order": run["ids"], "chain": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "questions": sum(1 for line in (p / "questions.tsv").read_text().splitlines()
                                 if int(line.split("\t", 1)[0]) in wanted),
                "baseline_note": "Native Claude reference generated under the recorded rules; legacy data remains unchanged",
                "reference": {"model": run["model"], "prompt_version": run["prompt_version"],
                    "prompt_sha256": run["prompt_sha256"], "input_contract_sha256": digest(contract),
                    "run_metadata_sha256": digest(run), "source_bench_manifest_sha256": sha(p / "manifest.json"),
                    "source_bench": str(p.resolve()), "source_run": str(run_dir.resolve())}}
    manifest.pop("integrity", None)
    files = [f for f in built.rglob("*") if f.is_file()]
    manifest["integrity"] = {"manifest_sha256": digest(manifest), "files": {str(f.relative_to(built)): sha(f) for f in files}}
    write(built / "manifest.json", manifest)
    verify_reference(built, manifest)
    out.mkdir(mode=0o700)
    for f in files:
        target = out / f.relative_to(built)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)
    shutil.copyfile(built / "manifest.json", out / "manifest.json")
    temporary.cleanup()
    return out
