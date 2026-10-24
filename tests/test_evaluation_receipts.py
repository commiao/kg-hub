"""Evidence-index invariants, using synthetic local receipts and no models."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import extract_evaluation as cli
from utils import bench_reference as ref
from utils import evaluation_receipts as er
from utils import semantic_regression as sr


class EvaluationReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fixture(self, model="gpt-fixture", tag="v5"):
        d = self.root / (model + "-" + tag)
        (d / "work").mkdir(parents=True)
        rules = "prompt_version = agent-" + tag.split("-")[0] + "\nSynthetic rules."
        (d / "rules.md").write_text(rules)
        (d / "system-prompt.txt").write_text("Synthetic system.")
        items = [{"id": 1, "episode_body": "Synthetic private observation."}]
        contract = {"selected_ids": [1], "parts": [{"part": "p", "ids": [1],
                    "content_sha256": ref.digest({"items": items, "lookahead": []})}]}
        snapshot = {"parts": [{"part": "p", "items": items, "lookahead": []}]}
        er.write_json(d / "input-contract.json", contract)
        er.write_json(d / "input-data.json", snapshot)
        er.write_json(d / "work" / "p.json", {"part": "p", "ids": [1],
                      "query_sha256": hashlib.sha256(ref.query_text(items, []).encode()).hexdigest()})
        er.write_json(d / "results-p.json", {"items": [{"id": 1, "entities": [{"summary": "Synthetic supported claim."}], "edges": []}]})
        run = {"tool": "extract_bench", "label": "codex-" + model, "executor": "codex", "model": model,
               "tag": tag, "bench": "synthetic-bench", "execution_mode": "native_adapter", "ids": [1],
               "calls": 1, "tokens": 100, "tokens_known": True, "credits": 0, "credits_known": False, "ms": 5,
               "prompt_version": "agent-" + tag.split("-")[0], "prompt_sha256": ref.sha(d / "rules.md"),
               "system_prompt_sha256": ref.sha(d / "system-prompt.txt"), "input_contract_sha256": ref.digest(contract),
               "input_data_sha256": ref.digest(snapshot), "result_items_sha256": ref.digest(ref.result_items(d))}
        er.write_json(d / "run.json", ref.seal_run(d, run))
        run_hash = ref.sha(d / "run.json")
        score = {"runs": [{**{k: run[k] for k in ("model", "executor", "tag", "prompt_version", "prompt_sha256")},
                            "run_sha256": run_hash, "ids_sha256": ref.digest([1])}],
                 "rows": [{"label": run["label"] + "（全样本）", "items": 1, "missing": 0,
                           "headline_verbatim_pct": 100, "rest_fact_en_pct": 90}]}
        er.write_json(d / "score.json", score)
        bench = d / "frozen-bench"; bench.mkdir()
        manifest = {"bench_id": run["bench"], "parts": [{"part": "p"}]}
        er.write_json(bench / "manifest.json", manifest)
        er.write_json(bench / "batch-p.json", {"items": items, "lookahead": []})
        suite = sr.create_suite(bench, manifest, {"suite_id": "synthetic-suite", "revision": "1", "scope": "synthetic-test",
                    "selection_rationale": "Complete synthetic fixed regression case.", "cases": [{
                        "case_id": "case-1", "obs_id": 1, "critical": True, "rationale": "Synthetic source anchor.",
                        "criteria": [{"criterion_id": "claim", "requirement": "Synthetic summary must be supported."}]}]})
        er.write_json(d / "suite.json", suite)
        complete_run = {**er.read_json(d / "run.json"), "dir": str(d), "results": ref.result_items(d)}
        review = sr.review_sheet(bench, manifest, complete_run, suite)
        review["reviewed_at"] = "2026-01-01T00:00:00+00:00"
        review["reviewer"] = {"name": "synthetic independent reviewer", "method": "human", "identity_evidence": "self-declared",
                              "independence_declaration": "Independent synthetic review for offline testing."}
        review["cases"][0]["judgments"][0].update(verdict="pass", source_quote="Synthetic private observation.",
            claim_pointer="/entities/0/summary", claim_quote="Synthetic supported claim.",
            responsibility_judgment="Synthetic source supports the specified claim.", explanation="Synthetic source and output were independently checked.")
        semantic = sr.check_review(bench, manifest, complete_run, suite, review,
                                   now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        er.write_json(d / "semantic.json", semantic)
        er.write_json(d / "admission.json", {"admission_version": "admission-v2", "model": model,
                      "executor": run["executor"], "bench": run["bench"], "prompt_version": run["prompt_version"],
                      "items": 1,
                      "prompt_sha256": run["prompt_sha256"], "run_dir": str(d), "semantic_review": semantic,
                      "verdict": "screen_pass", "decided_at": "2026-01-02T00:00:00+00:00",
                      "semantic_regression": {"verdict": "pass", "denominator": "fixed-critical-suite"},
                      "checks": [{"check": "synthetic nonsemantic check", "pass": True},
                                 {"check": "独立语义回归", "pass": True, "value": "pass"}]})
        return d

    def entry(self, d, phase="admission"):
        return {"phase": phase, "run": str(d), "score": str(d / "score.json"),
                "semantic": str(d / "semantic.json"), "suite": str(d / "suite.json"), "admission": str(d / "admission.json")}

    def test_four_phase_model_entries_survive_and_repeat_run_cost_deduplicates(self):
        a, b = self.fixture("a"), self.fixture("b")
        index = er.build_index({"entries": [self.entry(a, "smoke"), self.entry(b, "smoke"),
                                             self.entry(a), self.entry(b)]})
        self.assertEqual(len(index["entries"]), 4)
        self.assertTrue(index["ready"])
        self.assertEqual(index["cost"]["totals"]["tokens"]["value"], 200)
        self.assertEqual(len(index["cost"]["components"]), 2)
        self.assertIn("其余英文%", er.render_markdown(index))
        self.assertEqual(index, er.build_index({"entries": [self.entry(a, "smoke"), self.entry(b, "smoke"), self.entry(a), self.entry(b)]}))

    def test_same_model_v4_v5_never_overwrites(self):
        old, new = self.fixture(tag="v4"), self.fixture(tag="v5")
        index = er.build_index({"entries": [self.entry(old, "smoke"), self.entry(new, "smoke")]})
        self.assertEqual({r["prompt_version"] for r in index["entries"]}, {"agent-v4", "agent-v5"})

    def test_missing_or_changed_receipts_and_source_are_rejected(self):
        d = self.fixture()
        index = er.build_index({"entries": [self.entry(d)]})
        (d / "score.json").write_text("{}")
        with self.assertRaises(ValueError):
            er.verify_index(index)
        (d / "semantic.json").unlink()
        with self.assertRaises(ValueError):
            er.build_index({"entries": [self.entry(d)]})

    def test_wrong_run_score_and_changed_native_evidence_rejected(self):
        d = self.fixture()
        score = er.read_json(d / "score.json")
        score["runs"][0]["run_sha256"] = "wrong"
        er.write_json(d / "score.json", score)
        with self.assertRaisesRegex(ValueError, "different run"):
            er.build_index({"entries": [self.entry(d)]})
        (d / "results-p.json").write_text("{}")
        with self.assertRaises(SystemExit):
            er.build_index({"entries": [{"phase": "extraction", "run": str(d)}]})

    def test_unknown_cost_is_null_and_no_phase_fabricated(self):
        d = self.fixture()
        index = er.build_index({"entries": [self.entry(d), {"phase": "formal", "model": "not-started"}]})
        self.assertFalse(index["ready"])
        self.assertEqual(index["entries"][1]["status"], "planned")
        self.assertIsNone(index["cost"]["totals"]["credits"]["value"])
        self.assertFalse(index["cost"]["totals"]["credits"]["complete"])
        self.assertFalse(index["cost"]["overhead_measured"])
        self.assertIsNone(er.overhead_coverage([])["tokens"]["value"])

    def test_preflight_and_extraction_are_counted_once_without_token_subsets(self):
        d = self.fixture()
        run = er.read_json(d / "run.json")
        run.pop("evidence")
        probe = {"cost": {"calls": 1, "tokens": 7, "tokens_known": True, "credits": None, "ms": 2}}
        er.write_json(d / "preflight.json", probe)
        run["preflight"] = {"path": "preflight.json", "sha256": ref.sha(d / "preflight.json"), "cost": probe["cost"]}
        run["cached_input_tokens"] = 999
        run["reasoning_tokens"] = 999
        er.write_json(d / "run.json", ref.seal_run(d, run))
        index = er.build_index({"entries": [{"phase": "extraction", "run": str(d)}, {"phase": "another-view", "run": str(d)}]})
        self.assertEqual(index["cost"]["totals"]["tokens"]["value"], 107)
        self.assertEqual(index["cost"]["totals"]["calls"]["value"], 2)
        self.assertEqual({c["kind"] for c in index["cost"]["components"]}, {"preflight", "extraction"})

    def test_legacy_default_zero_without_known_flag_stays_unknown(self):
        component = er.cost_component("synthetic-legacy", "extraction", {"tokens": 0, "credits": 0, "calls": 1})
        self.assertIsNone(component["tokens"])
        self.assertIsNone(component["credits"])
        self.assertEqual(component["calls"], 1)

    def test_new_unsealed_result_file_cannot_hide_behind_old_index(self):
        d = self.fixture()
        index = er.build_index({"entries": [self.entry(d)]})
        er.write_json(d / "results-extra.json", {"items": []})
        with self.assertRaises(SystemExit):
            er.verify_index(index)

    def test_legacy_score_and_missing_semantic_stay_pending(self):
        d = self.fixture()
        old_score = er.read_json(d / "score.json")
        old_score["runs"][0].pop("run_sha256")
        er.write_json(d / "score.json", old_score)
        row = er.build_index({"entries": [self.entry(d, "smoke")]})["entries"][0]
        self.assertEqual(row["status"], "pending_evaluation")
        self.assertFalse(row["ready"])
        row = er.build_index({"entries": [{"phase": "admission", "run": str(d)}]})["entries"][0]
        self.assertFalse(row["ready"])

    def test_semantic_requires_frozen_suite_and_complete_case_evidence(self):
        d = self.fixture()
        missing_suite = self.entry(d)
        missing_suite.pop("suite")
        with self.assertRaisesRegex(ValueError, "frozen semantic suite"):
            er.build_index({"entries": [missing_suite]})
        original = er.read_json(d / "semantic.json")
        for mutation in ("minimal", "missing-case", "missing-criterion", "wrong-source", "wrong-claim", "context-output", "missing-date"):
            with self.subTest(mutation=mutation):
                receipt = copy.deepcopy(original)
                review = receipt["review"]
                if mutation == "minimal":
                    receipt["review"] = {"synthetic": True}
                elif mutation == "missing-case":
                    review["cases"] = []
                elif mutation == "missing-criterion":
                    review["cases"][0]["judgments"] = []
                elif mutation == "wrong-source":
                    review["cases"][0]["judgments"][0]["source_quote"] = "This sentence is not in the frozen source."
                elif mutation == "wrong-claim":
                    review["cases"][0]["judgments"][0]["claim_quote"] = "Invented output that does not exist."
                elif mutation == "context-output":
                    review["context"]["cases"][0]["actual_output"]["entities"][0]["summary"] = "Forged context output."
                    review["context_sha256"] = ref.digest(review["context"])
                    receipt["context_sha256"] = review["context_sha256"]
                else:
                    receipt.pop("created_at")
                receipt["review_sha256"] = ref.digest(receipt["review"])
                receipt["receipt_sha256"] = ref.digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
                er.write_json(d / "semantic.json", receipt)
                with self.assertRaises((ValueError, KeyError)):
                    er.build_index({"entries": [self.entry(d)]})

    def test_rehashed_forged_receipt_verdict_and_admission_identity_are_rejected(self):
        d = self.fixture()
        receipt = er.read_json(d / "semantic.json")
        forged = copy.deepcopy(receipt)
        forged["verdict"] = "fail"
        forged["receipt_sha256"] = ref.digest({k: v for k, v in forged.items() if k != "receipt_sha256"})
        er.write_json(d / "semantic.json", forged)
        with self.assertRaisesRegex(ValueError, "judgment or bindings"):
            er.build_index({"entries": [self.entry(d)]})
        er.write_json(d / "semantic.json", receipt)
        original_admission = er.read_json(d / "admission.json")
        for field in ("executor", "bench", "prompt_version", "items", "semantic_regression", "semantic_review"):
            with self.subTest(field=field):
                admission = copy.deepcopy(original_admission)
                if field == "semantic_regression":
                    admission[field]["verdict"] = "fail"
                elif field == "semantic_review":
                    admission[field]["reviewer"]["name"] = "Different reviewer with unchanged claimed hash"
                else:
                    admission[field] = "wrong"
                er.write_json(d / "admission.json", admission)
                with self.assertRaises(ValueError):
                    er.build_index({"entries": [self.entry(d)]})

    def test_failed_review_cannot_be_presented_as_admission_pass(self):
        d = self.fixture()
        original = er.read_json(d / "semantic.json")
        suite = er.read_json(d / "suite.json")
        run = {**er.read_json(d / "run.json"), "dir": str(d), "results": ref.result_items(d)}
        review = original["review"]
        review["cases"][0]["judgments"][0]["verdict"] = "fail"
        receipt = sr.check_review(Path(suite["bench_dir"]), er.read_json(Path(suite["bench_dir"]) / "manifest.json"),
                                 run, suite, review, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        er.write_json(d / "semantic.json", receipt)
        admission = er.read_json(d / "admission.json")
        admission.update(semantic_review=receipt, verdict="pass")
        admission["semantic_regression"]["verdict"] = "fail"
        admission["checks"][-1].update(value="fail", **{"pass": False})
        er.write_json(d / "admission.json", admission)
        with self.assertRaisesRegex(ValueError, "failed semantic review"):
            er.build_index({"entries": [self.entry(d)]})
        admission["verdict"] = "semantic_regression_fail"
        er.write_json(d / "admission.json", admission)
        index = er.build_index({"entries": [self.entry(d)]})
        self.assertTrue(index["ready"])
        self.assertEqual(index["entries"][0]["admission"]["verdict"], "semantic_regression_fail")

    def test_historical_expiry_is_separate_from_current_status_and_policy(self):
        d = self.fixture()
        with mock.patch.object(sr, "active_suite", side_effect=AssertionError("current policy must not be consulted")):
            index = er.build_index({"entries": [self.entry(d)]})
            er.verify_index(index)
        self.assertTrue(index["ready"])
        before = er.current_semantic_validity(index, now=datetime(2026, 1, 15, tzinfo=timezone.utc))[0]
        after = er.current_semantic_validity(index, now=datetime(2026, 2, 1, tzinfo=timezone.utc))[0]
        self.assertEqual(before["current_status"], "within_review_ttl")
        self.assertEqual(after["current_status"], "expired")
        self.assertEqual(after["historical_validity"], "valid_at_review_and_admission")
        admission = er.read_json(d / "admission.json")
        for date in ("2025-12-31T23:00:00+00:00", "2026-01-31T00:00:00+00:00"):
            admission["decided_at"] = date
            er.write_json(d / "admission.json", admission)
            with self.assertRaisesRegex(ValueError, "expired or invalid"):
                er.build_index({"entries": [self.entry(d)]})

    def test_verify_index_replays_semantic_evidence_even_after_rehash(self):
        d = self.fixture()
        index = er.build_index({"entries": [self.entry(d)]})
        index["entries"][0]["semantic"]["verdict"] = "fail"
        index["index_sha256"] = ref.digest({k: v for k, v in index.items() if k != "index_sha256"})
        with self.assertRaisesRegex(ValueError, "complete source evidence"):
            er.verify_index(index)

    def test_review_must_anchor_the_inputs_actually_seen_by_the_native_run(self):
        d = self.fixture()
        snapshot = er.read_json(d / "input-data.json")
        snapshot["parts"][0]["items"][0]["episode_body"] = "Different source actually visible to the candidate."
        contract = er.read_json(d / "input-contract.json")
        contract["parts"][0]["content_sha256"] = ref.digest({"items": snapshot["parts"][0]["items"], "lookahead": []})
        er.write_json(d / "input-data.json", snapshot)
        er.write_json(d / "input-contract.json", contract)
        call = er.read_json(d / "work" / "p.json")
        call["query_sha256"] = hashlib.sha256(ref.query_text(snapshot["parts"][0]["items"], []).encode()).hexdigest()
        er.write_json(d / "work" / "p.json", call)
        run = er.read_json(d / "run.json"); run.pop("evidence")
        run.update(input_data_sha256=ref.digest(snapshot), input_contract_sha256=ref.digest(contract))
        er.write_json(d / "run.json", ref.seal_run(d, run))
        new_hash = ref.sha(d / "run.json")
        # Rehash every outer binding, leaving the review attached to the wrong
        # source. A valid file checksum is insufficient to excuse that mismatch.
        score = er.read_json(d / "score.json"); score["runs"][0]["run_sha256"] = new_hash
        er.write_json(d / "score.json", score)
        receipt = er.read_json(d / "semantic.json")
        receipt["run_sha256"] = new_hash
        receipt["review"]["context"]["run_sha256"] = new_hash
        receipt["review"]["context_sha256"] = ref.digest(receipt["review"]["context"])
        receipt["context_sha256"] = receipt["review"]["context_sha256"]
        receipt["review_sha256"] = ref.digest(receipt["review"])
        receipt["receipt_sha256"] = ref.digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
        er.write_json(d / "semantic.json", receipt)
        admission = er.read_json(d / "admission.json"); admission["semantic_review"] = receipt
        er.write_json(d / "admission.json", admission)
        with self.assertRaisesRegex(ValueError, "actual frozen run inputs"):
            er.build_index({"entries": [self.entry(d)]})

    def test_reported_zero_service_failure_differs_from_unstarted(self):
        d = self.root / "failed"; d.mkdir()
        er.write_json(d / "run-aborted.json", {"status": "infrastructure_failed", "failure": {"kind": "auth"},
                      "queued_not_started": 4, "inflight_finished": 1,
                      "extraction_cost": {"calls": 1, "tokens": 0, "tokens_known": True}})
        row = er.build_index({"entries": [{"phase": "smoke", "run": str(d)}, {"phase": "smoke", "model": "queued"}]})
        self.assertEqual(row["entries"][0]["queued_not_started"], 4)
        self.assertEqual(row["cost"]["totals"]["tokens"]["value"], 0)
        self.assertEqual(row["entries"][1]["status"], "planned")

    def test_scope_never_grants_authorization_and_changes_invalidate_it(self):
        material = self.root / "blind.json"; material.write_text("[]")
        scope = er.create_scope({"artifacts": [str(material)], "records": 20, "destination": {"status": "unspecified"},
                                 "purpose": "blind review", "data_classes": ["source_body"],
                                 "approval_status": "approved", "human_message_refs": ["example-human-ref"]})
        self.assertEqual(er.verify_scope(scope)["readiness"], "action_required_destination")
        self.assertFalse(scope["permission_granted"])
        self.assertEqual(scope["approval_status"], "pending")
        material.write_text("[1]")
        with self.assertRaises(ValueError):
            er.verify_scope(scope)

    def test_public_cli_and_stdlib_import_dont_load_embeddings(self):
        process = subprocess.run([sys.executable, "-c", "from tools import extract_evaluation; import sys; assert 'numpy' not in sys.modules; assert 'fastembed' not in sys.modules"],
                                 cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        spec = self.root / "spec.json"; out = self.root / "index.json"
        er.write_json(spec, {"entries": [{"phase": "smoke", "model": "queued"}]})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["index", "--spec", str(spec), "--out", str(out)]), 0)
            self.assertEqual(cli.main(["report", "--index", str(out), "--markdown", str(self.root / "report.md")]), 0)
        self.assertIn("未完成", (self.root / "report.md").read_text())

    def test_blind_entry_writes_pending_scope_without_embedding_original_body(self):
        from tools import extract_bench
        out = self.root / "blind.json"
        sheet = [{"code": "anonymous-1", "observation": "synthetic original body"}]
        with mock.patch.object(extract_bench, "_bench", return_value=(self.root, {})), \
                mock.patch.object(extract_bench, "load_run", return_value={}), \
                mock.patch.object(extract_bench, "blind_sheet", return_value=(sheet, {})), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(extract_bench.main(["blind", "--bench", "synthetic", "--run", "synthetic-run", "--out", str(out)]), 0)
        scope = er.read_json(str(out) + ".scope.json")
        self.assertFalse(scope["permission_granted"])
        self.assertEqual(er.verify_scope(scope)["readiness"], "action_required_destination")
        self.assertNotIn("synthetic original body", json.dumps(scope))


if __name__ == "__main__":
    unittest.main()
