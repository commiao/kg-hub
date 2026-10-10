"""Model attempts are durable before HTTP and unknown outcomes stay frozen."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from utils.model_attempt_journal import (
    ModelAttemptJournal, NeedsReconciliation, summarize_attempts,
)
import model_gateway_client as client_module


def fields():
    return dict(key="key-1", business_key="kg_hub.entity_extract",
                source_description="source", source_obs_id="id-1",
                step_id="step-1", request_digest="request-hash")



def record_failed_worker(journal, sd="source", sid="id-1"):
    count = journal.task_execution_summary(sd, sid)["execution_count"]
    execution_id = f"worker-{count + 1}"
    journal.begin_task_execution(sd, sid, execution_id,
                                 manual_command_id=execution_id if count else None)
    journal.finish_task_execution(sd, sid, execution_id, state="failed")


class JournalTests(unittest.TestCase):
    def test_existing_journal_adds_http_start_column_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "attempts.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE model_attempts (
                    idempotency_key TEXT PRIMARY KEY, business_key TEXT,
                    source_description TEXT, source_obs_id TEXT, step_id TEXT,
                    request_digest TEXT, phase TEXT, provider_call_started INTEGER,
                    result_json TEXT, gateway_identity TEXT,
                    gateway_http_status INTEGER, created_at TEXT, updated_at TEXT)""")
                db.execute("""INSERT INTO model_attempts
                    VALUES ('old', 'kg_hub.entity_extract', 'source', 'id-1',
                            'step-1', 'hash', 'unknown', NULL, NULL, NULL, NULL,
                            '2026-09-25T00:00:00+00:00',
                            '2026-09-25T00:00:00+00:00')""")
            journal = ModelAttemptJournal(path)
            old = journal.find_task("source", "id-1")[0]
            self.assertEqual(old["idempotency_key"], "old")
            self.assertIsNone(old["http_started_at"])

    def test_episode_context_is_immutable_across_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            self.assertIsNone(journal.read_episode_context(
                "source", "id-1", "operation", "input"))
            self.assertEqual(journal.save_episode_context(
                "source", "id-1", "operation", "input", ["episode-1"]),
                ["episode-1"])
            self.assertEqual(journal.save_episode_context(
                "source", "id-1", "operation", "input", ["episode-2"]),
                ["episode-1"])
            with self.assertRaises(RuntimeError):
                journal.read_episode_context("source", "id-1", "operation", "changed")

    def test_manual_grant_is_single_use_and_three_real_calls_are_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            journal.prepare(**fields())
            journal.update_gateway_status(
                "key-1", {"phase": "failed", "provider_call_started": True})
            keys = ["key-1"]
            for _ in range(2):
                record_failed_worker(journal)
                grant = journal.authorize_retry(
                    "source", "id-1", "step-1", "request-hash",
                    deadline_seconds=180)
                key = journal.claim_retry(
                    grant, source_description="source", source_obs_id="id-1",
                    step_id="step-1", request_digest="request-hash",
                    business_key="kg_hub.entity_extract", base_key="base-key",
                    deadline_seconds=180)
                self.assertNotIn(key, keys)
                keys.append(key)
                with self.assertRaises(RuntimeError):
                    journal.claim_retry(
                        grant, source_description="source", source_obs_id="id-1",
                        step_id="step-1", request_digest="request-hash",
                        business_key="kg_hub.entity_extract", base_key="base-key",
                        deadline_seconds=180)
                journal.update_gateway_status(
                    key, {"phase": "failed", "provider_call_started": True})
            self.assertEqual(len(journal.find_task("source", "id-1")), 3)
            with self.assertRaises(RuntimeError):
                journal.authorize_retry(
                    "source", "id-1", "step-1", "request-hash",
                    deadline_seconds=180)

    def test_successful_retry_replays_from_original_step_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            journal.prepare(**fields())
            journal.update_gateway_status(
                "key-1", {"phase": "failed", "provider_call_started": True})
            record_failed_worker(journal)
            grant = journal.authorize_retry("source", "id-1", "step-1",
                                            "request-hash", deadline_seconds=180)
            key = journal.claim_retry(
                grant, source_description="source", source_obs_id="id-1",
                step_id="step-1", request_digest="request-hash",
                business_key="kg_hub.entity_extract", base_key="base-key",
                deadline_seconds=180)
            journal.complete(key, '{"response":"saved answer"}')
            self.assertEqual(journal.prepare(**fields()),
                             '{"response":"saved answer"}')

    def test_unknown_admission_cannot_be_granted(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            journal.prepare(**fields())
            journal.update_gateway_status(
                "key-1", {"phase": "absent", "provider_call_started": None})
            with self.assertRaises(RuntimeError):
                journal.authorize_retry(
                    "source", "id-1", "step-1", "request-hash",
                    deadline_seconds=0)

    def test_timed_out_local_http_call_can_receive_one_manual_grant(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            journal.prepare(**fields())
            journal.start_http("key-1")
            journal.update_gateway_status(
                "key-1", {"phase": "absent", "provider_call_started": None})
            with self.assertRaises(RuntimeError):
                journal.authorize_retry(
                    "source", "id-1", "step-1", "request-hash",
                    deadline_seconds=3600)
            record_failed_worker(journal)
            grant = journal.authorize_retry(
                "source", "id-1", "step-1", "request-hash",
                deadline_seconds=0)
            self.assertTrue(grant)

    def test_three_failed_stages_in_one_execution_do_not_exhaust_task(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            for key, step, stage in (
                ("node-call", "node-step", "node_extraction"),
                ("resolve-call", "resolve-step", "node_resolution"),
                ("edge-call", "edge-step", "edge_phase"),
            ):
                journal.prepare(**{**fields(), "key": key, "step_id": step,
                                   "request_digest": f"{step}-digest", "stage": stage})
                journal.start_http(key)
                journal.update_gateway_status(
                    key, {"phase": "failed", "provider_call_started": True})
            summary = summarize_attempts(journal.find_task("source", "id-1"),
                                         deadline_seconds=180)
            self.assertEqual(summary["failed_calls_total"], 3)
            self.assertEqual(summary["failed_calls_by_step"], {
                "node-step": 1, "resolve-step": 1, "edge-step": 1})
            record_failed_worker(journal)
            self.assertTrue(journal.authorize_retry(
                "source", "id-1", "edge-step", "edge-step-digest",
                deadline_seconds=180, expected_stage="edge_phase"))
            self.assertEqual(journal.task_execution_summary(
                "source", "id-1")["failed_attempts"], 1)

    def test_failures_in_distinct_graphiti_calls_do_not_share_retry_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            for key, step, stage in (
                ("node-call", "node-step", "node_extraction"),
                ("resolve-call", "resolve-step", "node_resolution"),
            ):
                journal.prepare(**{**fields(), "key": key, "step_id": step,
                                   "request_digest": f"{step}-digest", "stage": stage})
                journal.start_http(key)
                journal.update_gateway_status(
                    key, {"phase": "failed", "provider_call_started": True})
            record_failed_worker(journal)
            grant = journal.authorize_retry(
                "source", "id-1", "resolve-step", "resolve-step-digest",
                deadline_seconds=180, expected_stage="node_resolution")
            third_key = journal.claim_retry(
                grant, source_description="source", source_obs_id="id-1",
                step_id="resolve-step", request_digest="resolve-step-digest",
                business_key="kg_hub.entity_extract", base_key="base",
                deadline_seconds=180, stage="node_resolution")
            rows = journal.find_task("source", "id-1")
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[-1]["idempotency_key"], third_key)
            self.assertEqual(rows[-1]["stage"], "node_resolution")

    def test_gateway_wire_step_maps_to_local_request_and_manual_alias(self):
        from utils.reconciliation_mailbox import task_uuid

        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp) / "ingest.jsonl"
            journal = ModelAttemptJournal(Path(temp) / "model-attempts.sqlite3")
            journal.prepare(**{**fields(), "stage": "node_extraction"})

            class Request:
                def __init__(self, content):
                    self.content = content
                    self.headers = {}

                async def aread(self):
                    return self.content

            request = Request(b"serialized request")
            with patch.dict("os.environ", {
                    "KG_HUB_INGEST_BACKUP_PATH": str(backup)}):
                attempt_token = client_module._wire_attempt.set(("key-1", "step-1"))
                task_token = client_module._business_task.set(("source", "id-1"))
                try:
                    asyncio.run(client_module.gateway_task_correlation_request_hook(request))
                finally:
                    client_module._business_task.reset(task_token)
                    client_module._wire_attempt.reset(attempt_token)
            wire_id = __import__("hashlib").sha256(request.content).hexdigest()
            self.assertEqual(request.headers["X-Model-Gateway-Task-Ids"],
                             task_uuid("source", "id-1"))
            self.assertEqual(request.headers["X-Model-Gateway-Step-Id"], wire_id)
            self.assertEqual(journal.resolve_gateway_step("source", "id-1", wire_id),
                             {"local_step_id": "step-1",
                              "request_digest": "request-hash",
                              "stage": "node_extraction"})

            journal.prepare(**{**fields(), "key": "key-replay",
                               "stage": "node_extraction"})
            replay = Request(b"reserialized request")
            with patch.dict("os.environ", {
                    "KG_HUB_INGEST_BACKUP_PATH": str(backup)}):
                attempt_token = client_module._wire_attempt.set(("key-replay", "step-1"))
                task_token = client_module._business_task.set(("source", "id-1"))
                resume_token = client_module._resume.set({
                    "consumed": True, "step_id": "step-1",
                    "gateway_step_id": wire_id})
                try:
                    asyncio.run(client_module.gateway_task_correlation_request_hook(replay))
                finally:
                    client_module._resume.reset(resume_token)
                    client_module._business_task.reset(task_token)
                    client_module._wire_attempt.reset(attempt_token)
            replay_wire = __import__("hashlib").sha256(replay.content).hexdigest()
            self.assertEqual(replay.headers["X-Model-Gateway-Step-Id"], replay_wire)
            self.assertNotEqual(replay_wire, wire_id)
            self.assertEqual(journal.gateway_step_for_attempt("key-replay"), wire_id)
            self.assertEqual(journal.resolve_gateway_step("source", "id-1", wire_id),
                             {"local_step_id": "step-1",
                              "request_digest": "request-hash",
                              "stage": "node_extraction"})

    def test_attempt_summary_excludes_proven_preflight_and_cached_result(self):
        now = datetime(2026, 9, 26, tzinfo=timezone.utc)
        old = (now - timedelta(minutes=30)).isoformat()
        rows = [
            {"step_id": "step-a", "phase": "failed", "provider_call_started": 1,
             "result_json": None, "created_at": old},
            {"step_id": "step-a", "phase": "preflight", "provider_call_started": 0,
             "result_json": None, "created_at": old},
            {"step_id": "step-b", "phase": "completed", "provider_call_started": 1,
             "result_json": "{}", "created_at": old},
            {"step_id": "step-c", "phase": "absent", "provider_call_started": None,
             "result_json": None, "created_at": old},
        ]
        summary = summarize_attempts(rows, deadline_seconds=60, now=now)
        self.assertEqual(summary["failed_calls_by_step"], {"step-a": 1})
        self.assertEqual(summary["cached_model_steps"], 1)
        self.assertTrue(summary["admission_unknown"])
        self.assertTrue(summary["unknown_without_http_evidence"])

    def test_local_http_start_times_out_even_if_gateway_admission_unknown(self):
        now = datetime(2026, 9, 26, tzinfo=timezone.utc)
        old = (now - timedelta(minutes=30)).isoformat()
        rows = [{"step_id": "step-a", "phase": "unknown",
                 "provider_call_started": None, "result_json": None,
                 "created_at": old, "http_started_at": old}]
        summary = summarize_attempts(rows, deadline_seconds=180, now=now)
        self.assertEqual(summary["failed_calls_by_step"], {"step-a": 1})
        self.assertTrue(summary["admission_unknown"])
        self.assertFalse(summary["unknown_without_http_evidence"])
        self.assertFalse(summary["in_flight"])

    def test_local_http_start_stays_in_flight_until_deadline(self):
        now = datetime(2026, 9, 26, tzinfo=timezone.utc)
        recent = (now - timedelta(seconds=30)).isoformat()
        summary = summarize_attempts([{"step_id": "step-a", "phase": "unknown",
            "provider_call_started": None, "result_json": None,
            "created_at": recent, "http_started_at": recent}],
            deadline_seconds=180, now=now)
        self.assertEqual(summary["failed_calls_by_step"], {})
        self.assertTrue(summary["in_flight"])

    def test_saved_model_response_is_not_failed_when_business_graph_is_pending(self):
        now = datetime(2026, 9, 26, tzinfo=timezone.utc)
        old = (now - timedelta(minutes=30)).isoformat()
        summary = summarize_attempts([{"step_id": "step-a", "phase": "unknown",
            "provider_call_started": None, "result_json": '{"answer":"saved"}',
            "created_at": old, "http_started_at": old}],
            deadline_seconds=180, now=now)
        self.assertEqual(summary["failed_calls_by_step"], {})
        self.assertEqual(summary["cached_model_steps"], 1)
        self.assertFalse(summary["in_flight"])

    def test_gateway_completed_without_local_response_receipt_counts_as_failure(self):
        now = datetime(2026, 9, 26, tzinfo=timezone.utc)
        rows = [
            {"idempotency_key": "gateway-completed", "step_id": "exact-step",
             "phase": "completed", "provider_call_started": 1,
             "result_json": None, "created_at": now.isoformat()},
            {"idempotency_key": "other-step-completed", "step_id": "other-step",
             "phase": "completed", "provider_call_started": 1,
             "result_json": None, "created_at": now.isoformat()},
        ]
        summary = summarize_attempts(rows, deadline_seconds=180, now=now,
                                     missing_receipt_step_id="exact-step")
        self.assertEqual(summary["failed_calls_by_step"], {"exact-step": 1})
        self.assertEqual(summary["failed_calls_total"], 1)
        self.assertEqual(summary["cached_model_steps"], 0)
        self.assertFalse(summary["in_flight"])

    def test_completed_gateway_step_without_local_receipt_gets_exact_manual_grant(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            journal.prepare(**{**fields(), "stage": "node_extraction"})
            journal.update_gateway_status(
                "key-1", {"phase": "completed", "provider_call_started": True})
            record_failed_worker(journal)
            grant = journal.authorize_retry(
                "source", "id-1", "step-1", "request-hash",
                deadline_seconds=180, expected_stage="node_extraction")
            replay_key = journal.claim_retry(
                grant, source_description="source", source_obs_id="id-1",
                step_id="step-1", request_digest="request-hash",
                business_key="kg_hub.entity_extract", base_key="base",
                deadline_seconds=180, stage="node_extraction")
            self.assertTrue(replay_key)

    def test_prepared_identity_survives_restart_and_freezes_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "attempts.sqlite3"
            ModelAttemptJournal(path).prepare(**fields())
            restarted = ModelAttemptJournal(path)
            self.assertEqual(restarted.find_task("source", "id-1")[0]["phase"],
                             "prepared")
            with self.assertRaises(NeedsReconciliation):
                restarted.prepare(**fields())

    def test_completed_response_is_reused_without_new_request(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            journal.prepare(**fields())
            journal.complete("key-1", '{"response":"model output"}')
            self.assertEqual(journal.prepare(**fields()),
                             '{"response":"model output"}')

    def test_gateway_unknown_remains_frozen_and_preflight_is_proven_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            journal.prepare(**fields())
            unknown = journal.update_gateway_status(
                "key-1", {"phase": "absent", "provider_call_started": None})
            self.assertIsNone(unknown.provider_call_started)
            with self.assertRaises(NeedsReconciliation):
                journal.prepare(**fields())
            preflight = journal.update_gateway_status(
                "key-1", {"phase": "preflight", "provider_call_started": False})
            self.assertIs(preflight.provider_call_started, False)
            self.assertIsNone(journal.prepare(**fields()))


class ClientJournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_resume_pays_only_authorized_failed_step(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []

            class Message:
                content = []

                def model_dump_json(self):
                    return '{"id":"response"}'

            async def model_call(*args, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise TimeoutError("model timed out")
                return Message()

            fake = type("Client", (), {})()
            fake.messages = type("Messages", (), {"create": model_call})()
            env = {"KG_HUB_INGEST_BACKUP_PATH": str(Path(temp) / "ingest.jsonl"),
                   "KG_HUB_MODEL_GATEWAY_TOKEN": "test-token",
                   "KG_HUB_LLM_MIN_INTERVAL_SEC": "0"}
            with patch.dict("os.environ", env), patch.object(
                    client_module.breakers, "assert_closed"), patch.object(
                    client_module, "query_gateway_attempt_status",
                    return_value={"version": 1, "phase": "failed",
                                  "provider_call_started": True}):
                client_module.install_gateway_request_contract(fake)
                with client_module.model_business_task("source", "id-1"), \
                        client_module.model_operation("ingest.episode", "operation"):
                    with self.assertRaises(NeedsReconciliation):
                        await fake.messages.create(
                            model="kg_hub.entity_extract",
                            messages=[{"role": "user", "content": "hello"}])
                journal = ModelAttemptJournal(Path(temp) / "model-attempts.sqlite3")
                step = journal.find_task("source", "id-1")[0]
                record_failed_worker(journal)
                grant = journal.authorize_retry(
                    "source", "id-1", step["step_id"], step["request_digest"],
                    deadline_seconds=180)
                with client_module.model_business_task("source", "id-1"), \
                        client_module.model_operation("ingest.episode", "operation"), \
                        client_module.model_manual_resume("source", "id-1",
                                                          step["step_id"], grant):
                    with self.assertRaises(NeedsReconciliation):
                        await fake.messages.create(
                            model="kg_hub.entity_extract",
                            messages=[{"role": "user", "content": "changed"}])
                    await fake.messages.create(
                        model="kg_hub.entity_extract",
                        messages=[{"role": "user", "content": "hello"}])
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0]["extra_headers"]["Idempotency-Key"],
                                calls[1]["extra_headers"]["Idempotency-Key"])

    async def test_timeout_with_unknown_gateway_evidence_stops_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []

            async def model_call(*args, **kwargs):
                calls.append(kwargs)
                raise TimeoutError("model timed out")

            fake = type("Client", (), {})()
            fake.messages = type("Messages", (), {"create": model_call})()
            with patch.dict("os.environ", {
                    "KG_HUB_INGEST_BACKUP_PATH": str(Path(temp) / "ingest.jsonl"),
                    "KG_HUB_MODEL_GATEWAY_TOKEN": "test-token",
                    "KG_HUB_LLM_MIN_INTERVAL_SEC": "0",
                    }), patch.object(client_module.breakers, "assert_closed"), patch.object(
                    client_module, "query_gateway_attempt_status",
                    return_value={"version": 1, "phase": "absent",
                                  "provider_call_started": None}):
                client_module.install_gateway_request_contract(fake)
                with client_module.model_business_task("source", "id-1"), \
                        client_module.model_operation("ingest.episode", "operation"):
                    with self.assertRaises(NeedsReconciliation):
                        await fake.messages.create(model="kg_hub.entity_extract",
                                                   messages=[{"role": "user", "content": "hello"}])
            self.assertEqual(len(calls), 1)
            journal = ModelAttemptJournal(Path(temp) / "model-attempts.sqlite3")
            row = journal.find_task("source", "id-1")[0]
            self.assertEqual(row["phase"], "absent")
            self.assertIsNone(row["provider_call_started"])
            self.assertIsNotNone(row["http_started_at"])


class UnchangedGatewayStatusTests(unittest.TestCase):
    """2026-10-10: an unchanged status re-read every ~10s was rewritten every time."""

    def test_unchanged_status_is_not_written_and_a_change_is(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "attempts.sqlite3"
            journal = ModelAttemptJournal(path)
            journal.prepare(**fields())
            status = {"phase": "unknown", "provider_call_started": None,
                      "identity": "gw-1", "http_status": 503, "checked_at": "t1"}
            journal.update_gateway_status("key-1", status)
            watcher = sqlite3.connect(path)
            self.addCleanup(watcher.close)
            version = lambda: watcher.execute("PRAGMA data_version").fetchone()[0]
            before, updated = version(), journal.find_task("source", "id-1")[0]["updated_at"]
            again = journal.update_gateway_status("key-1", {**status, "checked_at": "t2"})
            self.assertEqual((again.step_id, again.phase, again.provider_call_started),
                             ("step-1", "unknown", None))
            self.assertEqual(version(), before)
            self.assertEqual(journal.find_task("source", "id-1")[0]["updated_at"], updated)
            journal.update_gateway_status("key-1", {**status, "phase": "failed"})
            self.assertNotEqual(version(), before)
            self.assertEqual(journal.find_task("source", "id-1")[0]["phase"], "failed")

    def test_unknown_key_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ModelAttemptJournal(Path(temp) / "attempts.sqlite3")
            result = journal.update_gateway_status("missing", {"phase": "failed"})
            self.assertEqual(result.step_id, "unknown")


class AttemptStatusPacerTests(unittest.TestCase):
    def setUp(self):
        from utils.model_attempt_journal import AttemptStatusPacer
        self.now = [0.0]
        self.pacer = AttemptStatusPacer(clock=lambda: self.now[0])

    def test_unchanged_answers_double_up_to_the_gateway_recheck_ceiling(self):
        answer = {"phase": "unknown", "provider_call_started": None, "checked_at": 1}
        delays = []
        for i in range(8):
            self.assertTrue(self.pacer.due("k"))
            delays.append(self.pacer.observed("k", {**answer, "checked_at": i}))
            self.assertFalse(self.pacer.due("k"))
            self.now[0] += delays[-1]
        self.assertEqual(delays, [10, 20, 40, 80, 160, 300, 300, 300])

    def test_a_changed_answer_resets_and_keys_are_independent(self):
        self.pacer.observed("k", {"phase": "unknown"})
        self.now[0] += 10
        self.pacer.observed("k", {"phase": "unknown"})
        self.now[0] += 20
        self.assertEqual(self.pacer.observed("k", {"phase": "failed"}), 10)
        self.assertTrue(self.pacer.due("other"))


class JournalSynchronousTests(unittest.TestCase):
    """2026-10-11 user decision (T-0234): model-attempts.sqlite3 runs at NORMAL."""

    def test_every_connection_to_model_attempts_uses_normal(self):
        from utils import journal_prune
        from utils.graphiti_stage_adapter import StageArtifactStore
        from utils.model_attempt_journal import JOURNAL_SYNCHRONOUS
        self.assertEqual(JOURNAL_SYNCHRONOUS, "NORMAL")
        normal = 1   # PRAGMA synchronous: 0 OFF, 1 NORMAL, 2 FULL
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model-attempts.sqlite3"
            journal = ModelAttemptJournal(path)
            with journal._connect() as db:
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], normal)
            with StageArtifactStore(path)._connect() as db:
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], normal)
            db = journal_prune._connect(path)
            try:
                self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], normal)
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
