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
            grant = journal.authorize_retry(
                "source", "id-1", "step-1", "request-hash",
                deadline_seconds=0)
            self.assertTrue(grant)

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


if __name__ == "__main__":
    unittest.main()
