"""Independent, fixed-case semantic reviews. Local hashes are not signatures.

Validation proves completeness and artifact binding; a named independent reviewer
supplies semantic judgments. It never infers truth from keywords or quotations.
"""
from __future__ import annotations

import copy
from llm_eval.review import validate_reviewer, validate_quotes
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kg_hub_eval.business.utils.bench_reference import collect_inputs, digest, result_items, sha, verify_run as native_verify_run

SUITE_VERSION = "semantic-regression-suite-v1"
REVIEW_VERSION = "semantic-regression-review-v1"
RECEIPT_VERSION = "semantic-review-receipt-v1"
ADMISSION_VERSION = "admission-v2"
POLICY_VERSION = "semantic-regression-policy-v1"
PENDING = "需补新版语义评审，不必重抽原始输出"


def need(ok, message):
    if not ok:
        raise ValueError("semantic_regression_precondition: " + message)


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("semantic_regression_precondition: unreadable artifact " + str(path)) from exc


def write_new(path, value):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Restrict access at creation, including while JSON is streaming and when
    # serialization fails. An incomplete artifact stays private and cannot be
    # overwritten by a retry; the operator may inspect/remove it explicitly.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def sealed(value, field):
    value = copy.deepcopy(value)
    value[field] = digest(value)
    return value


def verify_seal(value, field):
    need(isinstance(value, dict), "artifact must be an object")
    need(value.get(field) == digest({k: v for k, v in value.items() if k != field}), "changed " + field)


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _date(value):
    try:
        out = datetime.fromisoformat(value)
        need(out.tzinfo is not None, "timestamp needs timezone")
        return out
    except (TypeError, ValueError) as exc:
        raise ValueError("semantic_regression_precondition: invalid timestamp") from exc


def _sources(p, m, ids):
    source = {}
    for part in collect_inputs(Path(p), m, ids)["parts"]:
        for item in part["items"]:
            source[int(item["id"])] = {"observation": item, "lookahead": part["lookahead"]}
    return source


def create_suite(p, m, spec):
    """Register a fixed, nonempty required set. Selection is a policy declaration."""
    cases = spec.get("cases")
    need(isinstance(cases, list) and bool(cases), "suite must contain fixed required cases")
    need(spec.get("suite_id") and spec.get("revision") and spec.get("selection_rationale"),
         "suite_id, revision and selection_rationale required")
    need(spec.get("scope") in ("private-production", "synthetic-test"), "explicit suite scope required")
    ids = [c.get("obs_id") for c in cases]
    need(all(type(i) is int for i in ids), "integer observation IDs required")
    source = _sources(p, m, sorted(set(ids)))
    names = set()
    registered = []
    for case in cases:
        name = case.get("case_id")
        need(isinstance(name, str) and name.strip() and name not in names, "unique case_id required")
        names.add(name)
        criteria = case.get("criteria")
        need(isinstance(criteria, list) and bool(criteria), "nonempty criteria required")
        keys = [c.get("criterion_id") for c in criteria]
        need(all(isinstance(k, str) and k.strip() for k in keys) and len(set(keys)) == len(keys),
             "unique nonempty criterion IDs required")
        need(all(isinstance(c.get("requirement"), str) and c["requirement"].strip() for c in criteria),
             "each criterion requires a semantic requirement")
        need(case.get("critical") is True and bool(case.get("rationale")),
             "every registered regression case must be critical with rationale")
        origin = {}
        if spec["scope"] == "private-production":
            need(bool(case.get("known_failure_run")), "production case requires its original known-failure run")
        if case.get("known_failure_run"):
            old_dir = Path(case["known_failure_run"]).expanduser().resolve()
            old = read(old_dir / "run.json")
            old_results = result_items(old_dir)
            need(old.get("bench") == m["bench_id"] and case["obs_id"] in old.get("ids", []) and
                 case["obs_id"] in old_results, "known failure must anchor this bench and observation")
            need(old.get("status", "complete") == "complete" and not old.get("aborted"), "known failure run must be complete")
            if spec["scope"] == "private-production":
                # Historical v1 native evidence is valid. A path or a manually
                # registered legacy result cannot substitute for that evidence.
                native_verify_run(old_dir, old)
                snapshot = read(old_dir / "input-data.json")
                original = []
                for part in snapshot.get("parts", []):
                    for item in part.get("items", []):
                        if item.get("id") == case["obs_id"]:
                            original.append({"observation": item, "lookahead": part.get("lookahead") or []})
                need(len(original) == 1 and original[0] == source[case["obs_id"]],
                     "known failure original observation/lookahead context differs from suite bench")
            origin = {"known_failure_run_sha256": sha(old_dir / "run.json"),
                      "known_failure_output_sha256": digest(old_results[case["obs_id"]]),
                      "known_failure_model": old.get("model")}
        registered.append({"case_id": name, "obs_id": case["obs_id"], "critical": True,
                           "rationale": case["rationale"], "criteria": criteria,
                           "source_sha256": digest(source[case["obs_id"]]), **origin})
    return sealed({"version": SUITE_VERSION, "suite_id": spec["suite_id"], "revision": spec["revision"],
                   "scope": spec["scope"], "selection_rationale": spec["selection_rationale"],
                   "bench": m["bench_id"], "bench_dir": str(Path(p).resolve()),
                   "manifest_sha256": sha(Path(p) / "manifest.json"), "cases": registered}, "suite_sha256")


