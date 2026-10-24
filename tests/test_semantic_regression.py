"""Fully synthetic semantic admission contracts; no providers or graph calls."""
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import semantic_regression as sr
from utils import cli_runtime as runtime
from tools import extract_bench as xb
from tools import extract_campaign as campaign
from tools import extract_regression as cli


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class PrivateArtifactTest(unittest.TestCase):
    def test_mode_private_during_write_under_permissive_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            real_dump = json.dump

            def inspect_mode(value, stream, **kwargs):
                self.assertEqual(os.fstat(stream.fileno()).st_mode & 0o777, 0o600)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                return real_dump(value, stream, **kwargs)

            previous = os.umask(0o022)
            try:
                with mock.patch.object(sr.json, "dump", side_effect=inspect_mode):
                    sr.write_new(path, {"private": "synthetic review body"})
            finally:
                os.umask(previous)
            self.assertEqual(sr.read(path), {"private": "synthetic review body"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                sr.write_new(path, {"replacement": True})

    def test_serialization_exception_leaves_only_private_nonoverwritable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial-review.json"

            def partial_then_fail(value, stream, **kwargs):
                self.assertEqual(os.fstat(stream.fileno()).st_mode & 0o777, 0o600)
                stream.write('{"private":"synthetic fragment')
                stream.flush()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                raise ValueError("synthetic serializer failure")

            previous = os.umask(0o022)
            try:
                with mock.patch.object(sr.json, "dump", side_effect=partial_then_fail), self.assertRaises(ValueError):
                    sr.write_new(path, {"private": "synthetic body"})
            finally:
                os.umask(previous)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), '{"private":"synthetic fragment')
            with self.assertRaises(FileExistsError):
                sr.write_new(path, {"retry": True})


class SemanticRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bench = self.root / "bench"
        self.bench.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.state = self.root / "state"
        self.m = {"tool": xb.TOOL, "bench_id": "synthetic-fixed-cases", "chain": "c8", "parts": [
            {"part": "one", "baseline_model": "reference", "items": 1}]}
        dump(self.bench / "manifest.json", self.m)
        dump(self.bench / "batch-one.json", {"items": [{"id": 7, "name": "Synthetic ownership",
             "episode_body": "Only the Cedar team may approve a release after the Maple team has verified its results."}],
             "lookahead": [{"id": 8, "title": "Maple verifies; Cedar approves"}]})
        self.output = {"id": 7, "entities": [{"name": "Cedar"}, {"name": "Maple"}],
                       "edges": [{"source": "Maple", "target": "release", "fact": "Maple approves release unconditionally."}]}
        dump(self.run_dir / "results-one.json", {"items": [self.output]})
        self.run = {"tool": xb.TOOL, "ms": 1, "credits": None, "model": "candidate", "executor": "codex", "label": "candidate", "bench": self.m["bench_id"],
                    "ids": [7], "prompt_version": "agent-v5+bench2", "prompt_sha256": "rules",
                    "system_prompt_sha256": "complete-system", "status": "complete"}
        dump(self.run_dir / "run.json", self.run)
        dump(self.run_dir / "input-data.json", {"parts": [sr.read(self.bench / "batch-one.json")]})
        self.run.update({"dir": str(self.run_dir), "results": {7: self.output}, "cost": {}})
        self.spec = {"suite_id": "ownership-conditions", "revision": 1, "scope": "synthetic-test",
                     "selection_rationale": "Fixed historical responsibility and condition regressions.", "cases": [
            {"case_id": "approval-owner", "obs_id": 7, "critical": True,
             "known_failure_run": str(self.run_dir),
             "rationale": "A previous synthetic output assigned approval to the verifier.", "criteria": [
                {"criterion_id": "owner-and-condition", "requirement": "Preserve Cedar approval and the prior Maple verification condition."}]}]}
        self.suite = sr.create_suite(self.bench, self.m, self.spec)
        self.suite_file = self.root / "suite.json"
        dump(self.suite_file, self.suite)
        sr.activate(self.state, self.suite_file, reviewer="test operator declaration", confirm_suite_sha=self.suite["suite_sha256"])

    def filled(self, verdict="fail"):
        review = sr.review_sheet(self.bench, self.m, self.run, self.suite)
        review["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        review["reviewer"] = {"name": "independent-reviewer", "method": "model", "identity_evidence": "self-declared",
                              "independence_declaration": "I reviewed this output independently of the extracting model."}
        review["cases"][0]["judgments"][0].update({"verdict": verdict,
             "source_quote": "Only the Cedar team may approve a release", "claim_pointer": "/edges/0/fact",
             "claim_quote": "Maple approves release unconditionally.",
             "responsibility_judgment": "Cedar is the source-stated approver; Maple only verifies results.",
             "explanation": "The actual output changes the responsible team and removes the required prior verification."})
        return review

    def receipt(self, verdict="fail", now=None):
        return sr.check_review(self.bench, self.m, self.run, self.suite, self.filled(verdict), now=now)

    def test_fixed_case_failure_separate_from_random_review(self):
        receipt = self.receipt()
        self.assertEqual(receipt["verdict"], "fail")
        self.assertEqual(receipt["failures"], [{"case_id": "approval-owner", "criterion_id": "owner-and-condition"}])
        self.assertEqual(sr.validate_receipt(self.bench, self.m, self.run, self.suite, receipt), receipt)
        self.assertNotIn("random", receipt)

    def test_sheet_starts_pending_without_auto_truth(self):
        sheet = sr.review_sheet(self.bench, self.m, self.run, self.suite)
        self.assertEqual(sheet["cases"][0]["judgments"][0]["verdict"], "pending")
        with self.assertRaises(ValueError):
            sr.check_review(self.bench, self.m, self.run, self.suite, sheet)

    def test_empty_noncritical_and_subset_are_rejected(self):
        for mutation in (lambda s: s.update(cases=[]), lambda s: s["cases"][0].update(critical=False)):
            spec = copy.deepcopy(self.spec)
            mutation(spec)
            with self.assertRaises(ValueError):
                sr.create_suite(self.bench, self.m, spec)
        run = {**self.run, "ids": [], "results": {}}
        with self.assertRaisesRegex(ValueError, "required critical"):
            sr.review_sheet(self.bench, self.m, run, self.suite)

    def test_incomplete_or_boolean_only_review_cannot_pass(self):
        for mutation in (lambda r: r["cases"][0].update(judgments=[]),
                         lambda r: r["cases"][0]["judgments"][0].update(verdict=True),
                         lambda r: r["cases"][0]["judgments"][0].update(source_quote=""),
                         lambda r: r["cases"][0]["judgments"][0].update(claim_pointer="/edges/8/fact"),
                         lambda r: r["cases"][0]["judgments"][0].update(responsibility_judgment="true"),
                         lambda r: r["reviewer"].update(name="candidate"),
                         lambda r: r["reviewer"].update(identity_evidence="provider-wrapper")):
            review = self.filled("pass")
            mutation(review)
            with self.subTest(review=review), self.assertRaises(ValueError):
                sr.check_review(self.bench, self.m, self.run, self.suite, review)

    def test_changes_and_expiry_invalidate_receipts(self):
        at = datetime.now(timezone.utc)
        receipt = self.receipt("pass", now=at)
        with self.assertRaisesRegex(ValueError, "expired"):
            sr.validate_receipt(self.bench, self.m, self.run, self.suite, receipt, now=at + timedelta(days=31))
        for field, value in (("model", "other"), ("prompt_sha256", "changed"), ("system_prompt_sha256", "changed"),
                             ("executor", "other"), ("status", "infrastructure_failed")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                sr.validate_receipt(self.bench, self.m, {**self.run, field: value}, self.suite, receipt)
        for field in ("verdict", "suite_sha256", "run_sha256", "context_sha256"):
            changed = copy.deepcopy(receipt)
            changed[field] = "changed"
            with self.subTest(field=field), self.assertRaises(ValueError):
                sr.validate_receipt(self.bench, self.m, self.run, self.suite, changed)
        original = (self.run_dir / "results-one.json").read_text()
        (self.run_dir / "results-one.json").write_text(original + " ")
        with self.assertRaisesRegex(ValueError, "context changed"):
            sr.validate_receipt(self.bench, self.m, self.run, self.suite, receipt)

    def test_current_policy_mandatory_and_suite_revisions_invalidate(self):
        with self.assertRaisesRegex(ValueError, "no canonical"):
            sr.active_suite(self.root / "missing", self.m["bench_id"])
        old = self.receipt("pass")
        new_spec = {**self.spec, "revision": 2}
        new = sr.create_suite(self.bench, self.m, new_spec)
        dump(self.suite_file, new)
        with self.assertRaisesRegex(ValueError, "active suite changed"):
            sr.active_suite(self.state, self.m["bench_id"])
        sr.activate(self.state, self.suite_file, reviewer="operator", confirm_suite_sha=new["suite_sha256"])
        with self.assertRaises(ValueError):
            sr.validate_receipt(self.bench, self.m, self.run, sr.active_suite(self.state, self.m["bench_id"]), old)

    def test_synthetic_policy_cannot_unlock_production(self):
        record = {"admission_version": sr.ADMISSION_VERSION, "bench": self.m["bench_id"], "semantic_review": self.receipt("pass")}
        with self.assertRaisesRegex(ValueError, "synthetic test suite"):
            sr.validate_admission(record, self.state)
        with self.assertRaisesRegex(ValueError, sr.PENDING):
            sr.validate_admission({"verdict": "pass"}, self.state)

    def test_pass_receipt_only_unlocks_bound_run_and_current_suite(self):
        # Synthetic data in an isolated fake production-policy registry. No real
        # local suite or admission history is created or changed by this test.
        # Native evidence integrity has its own real lifecycle fixture in
        # test_bench_reference; this unit test isolates receipt/run binding.
        with mock.patch.object(sr, "native_verify_run"):
            self.suite = sr.create_suite(self.bench, self.m, {**self.spec, "scope": "private-production"})
        dump(self.suite_file, self.suite)
        sr.activate(self.state, self.suite_file, reviewer="test operator", confirm_suite_sha=self.suite["suite_sha256"])
        receipt = self.receipt("pass")
        rec = {"admission_version": sr.ADMISSION_VERSION, "model": "candidate", "bench": self.m["bench_id"],
               "prompt_version": self.run["prompt_version"], "prompt_sha256": self.run["prompt_sha256"],
               "run_dir": str(self.run_dir), "semantic_review": receipt}
        self.assertEqual(sr.validate_admission(rec, self.state)["suite_sha256"], self.suite["suite_sha256"])
        for changed in ({**rec, "model": "other"}, {**rec, "run_dir": str(self.root / "other-run")},
                        {**rec, "prompt_version": "agent-v5+other"}):
            with self.assertRaises((ValueError, FileNotFoundError, SystemExit)):
                sr.validate_admission(changed, self.state)
        # A new canonical revision requires supplementary review, preserving the
        # old receipt and extraction bytes rather than silently mutating them.
        with mock.patch.object(sr, "native_verify_run"):
            new = sr.create_suite(self.bench, self.m, {**self.spec, "scope": "private-production", "revision": 2})
        dump(self.suite_file, new)
        sr.activate(self.state, self.suite_file, reviewer="operator", confirm_suite_sha=new["suite_sha256"])
        with self.assertRaises(ValueError):
            sr.validate_admission(rec, self.state)
        self.assertEqual(receipt["verdict"], "pass")

    def test_production_origin_requires_real_native_evidence_and_source_context(self):
        with self.assertRaises(SystemExit):
            sr.create_suite(self.bench, self.m, {**self.spec, "scope": "private-production"})
        snapshot = sr.read(self.run_dir / "input-data.json")
        snapshot["parts"][0]["lookahead"] = []
        dump(self.run_dir / "input-data.json", snapshot)
        with mock.patch.object(sr, "native_verify_run"), self.assertRaisesRegex(ValueError, "original observation/lookahead"):
            sr.create_suite(self.bench, self.m, {**self.spec, "scope": "private-production"})

    def test_changed_source_lookahead_or_run_metadata_rejected(self):
        receipt = self.receipt("pass")
        batch = self.bench / "batch-one.json"
        original = batch.read_text()
        value = sr.read(batch)
        value["lookahead"].append({"id": 10, "title": "changed context"})
        dump(batch, value)
        with self.assertRaisesRegex(ValueError, "source or lookahead changed"):
            sr.validate_receipt(self.bench, self.m, self.run, self.suite, receipt)
        batch.write_text(original)
        (self.run_dir / "run.json").write_text((self.run_dir / "run.json").read_text() + " ")
        with self.assertRaisesRegex(ValueError, "context changed"):
            sr.validate_receipt(self.bench, self.m, self.run, self.suite, receipt)

    def test_cli_create_activate_sheet_check_no_model_call(self):
        spec_file = self.root / "spec.json"
        dump(spec_file, self.spec)
        out = self.root / "cli-suite.json"
        with mock.patch.dict(os.environ, {"KG_HUB_BENCH_ROOT": str(self.state)}), mock.patch("sys.stdout"):
            self.assertEqual(cli.main(["create", "--bench", str(self.bench), "--spec", str(spec_file), "--out", str(out)]), 0)
            self.assertEqual(cli.main(["activate", "--suite", str(out), "--reviewer", "test operator",
                                      "--confirm-suite-sha", sr.read(out)["suite_sha256"]]), 0)
            pending = self.root / "sheet.json"
            self.assertEqual(cli.main(["sheet", "--bench", str(self.bench), "--run", str(self.run_dir), "--out", str(pending)]), 0)
            self.assertEqual(sr.read(pending)["cases"][0]["judgments"][0]["verdict"], "pending")
            dump(pending, self.filled("fail"))
            receipt = self.root / "receipt.json"
            self.assertEqual(cli.main(["check", "--bench", str(self.bench), "--run", str(self.run_dir),
                                      "--review", str(pending), "--out", str(receipt)]), 1)
            self.assertEqual(sr.read(receipt)["verdict"], "fail")

    def test_bench_admit_random_pass_plus_directed_fail_rejects(self):
        # Isolate scoring: random blind says zero errors; the independent semantic
        # gate still makes a genuine fail and never adds cases to random n.
        row = {"label": "candidate", "items": 50, "questions": 50}
        patches = [mock.patch.object(xb.reference, "verify_reference", return_value=None),
                   mock.patch.dict(xb.BASELINE_EQUIV, {"c8": "agent-v5"}),
                   mock.patch.object(xb, "bench_system_prompt", return_value=("system", "agent-v5+bench2")),
                   mock.patch.object(xb, "rules_sha", return_value="rules"),
                   mock.patch.object(xb, "validate_blind_sheet"),
                   mock.patch.object(xb, "score_many", return_value=[row]),
                   mock.patch.object(xb, "_bench_items", return_value=([], {})),
                   mock.patch.object(xb, "lint_items", return_value=[]),
                   mock.patch.object(xb, "blind_result", return_value={"judged": 10, "errors": 0}),
                   mock.patch.object(xb, "admission_checks", return_value=([], "pass")),
                   mock.patch.object(xb, "bench_root", return_value=self.state)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(xb, "append_admission") as append:
            pending = xb.admit(self.bench, self.m, self.run, sheet=[], key={})
            self.assertEqual(pending["verdict"], "pending_semantic_regression")
            append.assert_not_called()
            bad = xb.admit(self.bench, self.m, self.run, sheet=[], key={}, semantic_review=self.receipt())
            self.assertEqual(bad["verdict"], "semantic_regression_fail")
            self.assertEqual(bad["blind"], {"judged": 10, "errors": 0})
            append.assert_called_once()

    def test_native_aborted_run_precedes_scoring(self):
        with mock.patch.object(xb, "score_many") as score, self.assertRaises(SystemExit):
            xb.admit(self.bench, self.m, {**self.run, "native_evidence_version": 2, "status": "infrastructure_failed"})
        score.assert_not_called()


class StartProbeTest(unittest.TestCase):
    def probe(self, model="candidate"):
        def call(system, query):
            return {"text": query, "tokens": 3, "tokens_known": True, "ms": 1, "error": None}
        return runtime.probe(call, executor="codex", model=model, proxy_mode="env-only")

    def test_requires_real_same_tool_model_route_receipt(self):
        receipt = self.probe()
        self.assertEqual(campaign.model_readiness(["candidate"], "codex", [receipt], proxy_mode="env-only")["candidate"]["receipt_sha256"], receipt["receipt_sha256"])
        for receipts in ([], [receipt, receipt], [self.probe("different")]):
            with self.assertRaises(ValueError):
                campaign.model_readiness(["candidate"], "codex", receipts, proxy_mode="env-only")
        for executor, proxy in (("claude", "env-only"), ("codex", "none"), ("cursor", "env-only")):
            with self.assertRaises(ValueError):
                campaign.model_readiness(["candidate"], executor, [receipt], proxy_mode=proxy)

    def test_old_pass_is_historical_only(self):
        rec = {"model": "candidate", "verdict": "pass", "prompt_version": "agent-v5+bench2", "prompt_sha256": "rules"}
        gate = campaign.admission_gate(["candidate"], "agent-v5", "rules", lookup=lambda m: rec)
        self.assertFalse(gate["candidate"]["ok"])
        self.assertIn(sr.PENDING, gate["candidate"]["why"])
        self.assertEqual(rec, {"model": "candidate", "verdict": "pass", "prompt_version": "agent-v5+bench2", "prompt_sha256": "rules"})

    def test_newstart_missing_probe_does_not_create_manifest(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": tmp}), \
             mock.patch.object(campaign, "tracking_guard", return_value={"tracked": False}), \
             mock.patch.object(campaign, "admission_gate", return_value={"candidate": {"ok": True}}):
            with self.assertRaisesRegex(SystemExit, "model-preflight"):
                campaign.cmd_start("no-probe", ["candidate"], executor="codex", quota=1, batch=1, after_id=0)
            self.assertFalse((campaign.campaign_dir("no-probe") / "campaign.json").exists())

    def test_newstart_persists_probe_and_old_campaign_claim_does_not_recheck(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "KG_HUB_AGENT_EXTRACT_DIR": tmp, "KG_HUB_CLAIMS_DIR": str(Path(tmp) / "claims")}), \
             mock.patch.object(campaign, "tracking_guard", return_value={"tracked": False}), \
             mock.patch.object(campaign, "admission_gate", return_value={"candidate": {"ok": True}}):
            receipt = self.probe()
            state = campaign.cmd_start("frozen", ["candidate"], executor="codex", quota=1, batch=1,
                after_id=0, model_preflight=[receipt], proxy_mode="env-only")
            self.assertEqual(state["start_contract"]["version"], "campaign-start-v2")
            self.assertEqual(state["start_contract"]["model_preflight"]["candidate"]["receipt_sha256"], receipt["receipt_sha256"])
            # Emulate an already-open older manifest, preserving its snapshot.
            state.pop("start_contract")
            state["admission"] = {"gate": {"candidate": {"verdict": "pass"}}, "skipped": None}
            directory = campaign.campaign_dir("frozen")
            campaign.save(directory, state)
            observation = {"id": 1, "name": "synthetic-one", "source_obs_id": "1", "source_description": "fixture",
                           "episode_body": "Synthetic observation for a frozen campaign.", "reference_time": "2026-01-01T00:00:00Z"}
            with mock.patch.object(campaign, "admission_gate", side_effect=AssertionError("old claim must not recheck")), \
                 mock.patch.object(campaign, "model_readiness", side_effect=AssertionError("old claim must not probe")), \
                 mock.patch.object(campaign.ax, "load_config", return_value={}), \
                 mock.patch.object(campaign.ax, "select_batch", return_value=([observation], {"picked": 1})):
                result = campaign.cmd_claim("frozen", "candidate", url="http://unused", token=None,
                    fetch=lambda after, limit: {"rows": [{"id": 1, "title": "synthetic"}], "exhausted": True},
                    check=lambda ids: {})
                self.assertTrue(result.get("batch"))


if __name__ == "__main__":
    unittest.main()
