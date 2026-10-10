"""An answer that fails the caller's schema is not replayed forever.

obs 8762 / 11459 (2026-10-09/10): a paid extraction answer missed a required
field. It was saved as completed, so every retry replayed it from the journal
and failed identically in ~2 s, without ever asking the model again.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from utils.model_attempt_journal import ModelAttemptJournal, summarize_attempts

ANSWER = {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'logical',
          'content': [{'type': 'tool_use', 'id': 't1', 'name': 'out', 'input': {'items': []}}],
          'stop_reason': 'tool_use', 'stop_sequence': None,
          'usage': {'input_tokens': 2, 'output_tokens': 1}}
BODY = {'model': 'kg_hub.entity_extract', 'max_tokens': 100,
        'messages': [{'role': 'user', 'content': 'hello'}]}

try:
    from graphiti_client import SingleAttemptAnthropicClient
    from graphiti_core.llm_client.client import ModelSize
    from pydantic import BaseModel, ValidationError
except ImportError:  # the lightweight host test environment omits graphiti-core
    SingleAttemptAnthropicClient = None


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = ModelAttemptJournal(Path(self.temp.name) / 'j.sqlite3')
        self.args = dict(business_key=BODY['model'], source_description='s', source_obs_id='1',
                         step_id='step', request_digest='digest', queue_owned=True)
        self.journal.prepare(key='k1', **self.args)
        self.journal.complete('k1', json.dumps(ANSWER))

    def tearDown(self):
        self.temp.cleanup()

    def test_a_rejected_answer_is_not_replayed_under_a_new_key(self):
        self.assertIsNotNone(self.journal.prepare(key='k2', **self.args))   # before: replayed
        self.assertTrue(self.journal.reject_result('s', '1', 'step', 'digest'))
        self.assertIsNone(self.journal.prepare(key='k3', **self.args))      # after: ask again
        self.assertFalse(self.journal.reject_result('s', '1', 'step', 'digest'))  # idempotent

    def test_a_rejected_answer_counts_as_one_failed_call(self):
        self.journal.reject_result('s', '1', 'step', 'digest')
        summary = summarize_attempts(self.journal.find_task('s', '1'), deadline_seconds=180)
        self.assertEqual((summary['failed_calls_total'], summary['cached_model_steps']), (1, 0))

    def test_release_tool_treats_it_as_settled_without_result(self):
        from tools import release_held as H
        self.assertEqual(H.classify(['rejected']), H.RESET)
        self.assertEqual(H.classify(['completed', 'rejected']), H.RESUME)


class SinkTests(unittest.IsolatedAsyncioTestCase):
    async def run_call(self, journal, cached):
        import model_gateway_client as mgc
        if cached:
            journal.prepare(key='old', business_key=BODY['model'], source_description='s',
                            source_obs_id='1', step_id='x', request_digest='y', queue_owned=True)

        async def queued(*args):
            from anthropic.types import Message
            return Message.model_validate(ANSWER)
        client = SimpleNamespace(messages=SimpleNamespace(create=mock.AsyncMock()))
        mgc.install_gateway_request_contract(client, queue_transport=queued)
        with mock.patch.object(mgc, 'journal_from_backup_env', return_value=journal), \
             mock.patch.object(mgc, 'gateway_token', return_value='fixture'):
            with mgc.model_business_task('s', '1'), mgc.model_operation('ingest.episode', '1'), \
                 mgc.capture_model_result() as sink:
                await client.messages.create(**BODY)
                second = None
                if cached:
                    with mgc.model_operation('ingest.episode', '2'), mgc.capture_model_result() as again:
                        await client.messages.create(**BODY)       # replayed by step identity
                        second = dict(again)
        return sink, second

    async def test_fresh_and_replayed_answers_are_both_traceable(self):
        import model_gateway_client as mgc
        with tempfile.TemporaryDirectory() as root:
            journal = ModelAttemptJournal(Path(root) / 'j.sqlite3')
            sink, replay = await self.run_call(journal, cached=True)
            self.assertIs(sink['journal'], journal)
            self.assertEqual(sink['step_id'], replay['step_id'])
            self.assertTrue(await mgc.reject_model_result(replay))     # the replayed row
            self.assertFalse(await mgc.reject_model_result(replay))    # nothing left to replay
            self.assertFalse(await mgc.reject_model_result({}))


@unittest.skipUnless(SingleAttemptAnthropicClient is not None, "graphiti-core unavailable")
class GenerateResponseTests(unittest.IsolatedAsyncioTestCase):
    def client(self, answer, sink_key='k'):
        import model_gateway_client as mgc
        journal = mock.Mock()
        journal.reject_result.return_value = True
        client = object.__new__(SingleAttemptAnthropicClient)
        client.max_tokens = 10

        async def generate(*a, **k):
            mgc._note_model_result(journal, ('s', '1'), sink_key, 'digest')
            return answer, 1, 1
        client._generate_response = generate
        client.token_tracker = SimpleNamespace(record=lambda *a: None)
        return client, journal

    async def test_schema_failure_rejects_the_answer_and_still_raises(self):
        class Out(BaseModel):
            entity_type_id: int
        client, journal = self.client({'name': 'x'})
        with self.assertRaises(ValidationError):
            await client.generate_response([], response_model=Out, model_size=ModelSize.medium)
        journal.reject_result.assert_called_once_with('s', '1', 'k', 'digest')

    async def test_a_valid_answer_is_kept(self):
        class Out(BaseModel):
            entity_type_id: int
        client, journal = self.client({'entity_type_id': 1})
        self.assertEqual(await client.generate_response([], response_model=Out), {'entity_type_id': 1})
        journal.reject_result.assert_not_called()


if __name__ == '__main__':
    unittest.main()