def validate_suite(suite):
    verify_seal(suite, "suite_sha256")
    need(suite.get("version") == SUITE_VERSION and bool(suite.get("cases")), "invalid or empty suite")
    need(all(c.get("critical") is True and c.get("criteria") for c in suite["cases"]), "critical criteria missing")
    if suite.get("scope") == "private-production":
        need(all(c.get("known_failure_run_sha256") and c.get("known_failure_output_sha256") for c in suite["cases"]),
             "production suite lacks original known-failure evidence")


def activate(root, suite_path, *, reviewer, confirm_suite_sha):
    suite_path = Path(suite_path).expanduser().resolve()
    suite = read(suite_path)
    validate_suite(suite)
    need(confirm_suite_sha == suite["suite_sha256"], "suite confirmation digest differs")
    need(isinstance(reviewer, str) and reviewer.strip(), "operator name declaration required")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "semantic-policy.json"
    # One atomic canonical registry. A policy selection is not a semantic pass.
    import fcntl
    with (root / ".semantic-policy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        policy = read(path) if path.exists() else {"version": POLICY_VERSION, "benches": {}}
        need(policy.get("version") == POLICY_VERSION, "policy version differs")
        policy["benches"][suite["bench"]] = {"suite_path": str(suite_path), "suite_sha256": suite["suite_sha256"],
                                             "operator_declaration": reviewer, "identity_evidence": "self-declared",
                                             "activated_at": _now().isoformat()}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    return policy["benches"][suite["bench"]]


def active_suite(root, bench):
    path = Path(root) / "semantic-policy.json"
    need(path.exists(), "no canonical suite registered; " + PENDING)
    policy = read(path)
    need(policy.get("version") == POLICY_VERSION, "policy version differs")
    entry = policy.get("benches", {}).get(bench)
    need(entry is not None, "no canonical suite for bench; " + PENDING)
    suite = read(entry["suite_path"])
    validate_suite(suite)
    need(suite["suite_sha256"] == entry["suite_sha256"] and suite["bench"] == bench, "active suite changed")
    return suite


def context(p, m, run, suite):
    validate_suite(suite)
    need(suite["bench"] == m["bench_id"] and suite["manifest_sha256"] == sha(Path(p) / "manifest.json"),
         "suite belongs to different or changed frozen bench")
    need(suite["bench_dir"] == str(Path(p).resolve()), "suite bench directory differs")
    need(run.get("status", "complete") == "complete" and not run.get("aborted"), "run did not complete")
    need(run.get("bench") == m["bench_id"], "run belongs to another bench")
    d = Path(run["dir"]).resolve()
    source = _sources(p, m, sorted({c["obs_id"] for c in suite["cases"]}))
    cases = []
    for case in suite["cases"]:
        ident = case["obs_id"]
        need(ident in run["ids"] and ident in run["results"], "run lacks a required critical regression case")
        need(digest(source[ident]) == case["source_sha256"], "required source or lookahead changed")
        cases.append({**case, **source[ident], "actual_output": run["results"][ident]})
    need(bool(run.get("system_prompt_sha256")), "complete system prompt digest required")
    return {"bench": m["bench_id"], "run_dir": str(d), "run_sha256": sha(d / "run.json"),
            "model": run["model"], "executor": run.get("executor"), "ids": run["ids"],
            "prompt": {"version": run["prompt_version"], "rules_sha256": run["prompt_sha256"],
                       "system_sha256": run["system_prompt_sha256"]},
            "result_files": {f.name: sha(f) for f in sorted(d.glob("results-*.json"))},
            "results_sha256": digest(run["results"]), "suite_sha256": suite["suite_sha256"], "cases": cases}


def review_sheet(p, m, run, suite):
    ctx = context(p, m, run, suite)
    return {"version": REVIEW_VERSION, "context": ctx, "context_sha256": digest(ctx), "reviewed_at": "",
            "reviewer": {"name": "", "method": "", "identity_evidence": "self-declared",
                         "independence_declaration": ""},
            "cases": [{"case_id": c["case_id"], "judgments": [
                {"criterion_id": k["criterion_id"], "verdict": "pending", "source_quote": "",
                 "claim_pointer": "", "claim_quote": "", "responsibility_judgment": "", "explanation": ""}
                for k in c["criteria"]]} for c in ctx["cases"]]}


def _pointer(value, pointer):
    need(isinstance(pointer, str) and pointer.startswith("/"), "JSON claim pointer required")
    try:
        for part in pointer[1:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise ValueError("semantic_regression_precondition: claim pointer does not exist") from exc


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def check_review(p, m, run, suite, review, *, now=None):
    ctx = context(p, m, run, suite)
    need(review.get("version") == REVIEW_VERSION, "review version differs")
    need(review.get("context") == ctx and review.get("context_sha256") == digest(ctx), "review context changed")
    reviewer = review.get("reviewer") or {}
    at = _date(review.get("reviewed_at"))
    need(at <= _now(now) + timedelta(minutes=5) and _now(now) < at + timedelta(days=30),
         "independent review date is future-dated or expired")
    validate_reviewer(reviewer, {run["model"]})
    rows = review.get("cases")
    need(isinstance(rows, list) and len(rows) == len(ctx["cases"]), "complete case coverage required")
    need(len({r.get("case_id") for r in rows}) == len(rows), "duplicate review case")
    by = {r.get("case_id"): r for r in rows}
    failures = []
    for case in ctx["cases"]:
        row = by.get(case["case_id"])
        need(row is not None, "required critical case missing")
        judgments = row.get("judgments")
        need(isinstance(judgments, list) and len(judgments) == len(case["criteria"]), "complete criterion coverage required")
        need(len({j.get("criterion_id") for j in judgments}) == len(judgments), "duplicate criterion judgment")
        judged = {j.get("criterion_id"): j for j in judgments}
        for criterion in case["criteria"]:
            j = judged.get(criterion["criterion_id"])
            need(j is not None and j.get("verdict") in ("pass", "fail"), "explicit semantic verdict required")
            source_text = _text(case["observation"]) + "\n" + _text(case["lookahead"])
            quote = j.get("source_quote")
            actual = _text(_pointer(case["actual_output"], j.get("claim_pointer")))
            claim = j.get("claim_quote")
            validate_quotes(source_text, actual, quote, claim, minimum_source_quote=8)
            need(all(isinstance(j.get(k), str) and len(j[k].strip()) >= 8 for k in ("responsibility_judgment", "explanation")),
                 "substantive responsibility judgment and explanation required")
            if j["verdict"] == "fail":
                failures.append({"case_id": case["case_id"], "criterion_id": criterion["criterion_id"]})
    return sealed({"version": RECEIPT_VERSION, "verdict": "fail" if failures else "pass", "failures": failures,
                   **{k: ctx[k] for k in ("model", "executor", "bench", "run_dir", "run_sha256", "prompt", "suite_sha256")},
                   "context_sha256": digest(ctx), "review_sha256": digest(review), "reviewer": reviewer,
                   "review": review, "created_at": at.isoformat(), "expires_at": (at + timedelta(days=30)).isoformat()},
                  "receipt_sha256")


def validate_receipt(p, m, run, suite, receipt, *, now=None):
    verify_seal(receipt, "receipt_sha256")
    need(receipt.get("version") == RECEIPT_VERSION, "receipt version differs")
    at = _now(now)
    created, expires = _date(receipt.get("created_at")), _date(receipt.get("expires_at"))
    need(created <= at + timedelta(minutes=5) and created <= at < expires <= created + timedelta(days=30), "expired or invalid review receipt")
    checked = check_review(p, m, run, suite, receipt.get("review") or {}, now=created)
    need(checked == receipt, "receipt judgment or bindings differ")
    return receipt


def validate_admission(rec, root, *, now=None):
    """Re-open original sealed run and current suite; old pass keeps historical meaning."""
    need(rec.get("admission_version") == ADMISSION_VERSION, "historical admission only; " + PENDING)
    suite = active_suite(root, rec["bench"])
    need(suite["scope"] == "private-production", "synthetic test suite cannot unlock production campaign")
    from kg_hub_eval.business.tools.extract_bench import _bench, load_run
    p, m = _bench(suite["bench_dir"])
    run = load_run(Path(rec["run_dir"]))
    receipt = validate_receipt(p, m, run, suite, rec.get("semantic_review") or {}, now=now)
    need(receipt["verdict"] == "pass", "semantic regression did not pass")
    need(rec["model"] == receipt["model"] and rec["prompt_version"] == receipt["prompt"]["version"] and
         rec["prompt_sha256"] == receipt["prompt"]["rules_sha256"], "admission fields differ from receipt")
    return {k: receipt[k] for k in ("receipt_sha256", "suite_sha256", "context_sha256", "expires_at", "reviewer")}
