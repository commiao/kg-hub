"""Source-bound review and preview regressions; all networking is fake."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import agent_extract as ax
from tools import extract_campaign as ec
from utils import extraction_quality as q

PROMPT = "agent-v5 frozen complete rules"
IDENTITY = q.extraction_identity("m", "codex", "run-1")
TITLE = "Worker retries only before provider dispatch"
FACT = "The worker limits pre-provider retries to three attempts, while dispatch failures require reconciliation."


def fixture():
    batch = {"batch_id": "b1", "prompt_version": "agent-v5", "items": [{
        "id": 1, "name": "obs-1", "source_description": "test", "source_obs_id": "source-1",
        "reference_time": "2026-10-24T00:00:00Z",
        "episode_body": f"[DISCOVERY] {TITLE}\n{FACT}\nProject: wrong-project"}],
        "lookahead": [{"id": 2, "title": "Worker dispatch boundary was clarified"}]}
    result = {"id": 1, "entities": [{"name": "Worker", "type": "Tool", "summary": FACT},
                                     {"name": "Provider", "type": "Tool"}],
              "edges": [{"source": "Worker", "target": "Provider", "name": "relates_to", "fact": fact}
                        for fact in (TITLE, "工作线程仅在调用供应商前重试", FACT)]}
    results = {"items": [result]}
    review = approved(batch, results)
    return batch, results, review


def approved(batch, results, prompt=PROMPT, identity=IDENTITY):
    """Test-only oracle: tests explicitly provide a known correct fixture, never live data."""
    review = q.review_sheet(batch, results, prompt, identity)
    review["reviewer"] = "independent-test-auditor"
    for item, rec in zip(batch["items"], review["items"]):
        rec.update(verdict="ok", translation_complete=True, facts_complete=True,
                   core_covered=True, ownership_checked=True)
        quote = q.body_evidence(item)
        for edge in rec["edges"]:
            edge.update(quote=quote)
        for summary in rec["summaries"]:
            summary["quote"] = quote
        if rec["empty"] is not None:
            rec["empty"].update(reason="no_stable_knowledge", quote=quote, no_new_information=True)
    return review


def payload(batch, results, review):
    out = ax.build_payload(batch["items"][0], results["items"][0], model="m", session="s", executor="codex", run_id="run-1", prompt_version="agent-v5")
    out.update(quality_review=review, quality_context={"batch": batch, "results": results,
               "prompt_text": PROMPT, "prompt_version": "agent-v5"})
    return out


class QualityReviewTest(unittest.TestCase):
    def test_template_is_never_an_approval_and_full_review_passes(self):
        batch, results, review = fixture()
        sheet = q.review_sheet(batch, results, PROMPT)
        self.assertEqual(sheet["items"][0]["verdict"], "pending")
        self.assertTrue(q.validate_review(batch, results, sheet, PROMPT))
        self.assertEqual(q.validate_review(batch, results, review, PROMPT), [])
        self.assertEqual(q.validate_payload_quality(payload(batch, results, review)), [])

    def test_mutating_any_reviewed_context_requires_new_review(self):
        for part in ("body", "lookahead", "prompt", "result"):
            with self.subTest(part=part):
                batch, results, review = fixture()
                prompt = PROMPT
                if part == "body":
                    batch["items"][0]["episode_body"] += " Changed body"
                elif part == "lookahead":
                    batch["lookahead"][0]["title"] += " Changed future"
                elif part == "prompt":
                    prompt += " Changed rules"
                else:
                    results["items"][0]["entities"][0]["summary"] += " Changed result"
                self.assertTrue(any("context_changed" in x for x in q.validate_review(batch, results, review, prompt)))

    def test_payload_cannot_change_identity_time_rules_or_result(self):
        batch, results, review = fixture()
        for key in ("name", "reference_time", "episode_body", "prompt_version", "extractor_model", "extractor_tool", "run_id"):
            out = payload(batch, results, review)
            out[key] += "-changed"
            self.assertTrue(q.validate_payload_quality(out), key)
        out = payload(batch, results, review)
        out["entities"][0]["summary"] = "fabricated summary"
        self.assertIn("quality_result_mismatch", q.validate_payload_quality(out))

    def test_normalizer_retains_all_approved_facts_and_rejects_structure_loss(self):
        from schema import ENTITY_TYPES, EDGE_TYPES
        from utils.extracted_ingest import ExtractedEntity, ExtractedEdge, normalize_extraction
        batch, results, review = fixture()
        result = results["items"][0]
        normalized = normalize_extraction([ExtractedEntity(**e) for e in result["entities"]],
            [ExtractedEdge(**e) for e in result["edges"]], set(ENTITY_TYPES), set(EDGE_TYPES))
        self.assertEqual([e.fact for e in normalized.edges], [e["fact"] for e in result["edges"]])
        for change in ("self", "case_folded_entity", "duplicate_edge", "no_edges"):
            with self.subTest(change=change):
                bad = copy.deepcopy(results)
                row = bad["items"][0]
                if change == "self":
                    for edge in row["edges"]:
                        edge["target"] = edge["source"]
                elif change == "case_folded_entity":
                    row["entities"].append({"name": "worker", "type": "Tool"})
                elif change == "duplicate_edge":
                    row["edges"].append(copy.deepcopy(row["edges"][-1]))
                else:
                    row["edges"] = []
                self.assertTrue(q.validate_review(batch, bad, approved(batch, bad), PROMPT))
                post = mock.Mock()
                with self.assertRaises(SystemExit):
                    ax.submit_results(batch, bad, model="m", session="s", url="http://fake", token=None,
                        executor="codex", run_id="run-1", post=post, dry_run=True,
                        quality_review=approved(batch, bad), prompt_text=PROMPT)
                post.assert_not_called()

    def test_rule21_correction_requires_lookahead_and_mixed_names_can_translate(self):
        batch, results, _ = fixture()
        results["items"][0]["edges"][0]["fact"] = TITLE + "（后被推翻：dispatch boundary clarified）"
        review = approved(batch, results)
        self.assertTrue(any("lookahead" in e for e in q.validate_review(batch, results, review, PROMPT)))
        review["items"][0]["edges"][0].update(source="lookahead", quote=batch["lookahead"][0]["title"])
        self.assertEqual(q.validate_review(batch, results, review, PROMPT), [])
        for title, translation in (("飞书 Worker retries before dispatch", "飞书工作线程在分发前重试"),
                                   ("飞书工作线程在分发前重试", "飞书 Worker retries before dispatch")):
            batch, results, _ = fixture()
            batch["items"][0]["episode_body"] = "[DISCOVERY] " + title + "\n" + FACT
            results["items"][0]["edges"][0]["fact"] = title
            results["items"][0]["edges"][1]["fact"] = translation
            self.assertEqual(q.validate_review(batch, results, approved(batch, results), PROMPT), [])
            results["items"][0]["edges"][1]["fact"] = title
            self.assertTrue(any("translation_identical" in e for e in q.validate_review(batch, results, approved(batch, results), PROMPT)))

    def test_review_identity_is_fixed_before_approval_and_cannot_be_reused(self):
        batch, results, review = fixture()
        for field in q.IDENTITY_FIELDS:
            changed = copy.deepcopy(review)
            changed["identity"][field] += "-other"
            self.assertTrue(any("context_changed" in e for e in q.validate_review(batch, results, changed, PROMPT)))
            body = payload(batch, results, review)
            body[field] += "-other"
            self.assertIn("quality_identity_mismatch: " + field, q.validate_payload_quality(body))
        post = mock.Mock()
        with self.assertRaises(SystemExit):
            ax.submit_results(batch, results, model="other", executor="codex", run_id="run-1", session="s",
                url="http://fake", token=None, post=post, dry_run=True, quality_review=review, prompt_text=PROMPT)
        post.assert_not_called()

    def test_project_recording_metadata_is_not_ownership_evidence(self):
        batch, results, _ = fixture()
        results["items"][0]["entities"].append({"name": "wrong-project", "type": "Project"})
        results["items"][0]["edges"].append({"source": "Worker", "target": "wrong-project",
                                                 "name": "belongs_to", "fact": "Worker belongs to wrong-project."})
        review = approved(batch, results)
        review["items"][0]["edges"][-1]["quote"] = "Project: wrong-project"
        errors = q.validate_review(batch, results, review, PROMPT)
        self.assertTrue(any("source_quote_required" in x for x in errors))

    def test_every_fact_and_summary_requires_its_own_evidence(self):
        batch, results, review = fixture()
        review["items"][0]["edges"].pop()
        self.assertTrue(q.validate_review(batch, results, review, PROMPT))
        review = approved(batch, results)
        review["items"][0]["summaries"] = []
        self.assertTrue(any("summary" in x for x in q.validate_review(batch, results, review, PROMPT)))

    def test_malformed_receipts_fail_closed_without_crashing(self):
        batch, results, review = fixture()
        for bad in (None, [], {"schema": q.QUALITY_VERSION, "items": None}, {"schema": q.QUALITY_VERSION, "items": {}}):
            self.assertTrue(q.validate_review(batch, results, bad, PROMPT))
        for key in ("edges", "summaries"):
            for value in (None, "bad", [None], [{"index": False}]):
                altered = copy.deepcopy(review)
                altered["items"][0][key] = value
                self.assertTrue(q.validate_review(batch, results, altered, PROMPT), (key, value))
        for context in (None, [], {"batch": None, "results": []}, {"batch": {"items": [None]}, "results": {}, "prompt_text": PROMPT}):
            out = payload(batch, results, review)
            out["quality_context"] = context
            self.assertTrue(q.validate_payload_quality(out))
        altered = copy.deepcopy(review)
        altered["items"][0]["edges"][0]["index"] = False
        self.assertTrue(q.validate_review(batch, results, altered, PROMPT))

    def test_empty_and_short_sources_do_not_require_invented_evidence(self):
        for body in ("", "短回执"):
            batch, _, _ = fixture()
            batch["items"][0]["episode_body"] = body
            results = {"items": [{"id": 1, "entities": [], "edges": []}]}
            review = approved(batch, results)
            review["items"][0]["empty"]["reason"] = "unreadable"
            self.assertEqual(q.validate_review(batch, results, review, PROMPT), [])

    def test_historical_test_numbers_cannot_be_mechanically_discarded(self):
        batch, _, _ = fixture()
        batch["items"][0]["episode_body"] = "Historical replay: watchdog_capture.py showed 38 passed, 1 failed; 13 files failed."
        results = {"items": [{"id": 1, "entities": [], "edges": []}]}
        errors = q.validate_review(batch, results, approved(batch, results), PROMPT)
        self.assertTrue(any("concrete_test_results" in x for x in errors))

    def test_duplicate_requires_prior_nonempty_fact_coverage(self):
        batch, results, _ = fixture()
        batch["items"].append({**batch["items"][0], "id": 2, "source_obs_id": "s2"})
        results["items"].append({"id": 2, "entities": [], "edges": []})
        review = approved(batch, results)
        review["items"][1]["empty"].update(reason="duplicate", duplicate_of=1, covered_quote=FACT)
        self.assertEqual(q.validate_review(batch, results, review, PROMPT), [])
        for ref in (2, 99):
            review["items"][1]["empty"]["duplicate_of"] = ref
            self.assertTrue(q.validate_review(batch, results, review, PROMPT))


class ClientGateTest(unittest.TestCase):
    def test_cli_requires_review_then_exact_preview_and_invalidates_mutations(self):
        batch, results, review = fixture()
        sent = []
        real_submit = ax.submit_results
        def post(method, url, body, token):
            sent.append(body)
            self.assertEqual(q.validate_payload_quality(body), [])
            return 200, {"status": "dry_run" if body.get("dry_run") else "ok"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            b, r, receipt, prompt = (root / name for name in ("batch.json", "results.json", "review.json", "prompt.md"))
            b.write_text(json.dumps(batch)); r.write_text(json.dumps(results)); prompt.write_text(PROMPT)
            args = ["--batch", str(b), "--file", str(r), "--prompt-file", str(prompt),
                    "--executor", "codex", "--run-id", "run-1"]
            env = {"KG_HUB_AGENT_EXTRACT_DIR": directory, "KG_HUB_CLAIMS_DIR": str(root / "claims")}
            with mock.patch.dict(os.environ, env), mock.patch.object(ax, "api_token", return_value="fake"), \
                    mock.patch.object(ax, "submit_results", side_effect=lambda *a, **kw: real_submit(*a, **{**kw, "post": post})), \
                    mock.patch("sys.stdout"):
                with self.assertRaises(SystemExit):
                    ax.main(["submit", *args, "--model", "m", "--dry-run"])
                self.assertEqual(ax.main(["quality-sheet", *args, "--model", "m", "--out", str(receipt)]), 0)
                self.assertEqual(ax.main(["quality-check", *args, "--review", str(receipt)]), 1)
                self.assertEqual(sent, [])
                receipt.write_text(json.dumps(review))
                self.assertEqual(ax.main(["quality-check", *args, "--review", str(receipt)]), 0)
                with self.assertRaises(SystemExit):
                    ax.main(["submit", *args, "--model", "m", "--skip-dryrun-check"])
                self.assertEqual(sent, [])
                with self.assertRaises(SystemExit):
                    ax.main(["submit", *args, "--model", "m"])
                self.assertEqual(ax.main(["submit", *args, "--model", "m", "--dry-run"]), 0)
                results["items"][0]["entities"][0]["summary"] += " "
                r.write_text(json.dumps(results))
                receipt.write_text(json.dumps(approved(batch, results)))
                self.assertEqual(ax.main(["quality-check", *args, "--review", str(receipt)]), 0)
                with self.assertRaises(SystemExit):
                    ax.main(["submit", *args, "--model", "m"])
                self.assertEqual(ax.main(["submit", *args, "--model", "m", "--dry-run"]), 0)
                self.assertEqual(ax.main(["submit", *args, "--model", "m"]), 0)
            self.assertEqual(len(sent), 3)
            self.assertEqual(json.loads((root / "log.jsonl").read_text())["status"], "ok")

    def test_every_executor_hits_same_gate_and_missing_review_never_posts(self):
        batch, results, review = fixture()
        for executor in ("codex", "claude-code", "qoder", "cursor", "dsh", "openclaw"):
            sent = []
            def post(method, url, body, token):
                sent.append(body)
                self.assertEqual(q.validate_payload_quality(body), [])
                return 200, {"status": "dry_run"}
            with self.assertRaises(SystemExit):
                ax.submit_results(batch, results, model="m", session="s", url="http://fake", token=None,
                                  executor=executor, post=post, dry_run=True, prompt_text=PROMPT)
            self.assertEqual(sent, [])
            ax.submit_results(batch, results, model="m", session="s", url="http://fake", token=None,
                              executor=executor, run_id="run-1", post=post, dry_run=True,
                              quality_review=approved(batch, results, identity=q.extraction_identity("m", executor, "run-1")), prompt_text=PROMPT)
            self.assertEqual(len(sent), 1)

    def test_preview_digest_changes_when_payload_changes_and_legacy_ids_do_not_pass(self):
        batch, results, review = fixture()
        with tempfile.TemporaryDirectory() as directory:
            sd = Path(directory)
            ax.record_dryrun(sd, "b1", [{"id": 1, "status": "dry_run"}])
            self.assertEqual(ax.dryrun_missing(sd, "b1", results, batch=batch, prompt_text=PROMPT, identity=IDENTITY), [1])
            recs = ax.submit_results(batch, results, model="m", session="s", url="http://fake", token=None,
                                     post=lambda *a: (200, {"status": "dry_run"}), dry_run=True,
                                     quality_review=review, prompt_text=PROMPT, executor="codex", run_id="run-1")
            ax.record_dryrun(sd, "b1", recs)
            self.assertEqual(ax.dryrun_missing(sd, "b1", results, batch=batch, prompt_text=PROMPT, identity=IDENTITY), [])
            results["items"][0]["edges"][2]["fact"] += " Changed."
            self.assertEqual(ax.dryrun_missing(sd, "b1", results, batch=batch, prompt_text=PROMPT, identity=IDENTITY), [1])

    def test_changing_previous_item_invalidates_later_pending_preview(self):
        batch, results, _ = fixture()
        batch["items"].append({**batch["items"][0], "id": 2, "source_obs_id": "source-2", "name": "obs-2"})
        results["items"].append({**copy.deepcopy(results["items"][0]), "id": 2})
        def preview():
            return ax.submit_results(batch, results, model="m", executor="codex", run_id="run-1", session="s",
                url="http://fake", token=None, post=lambda *a: (200, {"status": "dry_run"}), dry_run=True,
                quality_review=approved(batch, results), prompt_text=PROMPT)
        with tempfile.TemporaryDirectory() as directory:
            sd = Path(directory)
            ax.record_dryrun(sd, "b1", preview())
            self.assertEqual(ax.dryrun_missing(sd, "b1", results, batch=batch, prompt_text=PROMPT, identity=IDENTITY), [])
            results["items"][0]["entities"][0]["name"] = "RenamedWorker"
            for edge in results["items"][0]["edges"]:
                edge["source"] = "RenamedWorker"
            # Even if the first item gets previewed again, the old second preview
            # cannot be combined with it: its pending context was different.
            ax.record_dryrun(sd, "b1", preview()[:1])
            self.assertEqual(ax.dryrun_missing(sd, "b1", results, batch=batch, prompt_text=PROMPT, identity=IDENTITY), [2])
            batch["lookahead"][0]["title"] += " Updated future."
            self.assertEqual(ax.dryrun_missing(sd, "b1", results, batch=batch, prompt_text=PROMPT, identity=IDENTITY), [1, 2])
            ax.record_dryrun(sd, "b1", preview())
            self.assertEqual(ax.dryrun_missing(sd, "b1", results, batch=batch, prompt_text=PROMPT, identity=IDENTITY), [])

    def test_preflight_rejects_health_only_or_wrong_protocol(self):
        with mock.patch.dict(os.environ, {"CLAUDE_MEM_INTERNAL": "1"}):
            for response in ({"ok": True}, {"quality_versions": ["old"], "prompt_versions": ["agent-v5"]}):
                self.assertFalse(ax.runtime_preflight("http://fake", None, get=lambda *a: (200, response))["ok"])
            good = {"quality_versions": [q.QUALITY_VERSION], "prompt_versions": ["agent-v5"]}
            self.assertTrue(ax.runtime_preflight("http://fake", None, get=lambda *a: (200, good))["ok"])

    def test_bootstrap_ignores_stale_working_directory_modules(self):
        entry = Path(ax.__file__).resolve().parent / "extract_cli.py"
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "tools"
            fake.mkdir()
            (fake / "__init__.py").write_text("")
            (fake / "agent_extract.py").write_text("raise RuntimeError('stale checkout imported')")
            env = dict(os.environ, PYTHONPATH=directory)
            out = subprocess.run([sys.executable, str(entry), "agent", "prompt"], cwd=directory, env=env,
                                 text=True, capture_output=True, check=True)
            self.assertEqual(out.stdout, ax.PROMPT_PATH.read_text())


class ReportingTest(unittest.TestCase):
    def test_standalone_status_deduplicates_attempts_and_requires_empty_receipt(self):
        rows = [{"id": 1, "batch": "b", "model": "m", "status": "error"},
                {"id": 1, "batch": "b", "model": "m", "status": "ok"},
                {"id": 2, "batch": "b", "model": "m", "status": "empty"}]
        summary = ax.summarize_log(rows)["m"]
        self.assertEqual((summary["n"], summary["ok"], summary["empty"], summary["error"]), (2, 1, 0, 1))

    def test_mark_empty_v5_never_bypasses_review(self):
        batch, _, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.jsonl"
            path.write_text(json.dumps({"id": 1, "batch": "b1", "status": "empty", "model": "m",
                                        "prompt_version": "agent-v5"}) + "\n")
            post = mock.Mock(side_effect=AssertionError("must not post without quality review"))
            out = ax.mark_empty_from_log(path, lambda _: batch, url="http://fake", token=None, post=post)
            self.assertEqual(out["by_status"], {"quality_review_required": 1})
            post.assert_not_called()

    def test_random_and_targeted_reviews_of_same_item_are_not_merged(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": directory}):
            d = ec.campaign_dir("review-kinds")
            c = ec.new_campaign("review-kinds", ["m"], executor="codex", quota=1, batch=1, after_id=0)
            c["batches"] = [{"id": "m-001", "model": "m", "chain": "c0", "ids": [1],
                              "status": "submitted", "cost": {}}]
            d.mkdir(parents=True)
            ec.save(d, c)
            ec.cmd_review("review-kinds", "m-001", 1, verdict="ok", kind="random")
            ec.cmd_review("review-kinds", "m-001", 1, verdict="error", kind="targeted")
            rows = ec.load(d)["batches"][0]["reviews"]
            self.assertEqual([(x["kind"], x["verdict"]) for x in rows], [("random", "ok"), ("targeted", "error")])

    def test_legacy_review_kind_is_unknown_and_never_in_random_denominator(self):
        batch, results, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / "batches").mkdir(); (d / "results").mkdir()
            (d / "batches" / "b1.json").write_text(json.dumps(batch))
            (d / "results" / "b1.json").write_text(json.dumps(results))
            b = {"id": "b1", "model": "m", "chain": "c0", "status": "submitted", "reviews": [
                {"obs": 1, "verdict": "error", "note": "random sample"},
                {"obs": 1, "verdict": "ok", "kind": "random"},
                {"obs": 1, "verdict": "error", "kind": "targeted"}]}
            row = ec.batch_row(d, b)
            self.assertEqual((row["reviewed"], row["random_reviewed"], row["targeted_reviewed"], row["legacy_reviewed"]), (3, 1, 1, 1))
            self.assertEqual((row["review_errors"], row["random_review_errors"], row["legacy_review_errors"]), (2, 0, 1))
            merged = ec.merge_rows("m", [row])
            self.assertEqual(merged["legacy_reviewed"], 1)

    def test_final_status_and_failures_use_latest_confirmed_record(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            records = [{"batch": "b", "id": 1, "status": "error"},
                       {"batch": "b", "id": 1, "status": "ok", "http": 200},
                       {"batch": "b", "id": 2, "status": "empty"},
                       {"batch": "b", "id": 3, "status": "empty", "http": 200, "empty_recorded": True}]
            (d / "log.jsonl").write_text("\n".join(json.dumps(r) for r in records))
            self.assertEqual(ec.failed_ids(d, "b"), [2])
            self.assertEqual(ec.final_statuses(d, "b", {1, 2, 3}), {1: "ok", 2: "unconfirmed_empty", 3: "empty"})

    def test_unknown_cost_has_explicit_coverage(self):
        row = {"_n": 50, "_cost": {}, "items": 50}
        report = ec.merge_rows("model", [row])
        self.assertIsNone(report["tokens_per_obs"])
        self.assertEqual(report["cost_coverage"]["tokens"], {"items": 0, "total_items": 50, "value": None})


if __name__ == "__main__":
    unittest.main()
