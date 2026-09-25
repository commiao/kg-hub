"""Model attempts are durable before HTTP and unknown outcomes stay frozen."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.model_attempt_journal import ModelAttemptJournal, NeedsReconciliation
import model_gateway_client as client_module


def fields():
    return dict(key="key-1", business_key="kg_hub.entity_extract",
                source_description="source", source_obs_id="id-1",
                step_id="step-1", request_digest="request-hash")


class JournalTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
