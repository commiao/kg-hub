import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from tools import extract_bench as xb
from utils import bench_reference as ref
from utils import cli_runtime as rt


def response(text="", **extra):
    return {"text": text, "error": None, "stop": None, "credits": None, "tokens": None, "ms": 1, **extra}


class ChunkRuntimeTest(unittest.TestCase):
    items = [{"id": i, "episode_body": "public synthetic data"} for i in range(1, 5)]

    def test_401_403_never_split_and_counterexample_generated_403_is_valid(self):
        for status in (401, 403):
            call = mock.Mock(return_value=response(error="denied", status_code=status))
            _, stats = xb.run_chunk(call, self.items, [], lambda *a: None)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(stats["infrastructure_failure"]["kind"], "auth")
        call = mock.Mock(return_value=response(json.dumps({"items": [{"id": i, "note": "HTTP 403"} for i in range(1, 5)]})))
        got, stats = xb.run_chunk(call, self.items, [], lambda *a: None)
        self.assertEqual(len(got), 4)
        self.assertIsNone(stats["infrastructure_failure"])

    def test_service_errors_retry_same_batch_with_bounded_backoff(self):
        for status in (429, 500, 503):
            call = mock.Mock(return_value=response(error="service rejected", status_code=status))
            pauses = []
            _, stats = xb.run_chunk(call, self.items, [], lambda *a: None, sleep=pauses.append)
            self.assertEqual(call.call_count, 3)
            self.assertEqual(len({args.args[0] for args in call.call_args_list}), 1)
            self.assertEqual(pauses, [1, 2])
            self.assertIsNotNone(stats["infrastructure_failure"])

    def test_missing_items_still_split(self):
        def call(query):
            rows = json.loads(query.split("观测：", 1)[1])
            return response(json.dumps({"items": [{"id": rows[0]["id"]}]}))
        got, stats = xb.run_chunk(call, self.items, [], lambda *a: None)
        self.assertEqual(set(got), {1, 2, 3, 4})
        self.assertGreater(stats["calls"], 1)
        self.assertFalse(stats["tokens_known"])


class QueueRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        corpus = self.root / "corpus"
        corpus.mkdir()
        for name in ("edges.jsonl", "archived.json", "vec-minilm.npy"):
            (corpus / name).write_text("[]")
        for name in ("background.json", "background-vec.npy"):
            (self.source / name).write_text("[]")
        self.items = [{"id": i, "name": f"obs-{i}", "episode_body": f"[DISCOVERY] public title {i}\nbody"} for i in range(1, 9)]
        ref.write(self.source / "batch-b.json", {"items": self.items, "lookahead": []})
        ref.write(self.source / "baseline-b.json", {"items": []})
        (self.source / "questions.tsv").write_text("".join(f"{i}\tzh{i}\ten{i}\n" for i in range(1, 9)))
        self.manifest = {"bench_id": "synthetic", "corpus": str(corpus), "parts": [
            {"part": "b", "baseline_model": "base", "items": 8}]}

    def run_model(self, adapter, **extra):
        with mock.patch.dict(xb.ADAPTERS, {"claude": adapter}):
            return xb.run_models(self.source, self.manifest, ["m"], executor="claude", out_root=self.root / "runs",
                tag="r", limit=None, chunk=1, parallel=2, proxy_mode="env-only", **extra)

    def test_preflight_failure_sends_no_private_batch_and_has_cost_receipt(self):
        queries = []
        def adapter(model, system, query, **kw):
            queries.append(query)
            return response(error="API Error: 403 forbidden")
        with self.assertRaises(rt.InfrastructureFailure):
            self.run_model(adapter)
        self.assertEqual(len(queries), 1)
        self.assertNotIn("观测：", queries[0])
        d = next((self.root / "runs").iterdir())
        self.assertFalse((d / "run.json").exists())
        receipt = ref.read(d / "run-aborted.json")
        self.assertEqual(receipt["preflight"]["cost"]["calls"], 1)
        self.assertFalse(receipt["preflight"]["cost"]["credits_known"])
        self.assertEqual(receipt["extraction_cost"]["calls"], 0)
        with self.assertRaisesRegex(SystemExit, "infrastructure/canceled run"):
            xb.load_run(d)
        self.assertFalse(list(self.root.rglob("admissions")))

    def test_auth_stops_queued_native_calls_and_preserves_inflight(self):
        started, release = threading.Event(), threading.Event()
        queries = []
        def adapter(model, system, query, **kw):
            if "观测：" not in query:
                return response(query, actual_models=[model])
            rows = json.loads(query.split("观测：", 1)[1])
            queries.append(rows[0]["id"])
            if rows[0]["id"] == 1:
                started.wait(2)
                release.set()
                return response(error="API Error: 401 denied")
            started.set()
            release.wait(2)
            return response(json.dumps({"items": [{"id": rows[0]["id"]}]}), actual_models=[model])
        with self.assertRaises(rt.InfrastructureFailure):
            self.run_model(adapter)
        self.assertLessEqual(len(queries), 2)
        d = next((self.root / "runs").iterdir())
        self.assertEqual(len(list((d / "work").glob("*.json"))), len(queries))
        self.assertFalse((d / "run.json").exists())
        self.assertGreaterEqual(ref.read(d / "run-aborted.json")["queued_not_started"], 6)

    def test_v2_transient_attempt_empty_identity_then_success_verifies(self):
        calls = []
        def adapter(model, system, query, **kw):
            if "观测：" not in query:
                return response(query, actual_models=[model])
            calls.append(query)
            if len(calls) == 1:
                return response(error="HTTP 503 unavailable", actual_models=[])
            rows = json.loads(query.split("观测：", 1)[1])
            return response(json.dumps({"items": [{"id": row["id"]} for row in rows]}), actual_models=[model])
        d = self.run_model(adapter)[0]
        metadata = ref.read(d / "run.json")
        self.assertEqual(metadata["evidence"]["version"], ref.RUN_EVIDENCE_V2)
        ref.verify_run(d, metadata)
        self.assertEqual(metadata["calls"], 9)
        self.assertEqual(metadata["preflight"]["cost"]["calls"], 1)
        # Counterexample: mutating a service receipt into auth is rejected even
        # after a local reseal, rather than trusting the declared retry category.
        attempt = next(p for p in (d / "work").glob("*.json") if ref.read(p)["outcome"] == "service_error")
        data = ref.read(attempt)
        data["response_error"] = "HTTP 403 forbidden"
        attempt.write_text(json.dumps(data))
        resealed = ref.seal_run(d, {k: v for k, v in metadata.items() if k != "evidence"})
        with self.assertRaisesRegex(SystemExit, "terminal service"):
            ref.verify_run(d, resealed)

    def test_v1_history_keeps_original_verification_rules(self):
        def adapter(model, system, query, **kw):
            if "观测：" not in query:
                return response(query, actual_models=[model])
            rows = json.loads(query.split("观测：", 1)[1])
            return response(json.dumps({"items": [{"id": row["id"]} for row in rows]}), actual_models=[model])
        d = self.run_model(adapter)[0]
        metadata = ref.read(d / "run.json")
        old = {k: v for k, v in metadata.items() if k not in {"evidence", "native_evidence_version", "status", "preflight"}}
        old = ref.seal_run(d, old)
        self.assertEqual(old["evidence"]["version"], ref.RUN_EVIDENCE)
        ref.verify_run(d, old)
