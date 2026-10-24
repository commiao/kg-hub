"""Native v5 reference lifecycle: local fake adapters, no model/graph calls."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from tools import extract_bench as xb
from tools import extract_campaign as campaign
from utils import bench_reference as ref
from utils import semantic_regression as semantic


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1))


def fake_embed(texts, cache_dir=None):
    out = []
    for text in texts:
        seed = int(hashlib.sha256(text[:6].encode()).hexdigest()[:8], 16)
        values = np.random.default_rng(seed).normal(size=16)
        out.append(values / np.linalg.norm(values))
    return np.asarray(out, dtype=np.float32).reshape((-1, 16))


def answer(obs):
    return {"id": obs["id"], "entities": [
        {"name": "A", "type": "Concept", "summary": "A records the source-supported behavior of the synthetic observation."},
        {"name": "B", "type": "Tool", "summary": "B provides the source-supported behavior in this deterministic fixture."}],
        "edges": [{"source": "A", "target": "B", "name": "relates_to", "fact": fact} for fact in
                  ("合成观测标题" + str(obs["id"]), "Synthetic observation title " + str(obs["id"]),
                   "A relates to B because this synthetic observation explicitly records the supported behavior.")]}


def fake_adapter(model, system, query, **kwargs):
    if "观测：" not in query:
        return {"text": query, "error": None, "stop": None, "credits": None, "tokens": 2,
                "tokens_known": True, "ms": 1, "actual_models": [model]}
    obs = json.loads(query.split("观测：", 1)[1])
    return {"text": json.dumps({"items": [answer(it) for it in obs]}, ensure_ascii=False),
            "error": None, "stop": "end_turn", "credits": None, "tokens": 100, "tokens_known": True,
            "ms": 5, "actual_models": [model]}


class ReferenceLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        corpus = self.root / "corpus"
        corpus.mkdir()
        (corpus / "archived.json").write_text("[]")
        (corpus / "edges.jsonl").write_text(json.dumps({"f": "Corpus distractor", "e": ["ep"]}) + "\n")
        np.save(corpus / "vec-minilm.npy", fake_embed(["Corpus distractor"]))
        parts = []
        for n, model in enumerate(("haiku", "opus", "sonnet")):
            name = f"b{n}"
            ids = list(range(1 + n * 12, 13 + n * 12))
            items = [{"id": i, "name": f"obs-{i}", "source_description": "synthetic-source",
                      "reference_time": "2026-01-01T00:00:00Z", "source_obs_id": str(i),
                      "episode_body": f"[DISCOVERY] 合成观测标题{i}\nA relates to B because this synthetic observation explicitly records the supported behavior."}
                     for i in ids]
            dump(self.source / f"batch-{name}.json", {"batch_id": name, "prompt_version": "source-capture-v3",
                 "items": items, "lookahead": [{"id": 100 + n, "title": f"follow-up-{n}"}]})
            dump(self.source / f"baseline-{name}.json", {"items": [answer(it) for it in items]})
            parts.append({"part": name, "baseline_model": model, "items": 12})
        (self.source / "questions.tsv").write_text("".join(
            f"{i}\t合成观测标题{i}？\tSynthetic observation title {i}?\n" for i in range(1, 37)))
        (self.source / "background.json").write_text("[]")
        np.save(self.source / "background-vec.npy", np.zeros((0, 16), dtype=np.float32))
        self.manifest = {"tool": xb.TOOL, "bench_id": "legacy-source", "chain": "c8", "items": 36,
                         "questions": 36, "corpus": str(corpus), "parts": parts}
        dump(self.source / "manifest.json", self.manifest)
        self.original = {str(p): ref.sha(p) for p in self.root.rglob("*") if p.is_file()}
        with mock.patch.dict(xb.ADAPTERS, {"claude": fake_adapter}):
            self.run_dir = xb.run_models(self.source, self.manifest, ["claude-reference"], executor="claude",
                out_root=self.root / "runs", tag="baseline", limit=30, chunk=10, parallel=2)[0]
        self.run = ref.read(self.run_dir / "run.json")

    def freeze(self, name="new-v5"):
        with mock.patch("sys.stdout"):
            self.assertEqual(xb.main(["baseline", "--bench", str(self.source), "--run", str(self.run_dir),
                "--bench-id", name, "--root", str(self.root / "benches")]), 0)
        path = self.root / "benches" / name
        return path, ref.read(path / "manifest.json")

    def candidate(self, p, m, model="gpt-candidate", limit=None):
        with mock.patch.dict(xb.ADAPTERS, {"codex": fake_adapter}):
            return xb.run_models(p, m, [model], executor="codex", out_root=self.root / "candidates",
                tag=model + str(limit), limit=limit, chunk=10, parallel=1)[0]

    def semantic_receipt(self, p, m, run):
        """Isolated synthetic native lifecycle; no real policy or model review."""
        suite = semantic.create_suite(p, m, {"suite_id": "synthetic-native", "revision": 1,
            "scope": "private-production", "selection_rationale": "Fixed synthetic relationship criterion.",
            "cases": [{"case_id": "source-supported-relationship", "obs_id": run["ids"][0], "critical": True,
                       "known_failure_run": run["dir"],
                       "rationale": "Keep the synthetic relationship's source and target.", "criteria": [
                           {"criterion_id": "relationship", "requirement": "A relates to B as recorded in the source."}]}]})
        path = self.root / "native-suite.json"
        dump(path, suite)
        semantic.activate(self.root / "state", path, reviewer="synthetic fixture declaration", confirm_suite_sha=suite["suite_sha256"])
        review = semantic.review_sheet(p, m, run, suite)
        review["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        review["reviewer"] = {"name": "synthetic independent reviewer", "method": "human", "identity_evidence": "self-declared",
                              "independence_declaration": "Synthetic judgment supplied only for an isolated test fixture."}
        review["cases"][0]["judgments"][0].update({"verdict": "pass", "source_quote": "A relates to B because this synthetic observation",
            "claim_pointer": "/edges/2/fact", "claim_quote": "A relates to B because this synthetic observation",
            "responsibility_judgment": "A is the source and B is the target of the relationship.",
            "explanation": "This synthetic output preserves the complete source-supported relationship."})
        return semantic.check_review(p, m, run, suite, review)

    def test_public_cli_native_reference_to_admission_and_campaign_gate(self):
        p, m = self.freeze()
        self.assertEqual(ref.verify_reference(p, m)["prompt_version"], "agent-v5+bench2")
        expected = xb.select_ids(xb._bench_items(self.source, self.manifest)[0], 30)
        self.assertEqual(m["selection_order"], expected)
        self.assertEqual({part["comparison_stratum"] for part in m["parts"]}, {"haiku", "opus", "sonnet"})
        for name in ("background.json", "background-vec.npy", "questions.tsv"):
            self.assertEqual((p / name).read_bytes(), (self.source / name).read_bytes())
        for part in m["parts"]:
            original = ref.read(self.source / f"batch-{part['part']}.json")
            actual = ref.read(p / f"batch-{part['part']}.json")
            self.assertEqual(actual["lookahead"], original["lookahead"])
            self.assertEqual(actual["items"], [it for it in original["items"] if it["id"] in expected])
        smoke = xb.load_run(self.candidate(p, m, limit=10))
        self.assertEqual(smoke["ids"], expected[:10])
        candidate = self.candidate(p, m)
        loaded = xb.load_run(candidate)
        semantic_receipt = self.semantic_receipt(p, m, loaded)
        semantic_file = self.root / "semantic-receipt.json"
        dump(semantic_file, semantic_receipt)
        sheet, key = xb.blind_sheet(p, m, [loaded], n=10)
        for row in sheet:
            row["verdict"] = "ok"
        sheet_file = self.root / "blind.json"
        dump(sheet_file, sheet); dump(Path(str(sheet_file) + ".key.json"), key)
        with mock.patch.object(xb, "embed", fake_embed), mock.patch.dict("os.environ", {"KG_HUB_BENCH_ROOT": str(self.root / "state")}), mock.patch("sys.stdout"):
            xb._corpus_cache.clear()
            self.assertEqual(xb.main(["admit", "--bench", str(p), "--run", str(candidate), "--sheet", str(sheet_file),
                                     "--semantic-review", str(semantic_file)]), 0)
            record = xb.latest_admission("gpt-candidate")
            self.assertEqual(record["verdict"], "pass")
            self.assertEqual(sum("不低于基线" in check["check"] for check in record["checks"]), 6)
            gate = campaign.admission_gate(["gpt-candidate"], "agent-v5", xb.rules_sha())
            self.assertTrue(gate["gpt-candidate"]["ok"])
        self.assertTrue(all(ref.sha(Path(path)) == value for path, value in self.original.items()))
        # Frozen reference remains self-contained after archiving its native run.
        shutil.rmtree(self.run_dir)
        self.assertIsNotNone(ref.verify_reference(p, m))

    def test_config_mismatch_is_not_a_model_failure_record(self):
        p, m = self.freeze()
        run = xb.load_run(self.candidate(p, m))
        with mock.patch.dict("os.environ", {"KG_HUB_BENCH_ROOT": str(self.root / "state")}), \
                mock.patch.object(xb, "score_many", side_effect=AssertionError("must fail before scoring")):
            with self.assertRaisesRegex(SystemExit, "baseline/rules versions incompatible"):
                xb.admit(self.source, self.manifest, run, sheet=[], key={})
            self.assertFalse((self.root / "state/admissions").exists())

    def test_legacy_register_cannot_issue_current_rules_admission(self):
        results = self.root / "manual"
        results.mkdir()
        shutil.copyfile(self.source / "baseline-b0.json", results / "results-b0.json")
        xb.register_run(self.source, self.manifest, results, model="pretend", executor="manual", prompt_version="agent-v4")
        with mock.patch.object(xb, "score_many", side_effect=AssertionError("must not score")):
            with self.assertRaisesRegex(SystemExit, "candidate rules are not current"):
                xb.admit(self.source, self.manifest, xb.load_run(results), write=False)

    def test_reference_rejects_changed_native_result_or_work(self):
        for pattern in ("results-*.json", "work/*.json", "rules.md", "input-data.json"):
            path = next(self.run_dir.glob(pattern))
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.assertRaisesRegex(SystemExit, "artifact changed|rules snapshot"):
                self.freeze("tamper")
            path.write_bytes(original)

    def test_changed_ids_cannot_be_hidden_in_updated_native_seal(self):
        self.run["ids"] = list(reversed(self.run["ids"]))
        self.run["evidence"]["metadata_sha256"] = ref.digest({k: v for k, v in self.run.items() if k != "evidence"})
        dump(self.run_dir / "run.json", self.run)
        with self.assertRaisesRegex(SystemExit, "IDs differ"):
            self.freeze()

    def test_old_version_cannot_be_relabelled_with_current_sha(self):
        self.run["prompt_version"] = "agent-v4+bench2"
        self.run["evidence"]["metadata_sha256"] = ref.digest({k: v for k, v in self.run.items() if k != "evidence"})
        dump(self.run_dir / "run.json", self.run)
        with self.assertRaisesRegex(SystemExit, "version differs"):
            self.freeze()

    def test_full_system_prompt_version_and_body_must_match_current(self):
        system, version = xb.bench_system_prompt()
        for changed_system, changed_version in ((system, "agent-v5+bench3"), (system + "\nchanged addendum", version)):
            with mock.patch.object(xb, "bench_system_prompt", return_value=(changed_system, changed_version)):
                with self.assertRaisesRegex(SystemExit, "current rules|system prompt"):
                    self.freeze()
        p, m = self.freeze()
        candidate = xb.load_run(self.candidate(p, m))
        for changed_system, changed_version in ((system, "agent-v5+bench3"), (system + "\nchanged addendum", version)):
            with mock.patch.object(xb, "bench_system_prompt", return_value=(changed_system, changed_version)), \
                    mock.patch.object(xb, "score_many", side_effect=AssertionError("must not score")):
                with self.assertRaisesRegex(SystemExit, "complete prompt|system prompts"):
                    xb.admit(p, m, candidate, write=False)

    def test_three_original_sample_strata_are_checked_against_real_single_reference(self):
        p, m = self.freeze()
        run = xb.load_run(self.candidate(p, m))
        with mock.patch.object(xb, "embed", fake_embed):
            xb._corpus_cache.clear()
            rows = xb.score_many(p, m, [(run["label"], run["results"], {})], ids=run["ids"])
        self.assertEqual(len(rows), 8)
        self.assertTrue(all("claude-reference参考｜原样本组" in row["label"] for row in rows[2:]))
        # Overall quality still looks excellent; one original stratum drops six
        # points and must independently fail the unchanged five-point gate.
        for row in rows:
            row["hit_zh_pct"] = row["hit_en_pct"] = 100
        rows[2]["hit_zh_pct"] = 94
        receipt = self.semantic_receipt(p, m, run)
        with mock.patch.object(xb, "score_many", return_value=rows), mock.patch.object(xb, "bench_root", return_value=self.root / "state"):
            record = xb.admit(p, m, run, write=False, semantic_review=receipt)
        self.assertEqual(record["verdict"], "fail")
        self.assertEqual(sum("不低于基线" in check["check"] for check in record["checks"]), 6)

    def test_missing_failed_manual_and_wrong_model_cannot_be_reference(self):
        for key, value in (("failed", [1]), ("execution_mode", "manual"), ("actual_models", []), ("actual_models", ["another-model"])):
            data = dict(self.run, **{key: value})
            data["evidence"] = dict(self.run["evidence"], metadata_sha256=ref.digest({k: v for k, v in data.items() if k != "evidence"}))
            dump(self.run_dir / "run.json", data)
            with self.assertRaises(SystemExit):
                self.freeze()
        dump(self.run_dir / "run.json", self.run)

    def test_input_questions_background_or_corpus_change_fails_before_creation(self):
        for path in (self.source / "batch-b0.json", self.source / "questions.tsv", self.source / "background.json",
                     Path(self.manifest["corpus"]) / "archived.json"):
            before = path.read_bytes()
            path.write_bytes(before + b" ")
            # JSON whitespace does not change model input, but byte-frozen retrieval
            # resources do. Change actual batch data for the observation case.
            if path.name == "batch-b0.json":
                value = ref.read(path); value["items"][0]["episode_body"] += " changed"
                dump(path, value)
            if path.name == "questions.tsv":
                path.write_bytes(before.replace("？".encode(), "变更？".encode(), 1))
            with self.assertRaises(SystemExit):
                self.freeze("changed")
            self.assertFalse((self.root / "benches/changed").exists())
            path.write_bytes(before)

    def test_frozen_reference_tamper_and_self_admission_are_rejected(self):
        p, m = self.freeze()
        self.assertRaisesRegex(SystemExit, "already exists", self.freeze)
        original = (p / "baseline-b0.json").read_bytes()
        (p / "baseline-b0.json").write_bytes(original + b" ")
        self.assertRaisesRegex(SystemExit, "artifact changed", xb._bench, str(p))
        (p / "baseline-b0.json").write_bytes(original)
        loaded = xb.load_run(self.candidate(p, m, model="claude-reference"))
        self.assertRaisesRegex(SystemExit, "self-reference", xb.admit, p, m, loaded, write=False)

    def test_invalid_counts_never_call_adapter(self):
        for bad in ({"limit": 0}, {"limit": -1}, {"chunk": 0}, {"parallel": 0}, {"chunk": -1}):
            options = {"limit": 10, "chunk": 10, "parallel": 1, **bad}
            adapter = mock.Mock(side_effect=AssertionError("model must not be called"))
            with mock.patch.dict(xb.ADAPTERS, {"codex": adapter}), self.assertRaises(SystemExit):
                xb.run_models(self.source, self.manifest, ["m"], executor="codex", out_root=self.root / "invalid",
                              tag="bad", **options)
            adapter.assert_not_called()

    def test_blind_duplicates_mutations_missing_entries_and_empty_error_notes_reject(self):
        import copy
        p, m = self.freeze()
        run = xb.load_run(self.candidate(p, m))
        sheet, key = xb.blind_sheet(p, m, [run], n=10)
        for entry in sheet:
            entry.update(verdict="ok", note="")
        xb.validate_blind_sheet(p, m, run, sheet, key)
        other = xb.load_run(self.candidate(p, m, model="other-candidate"))
        combined, combined_key = xb.blind_sheet(p, m, [run, other], n=10)
        for entry in combined:
            entry.update(verdict="ok", note="")
        xb.validate_blind_sheet(p, m, run, list(reversed(combined)), combined_key)
        duplicate = [copy.deepcopy(sheet[0]) for _ in range(10)]
        self.assertRaisesRegex(SystemExit, "duplicate", xb.blind_result, duplicate, key, run["label"])
        changes = [lambda s: s.pop(),
                   lambda s: s[0].update(episode_body="changed source"),
                   lambda s: s[0].update(lookahead=[]),
                   lambda s: s[0]["edges"][0].update(fact="changed output"),
                   lambda s: s[0].update(verdict="error", note=""),
                   lambda s: s[0].update(obs_id=s[1]["obs_id"])]
        for change in changes:
            bad = copy.deepcopy(sheet); change(bad)
            with self.assertRaises(SystemExit), mock.patch.object(xb, "score_many", side_effect=AssertionError("must not score")):
                xb.admit(p, m, run, sheet=bad, key=key)

    def test_rules_changed_during_call_do_not_relabel_used_prompt(self):
        rules = self.root / "mutable-rules.md"
        original = xb.PROMPT_PATH.read_bytes(); rules.write_bytes(original)
        real_prompt = xb.bench_system_prompt
        seen = []
        def mutating(model, system, query, **kwargs):
            if "观测：" not in query:
                return fake_adapter(model, system, query, **kwargs)
            seen.append(system)
            rules.write_bytes(original + b"\nUnrelated later edit.\n")
            return fake_adapter(model, system, query, **kwargs)
        with mock.patch.object(xb, "PROMPT_PATH", rules), \
                mock.patch.object(xb, "bench_system_prompt", side_effect=lambda: real_prompt(rules)), \
                mock.patch.dict(xb.ADAPTERS, {"claude": mutating}):
            run_dir = xb.run_models(self.source, self.manifest, ["claude-reference"], executor="claude",
                out_root=self.root / "changed-rules", tag="native", limit=30, chunk=10, parallel=1)[0]
        stored = ref.read(run_dir / "run.json")
        self.assertEqual(stored["prompt_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual((run_dir / "rules.md").read_bytes(), original)
        self.assertEqual(len(set(seen)), 1)
        ref.verify_run(run_dir, stored)

    def test_source_mutation_during_call_uses_single_frozen_input_snapshot(self):
        before = (self.source / "batch-b2.json").read_bytes()
        queries = []
        def mutating(model, system, query, **kwargs):
            queries.append(query)
            changed = json.loads(before); changed["items"][0]["episode_body"] = "injected later edit"
            dump(self.source / "batch-b2.json", changed)
            return fake_adapter(model, system, query, **kwargs)
        with mock.patch.dict(xb.ADAPTERS, {"claude": mutating}):
            run_dir = xb.run_models(self.source, self.manifest, ["claude-reference"], executor="claude",
                out_root=self.root / "changed-source", tag="native", limit=30, chunk=10, parallel=1)[0]
        self.assertTrue(all("injected later edit" not in query for query in queries))
        ref.verify_run(run_dir, ref.read(run_dir / "run.json"))
        (self.source / "batch-b2.json").write_bytes(before)


class IdentityAndSnapshotTest(unittest.TestCase):
    def test_claude_identity_missing_is_nonretryable(self):
        response = mock.Mock(returncode=0, stdout=json.dumps({"result": '{"items": []}', "is_error": False}), stderr="")
        with mock.patch("subprocess.run", return_value=response):
            result = xb.claude_call("claude-reference", "s", "q", cwd=Path("/tmp"))
        self.assertTrue(result["error"])
        self.assertFalse(result["retryable"])
        self.assertFalse(result["tokens_known"])
        called = mock.Mock(return_value=result)
        _, stats = xb.run_chunk(called, [{"id": 1, "episode_body": "x"}], [], lambda *args: None)
        self.assertEqual(stats["failed"], [1])
        self.assertEqual(called.call_count, 1)

    def test_duplicate_observations_in_actual_response_are_not_last_wins(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            xb.parse_items(json.dumps({"items": [{"id": 1}, {"id": 1}]}), [1])


if __name__ == "__main__":
    unittest.main()
