"""Graphiti's semantic parse/validation failures must never trigger paid retries."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from graphiti_client import SingleAttemptAnthropicClient
    from graphiti_core.llm_client.client import ModelSize
    from model_gateway_client import install_gateway_request_contract, model_operation
except ImportError:  # the lightweight host test environment omits graphiti-core
    SingleAttemptAnthropicClient = None
    ModelSize = None


@unittest.skipUnless(SingleAttemptAnthropicClient is not None, "graphiti-core unavailable")
class SingleAttemptTests(unittest.TestCase):
    def client(self, implementation):
        client = object.__new__(SingleAttemptAnthropicClient)
        client.max_tokens = 123
        client._generate_response = implementation

        class Tracker:
            def __init__(self):
                self.calls = []

            def record(self, *args):
                self.calls.append(args)

        client.token_tracker = Tracker()
        return client

    def test_parse_failure_propagates_after_one_attempt(self):
        calls = []

        class Messages:
            def __init__(self):
                self.calls = []

            async def create(self, **kwargs):
                self.calls.append(kwargs)

        class Transport:
            def __init__(self):
                self.messages = Messages()

        transport = install_gateway_request_contract(Transport())

        async def fails(*args):
            calls.append(args)
            await transport.messages.create(
                model="kg_hub.entity_extract", messages=[]
            )
            raise ValueError("invalid structured output")

        client = self.client(fails)
        with self.assertRaisesRegex(ValueError, "invalid structured output"):
            with model_operation(
                "test.single_attempt", "test:single-attempt:parse-failure"
            ):
                asyncio.run(client.generate_response(
                    [], model_size=ModelSize.medium, prompt_name="entity"
                ))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(transport.messages.calls), 1)
        keys = [
            call["extra_headers"]["Idempotency-Key"]
            for call in transport.messages.calls
        ]
        self.assertEqual(len(keys), 1)
        self.assertEqual(client.token_tracker.calls, [])

    def test_success_records_usage_once(self):
        calls = []

        async def succeeds(*args):
            calls.append(args)
            return {"entities": []}, 7, 3

        client = self.client(succeeds)
        result = asyncio.run(client.generate_response(
            [], model_size=ModelSize.medium, prompt_name="entity"
        ))
        self.assertEqual(result, {"entities": []})
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.token_tracker.calls, [("entity", 7, 3)])


if __name__ == "__main__":
    unittest.main()
