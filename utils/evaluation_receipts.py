"""Local evaluation receipts. Integrity evidence only; never an authorization service.

This module uses only the standard library. It neither calls models nor loads the
embedding stack, so frozen-batch consumers can inspect reports offline.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from utils import bench_reference as reference


def need(ok, message):
    if not ok:
        raise ValueError("evaluation_receipt: " + message)


def digest(value):
    return reference.digest(value)


def read_json(path):
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def artifact(path):
    path = Path(path).expanduser().resolve()
    need(path.is_file(), "missing artifact " + str(path))
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": reference.sha(path)}


def verify_artifact(ref):
    current = artifact(ref["path"])
    need(current == ref, "artifact changed: " + ref["path"])


def create_scope(spec):
    """Freeze the material and requested scope, without inventing human approval."""
    for name in ("destination", "purpose", "data_classes", "records", "artifacts"):
        need(spec.get(name) is not None, "scope missing " + name)
    need(isinstance(spec["destination"], dict) and bool(spec["destination"]), "destination must identify provider/repository")
    need(type(spec["records"]) is int and spec["records"] >= 0, "records must be a nonnegative integer")
    need(isinstance(spec["data_classes"], list) and bool(spec["data_classes"]), "data classes required")
    need(bool(spec["artifacts"]), "scope requires concrete material")
    body = {"version": "evaluation-outbound-scope-v1", "destination": spec["destination"],
            "purpose": spec["purpose"], "data_classes": spec["data_classes"], "records": spec["records"],
            "anonymization": spec.get("anonymization", "model_identity_only"),
            "artifacts": [artifact(p) for p in spec["artifacts"]],
            "followup_scope": spec.get("followup_scope", []),
            "human_message_refs": spec.get("human_message_refs", []),
            "approval_status": "pending", "permission_granted": False,
            "authorization_note": "Evidence only. The coordinator must retain actual human authorization and obey platform approval review."}
    return {**body, "scope_sha256": digest(body)}


def verify_scope(scope):
    need(scope.get("version") == "evaluation-outbound-scope-v1", "unsupported scope")
    need(scope.get("approval_status") == "pending" and scope.get("permission_granted") is False,
         "scope evidence cannot grant permission")
    need(scope.get("scope_sha256") == digest({k: v for k, v in scope.items() if k != "scope_sha256"}), "scope fingerprint changed")
    for ref in scope["artifacts"]:
        verify_artifact(ref)
    destination_known = scope["destination"].get("status") != "unspecified" and any(
        scope["destination"].get(k) for k in ("provider", "service", "repository", "repo"))
    return {"integrity": "valid", "readiness": "scope_described" if destination_known else "action_required_destination",
            "approval_status": "pending", "permission_granted": False,
            "scope_sha256": scope["scope_sha256"]}


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def cost_component(source, kind, data, *, unstarted=False):
    """Top-level provider totals already include cached/reasoning token subsets."""
    values = {}
    for metric in ("calls", "tokens", "credits", "ms"):
        value = data.get(metric)
        known = data.get(metric + "_known", False if metric in ("tokens", "credits") else value is not None)
        values[metric] = value if known and _number(value) else None
    return {"source": source, "kind": kind, "unstarted": unstarted, **values}


def cost_totals(components):
    out = {}
    for metric in ("calls", "tokens", "credits", "ms"):
        known = [c[metric] for c in components if c.get(metric) is not None]
        out[metric] = {"value": sum(known) if known else None, "known_components": len(known),
                       "total_components": len(components), "complete": bool(components) and len(known) == len(components)}
    return out


def overhead_coverage(rows):
    coverage = {}
    for key in ("tokens", "credits", "seconds"):
        values = [r[key] for r in rows if _number(r.get(key))]
        coverage[key] = {"value": sum(values) if values else None, "known_entries": len(values),
                         "total_entries": len(rows), "complete": bool(rows) and len(values) == len(rows)}
    return coverage


def _record(path, run, run_ref):
    data = read_json(path)
    need(isinstance(data, dict), "receipt must be an object")
    if "history" in data:
        matches = [x for x in data["history"] if x.get("run_dir") and Path(x["run_dir"]).resolve() == Path(run_ref["path"]).parent]
        need(len(matches) == 1, "select an unambiguous standalone admission record")
        data = matches[0]
    need(data.get("model") == run.get("model"), "receipt model differs")
    if data.get("run_dir"):
        need(Path(data["run_dir"]).resolve() == Path(run_ref["path"]).parent, "receipt run differs")
    if data.get("run_sha256"):
        need(data["run_sha256"] == run_ref["sha256"], "receipt run hash differs")
    if data.get("prompt_sha256"):
        need(data["prompt_sha256"] == run.get("prompt_sha256"), "receipt prompt differs")
    return data


def _time(value, label):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("evaluation_receipt: invalid " + label) from exc
    need(parsed.tzinfo is not None, label + " requires timezone")
    return parsed


def _semantic_evidence(spec, run, run_ref, refs):
    """Validate historical judgments against their frozen suite, never active policy."""
    from utils import semantic_regression as semantic
    need(bool(spec.get("suite")), "frozen semantic suite artifact required")
    suite_ref = artifact(spec["suite"])
    refs.append(suite_ref)
    suite = read_json(suite_ref["path"])
    need(isinstance(suite, dict) and isinstance(suite.get("cases"), list) and
         all(isinstance(c, dict) and isinstance(c.get("criteria"), list) and
             all(isinstance(k, dict) for k in c["criteria"]) for c in suite["cases"]), "malformed frozen semantic suite")
    semantic.validate_suite(suite)
    need(suite.get("scope") in ("synthetic-test", "private-production") and
         all(suite.get(k) for k in ("suite_id", "revision", "selection_rationale")), "incomplete frozen suite declaration")
    names = [c.get("case_id") for c in suite["cases"]]
    need(all(isinstance(n, str) and n.strip() for n in names) and len(set(names)) == len(names), "duplicate or missing frozen suite case")
    for case in suite["cases"]:
        criteria = case["criteria"]
        keys = [c.get("criterion_id") for c in criteria]
        need(all(isinstance(k, str) and k.strip() for k in keys) and len(set(keys)) == len(keys),
             "duplicate or missing frozen criterion")
        need(type(case.get("obs_id")) is int and case.get("rationale") and
             all(isinstance(c.get("requirement"), str) and c["requirement"].strip() for c in criteria),
             "incomplete frozen case requirements")
    directory = Path(run_ref["path"]).parent
    # A metadata hash alone cannot prove that source/output artifacts form a run.
    reference.verify_run(directory, run, native=True)
    bench = Path(suite["bench_dir"])
    manifest_ref = artifact(bench / "manifest.json")
    refs.append(manifest_ref)
    manifest = read_json(manifest_ref["path"])
    for part in manifest["parts"]:
        refs.append(artifact(bench / ("batch-" + reference.part_name(part["part"]) + ".json")))
    need(read_json(directory / "input-data.json") == reference.collect_inputs(bench, manifest, run["ids"]),
         "semantic source differs from actual frozen run inputs")
    complete_run = {**run, "dir": str(directory), "results": reference.result_items(directory)}
    receipt = _record(spec["semantic"], run, run_ref)
    need(isinstance(receipt.get("review"), dict), "complete semantic review object required")
    reviewed_at = _time(receipt.get("created_at"), "semantic created_at")
    semantic.validate_receipt(bench, manifest, complete_run, suite, receipt, now=reviewed_at)
    return receipt, (semantic, bench, manifest, complete_run, suite)


def _admission_binding(admission, receipt, evidence):
    semantic, bench, manifest, run, suite = evidence
    need(admission.get("admission_version") == "admission-v2", "unsupported admission contract")
    for name in ("model", "executor", "bench", "prompt_version", "prompt_sha256"):
        need(admission.get(name) == run.get(name), "admission " + name + " differs")
    need(admission.get("run_dir") and Path(admission["run_dir"]).resolve() == Path(run["dir"]),
         "admission run differs")
    need(admission.get("items") == len(run["ids"]), "admission item coverage differs")
    need(admission.get("semantic_review") == receipt, "admission embeds a different semantic receipt")
    decided_at = _time(admission.get("decided_at"), "admission decided_at")
    semantic.validate_receipt(bench, manifest, run, suite, receipt, now=decided_at)
    regression = admission.get("semantic_regression") or {}
    need(regression.get("verdict") == receipt["verdict"] and regression.get("denominator") == "fixed-critical-suite",
         "admission semantic verdict differs")
    checks = admission.get("checks")
    need(isinstance(checks, list) and bool(checks) and all(isinstance(c, dict) and type(c.get("pass")) is bool for c in checks),
         "complete admission checks required")
    semantic_checks = [c for c in checks if c.get("check") == "独立语义回归"]
    need(len(semantic_checks) == 1 and semantic_checks[0]["pass"] == (receipt["verdict"] == "pass") and
         semantic_checks[0].get("value") == receipt["verdict"], "admission semantic check differs")
    verdict = admission.get("verdict")
    need(verdict in ("fail", "screen_pass", "pending_blind", "semantic_regression_fail", "pass"),
         "admission verdict incomplete")
    if receipt["verdict"] == "fail":
        need(verdict == "semantic_regression_fail", "failed semantic review cannot become admission pass")
    else:
        need(verdict != "semantic_regression_fail", "admission semantic failure differs from review")
        if verdict in ("pass", "screen_pass", "pending_blind"):
            need(all(c["pass"] for c in checks), "admission verdict contradicts failed checks")
        elif verdict == "fail":
            need(any(not c["pass"] for c in checks), "admission fail lacks a failed check")
    return True


def _entry(spec):
    phase = spec.get("phase")
    need(isinstance(phase, str) and bool(phase), "entry phase required")
    refs, costs = [], []
    item = {"phase": phase, "model": spec.get("model"), "tag": spec.get("tag"),
            "status": "planned", "ready": False, "artifacts": refs,
            "sources": {k: str(Path(spec[k]).expanduser().resolve()) for k in ("score", "semantic", "suite", "admission") if spec.get(k)}}
    if not spec.get("run"):
        return item, costs
    directory = Path(spec["run"]).expanduser().resolve()
    item["run_dir"] = str(directory)
    state_path = directory / "execution-state.json"
    if state_path.exists():
        refs.append(artifact(state_path))
        state = read_json(state_path)
        item["execution"] = {k: state.get(k) for k in ("version", "state", "updated_at", "pid", "queued_not_started", "inflight_finished") if k in state}
        item["status"] = state.get("state", "unknown")
    run_path = directory / "run.json"
    aborted_path = directory / "run-aborted.json"
    if not run_path.exists():
        if aborted_path.exists():
            ref = artifact(aborted_path); refs.append(ref)
            aborted = read_json(aborted_path)
            item.update(status="infrastructure_failed", failure=aborted.get("failure"),
                        model=aborted.get("model", item["model"]), tag=aborted.get("tag", item["tag"]),
                        queued_not_started=aborted.get("queued_not_started"), inflight_finished=aborted.get("inflight_finished"))
            costs.append(cost_component(ref["sha256"], "extraction", aborted.get("extraction_cost") or {}))
            if aborted.get("preflight"):
                probe = aborted["preflight"]
                if probe.get("path"):
                    pref = artifact(directory / probe["path"])
                    need(pref["sha256"] == probe.get("sha256"), "aborted preflight changed")
                    refs.append(pref)
                costs.append(cost_component(probe.get("sha256") or ref["sha256"], "preflight", probe.get("cost") or {},
                                            unstarted=probe.get("status") == "not_started"))
        return item, costs
    run = read_json(run_path); run_ref = artifact(run_path); refs.append(run_ref)
    need(not spec.get("model") or spec["model"] == run.get("model"), "entry model differs from run")
    need(not spec.get("tag") or spec["tag"] == run.get("tag"), "entry tag differs from run")
    item.update(model=run.get("model"), tag=run.get("tag"), executor=run.get("executor"),
                prompt_version=run.get("prompt_version"), prompt_sha256=run.get("prompt_sha256"),
                system_prompt_sha256=run.get("system_prompt_sha256"), ids_sha256=digest(run.get("ids")),
                items=len(run.get("ids") or []), run_sha256=run_ref["sha256"], status="historical_unverified",
                started_at=run.get("started_at"), ended_at=run.get("ended_at"))
    sealed = bool(run.get("evidence"))
    if sealed:
        reference.verify_run(directory, run, native=True)
        for rel in run["evidence"]["files"]:
            refs.append(artifact(directory / rel))
        item["status"] = "complete"
    if item.get("execution", {}).get("state") == "complete":
        state = read_json(state_path)
        need(state.get("run_sha256") == run_ref["sha256"], "terminal state does not bind run")
    costs.append(cost_component(run_ref["sha256"], "extraction", run))
    if run.get("preflight"):
        probe = run["preflight"]
        if probe.get("path"):
            pref = artifact(directory / probe["path"])
            need(pref["sha256"] == probe.get("sha256"), "preflight changed")
            refs.append(pref)
        costs.append(cost_component(probe.get("sha256") or run_ref["sha256"], "preflight", probe.get("cost") or {}))
    score_bound = False
    if spec.get("score"):
        ref = artifact(spec["score"]); refs.append(ref)
        score = read_json(spec["score"])
        candidates = [r for r in score.get("runs", []) if all(r.get(k) == run.get(k) for k in ("model", "executor", "tag", "prompt_version"))]
        need(len(candidates) == 1, "score run identity ambiguous or different")
        source = candidates[0]
        if source.get("run_sha256"):
            need(source["run_sha256"] == run_ref["sha256"], "score binds different run")
            need(source.get("ids_sha256") == item["ids_sha256"], "score IDs differ")
            need(source.get("prompt_sha256") == run.get("prompt_sha256"), "score prompt differs")
            score_bound = True
        full = [r for r in score.get("rows", []) if r.get("label") == run.get("label", "") + "（全样本）"]
        need(len(full) == 1, "score must contain one full-sample candidate row")
        need(full[0].get("items") == item["items"], "score does not cover the full run")
        item["scores"] = full[0]
        item["score_binding"] = "run_sha256" if score_bound else "legacy_identity_only"
    semantic_bound = admission_bound = False
    if spec.get("semantic"):
        ref = artifact(spec["semantic"]); refs.append(ref)
        semantic, semantic_evidence = _semantic_evidence(spec, run, run_ref, refs)
        item["semantic"] = {k: semantic.get(k) for k in ("verdict", "reviewer", "suite_sha256", "context_sha256", "review_sha256", "created_at", "expires_at", "receipt_sha256")}
        item["semantic"]["historical_validity"] = "valid_at_review"
        semantic_bound = True
    if spec.get("admission"):
        ref = artifact(spec["admission"]); refs.append(ref)
        admission = _record(spec["admission"], run, run_ref)
        embedded = admission.get("semantic_review") or {}
        item["admission"] = {k: admission.get(k) for k in ("admission_version", "verdict", "checks", "blind", "decided_at", "semantic_regression")}
        item["admission"]["semantic_receipt_sha256"] = embedded.get("receipt_sha256")
        if admission.get("admission_version") == "admission-v2":
            need(semantic_bound, "admission-v2 requires complete independent semantic evidence")
            admission_bound = _admission_binding(admission, semantic, semantic_evidence)
            item["semantic"]["historical_validity"] = "valid_at_review_and_admission"
    requirements = (score_bound if phase in ("smoke", "admission", "formal") else True)
    if phase in ("admission", "formal"):
        requirements = requirements and semantic_bound and admission_bound
    item["ready"] = sealed and bool(requirements)
    if sealed and not requirements:
        item["status"] = "pending_evaluation"
    return item, costs


def build_index(spec):
    entries, components, seen_entries, seen_cost = [], [], set(), set()
    for declaration in spec.get("entries", []):
        row, costs = _entry(declaration)
        key = (row["phase"], row.get("model"), row.get("tag"), row.get("run_dir"))
        need(key not in seen_entries, "duplicate phase/model/tag/run entry")
        seen_entries.add(key)
        entries.append(row)
        for cost in costs:
            identity = (cost["source"], cost["kind"])
            if identity not in seen_cost:
                seen_cost.add(identity); components.append(cost)
    overhead = []
    for path in spec.get("overhead", []):
        ref = artifact(path)
        if ref in [x["artifact"] for x in overhead]:
            continue
        data = read_json(path)
        rows = data if isinstance(data, list) else data.get("overhead", [data])
        overhead.append({"artifact": ref, "coverage": overhead_coverage(rows)})
    body = {"version": "evaluation-index-v1", "entries": entries,
            "ready": bool(entries) and all(e["ready"] for e in entries),
            "cost": {"components": components, "totals": cost_totals(components),
                     "overhead": overhead, "overhead_measured": any(
                         c["known_entries"] for entry in overhead for c in entry["coverage"].values())}}
    return {**body, "index_sha256": digest(body)}


def verify_index(index):
    need(index.get("version") == "evaluation-index-v1", "unsupported index")
    need(index.get("index_sha256") == digest({k: v for k, v in index.items() if k != "index_sha256"}), "index changed")
    for row in index["entries"]:
        for ref in row["artifacts"]:
            verify_artifact(ref)
    for overhead in index["cost"]["overhead"]:
        verify_artifact(overhead["artifact"])
    declarations = [{"phase": row["phase"], "model": row.get("model"), "tag": row.get("tag"),
                     **({"run": row["run_dir"]} if row.get("run_dir") else {}), **row.get("sources", {})}
                    for row in index["entries"]]
    rebuilt = build_index({"entries": declarations, "overhead": [o["artifact"]["path"] for o in index["cost"]["overhead"]]})
    need(rebuilt == index, "indexed summary differs from its complete source evidence")


def current_semantic_validity(index, *, now=None):
    """A current expiry view does not change the historical evaluation verdict."""
    at = now or datetime.now(timezone.utc)
    return [{"phase": row["phase"], "model": row.get("model"), "tag": row.get("tag"),
             "historical_validity": row["semantic"]["historical_validity"],
             "current_status": ("not_yet_reviewed" if at < _time(row["semantic"]["created_at"], "semantic created_at") else
                                "expired" if at >= _time(row["semantic"]["expires_at"], "semantic expires_at") else "within_review_ttl"),
             "expires_at": row["semantic"]["expires_at"]}
            for row in index["entries"] if row.get("semantic")]


def render_markdown(index):
    verify_index(index)
    def cell(value):
        return str(value if value is not None else "未知").replace("|", "\\|").replace("\n", " ")
    lines = ["# 模型评测回执报告", "", "状态：" + ("回执完整" if index["ready"] else "待补回执 / 未完成"),
             "", "索引 SHA256：`" + index["index_sha256"] + "`", "",
             "| 阶段 | 模型 | 标签 | 条数 | 状态 | 准入 |", "|---|---|---|---:|---|---|"]
    for e in index["entries"]:
        lines.append("| " + " | ".join(cell(v) for v in (e["phase"], e.get("model"), e.get("tag"), e.get("items"), e["status"], e.get("admission", {}).get("verdict"))) + " |")
    validity = current_semantic_validity(index)
    if validity:
        lines += ["", "语义回执历史有效性与当前有效期分列；历史完整不代表今天仍可用于新战役。", "",
                  "| 阶段 / 模型 / 标签 | 历史证据 | 当前有效期 | 到期时间 |", "|---|---|---|---|"]
        for row in validity:
            lines.append("| " + " | ".join(cell(v) for v in (
                " / ".join(str(row[k]) for k in ("phase", "model", "tag")), row["historical_validity"],
                row["current_status"], row["expires_at"])) + " |")
    columns = [("items", "条数"), ("missing", "未交"), ("missing_edges_key", "缺edges"),
               ("offschema_edge_pct", "越界边%"), ("dangling_edge_pct", "悬空边%"), ("unknown_type", "未知类型"),
               ("headline_verbatim_pct", "照抄标题%"), ("second_other_lang_pct", "译文边%"),
               ("rest_fact_en_pct", "其余英文%"), ("rest_fact_len_ok_pct", "其余长度合规%"),
               ("hit_zh_pct", "中文命中%"), ("hit_en_pct", "英文命中%"),
               ("ent_per_obs", "实体/条"), ("edge_per_obs", "边/条"), ("credits_per_obs", "credits/条")]
    scored = [e for e in index["entries"] if e.get("scores")]
    if scored:
        lines += ["", "| 阶段 / 模型 / 标签 | " + " | ".join(label for _, label in columns) + " |",
                  "|---|" + "---:|" * len(columns)]
        for e in scored:
            lines.append("| " + cell(" / ".join(str(e[k]) for k in ("phase", "model", "tag"))) + " | " +
                         " | ".join(cell(e["scores"].get(key)) for key, _ in columns) + " |")
    lines += ["", "| 成本 | 已知合计 | 已测组件 / 全部组件 |", "|---|---:|---:|"]
    for metric, cost in index["cost"]["totals"].items():
        lines.append(f"| {metric} | {cell(cost['value'])} | {cost['known_components']} / {cost['total_components']} |")
    lines += ["", "编排开销：" + ("见 JSON 中 overhead 的覆盖范围" if index["cost"]["overhead_measured"] else "未知，未提供测量回执"),
              "", "相同运行重复引用只计一次成本。缓存和思考 token 不另加到 provider 总数。", ""]
    return "\n".join(lines)
