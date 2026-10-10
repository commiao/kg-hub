"""kg-hub follows the gateway's unified result mapping (credvault #68).

disposition: retry (same_key true/false) / paused / failed / reconciliation,
plus /v1/queue/cancel for a job kg-hub stops waiting for.
"""
import ast
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from utils import gateway_queue as gq
from utils.model_attempt_journal import ModelAttemptJournal, NeedsReconciliation

ROOT = Path(__file__).resolve().parents[1]
BODY = {'model': 'kg_hub.entity_extract', 'max_tokens': 100,
        'messages': [{'role': 'user', 'content': 'hello'}]}
ANSWER = {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'logical',
          'content': [{'type': 'text', 'text': 'answer'}], 'stop_reason': 'end_turn',
          'stop_sequence': None, 'usage': {'input_tokens': 2, 'output_tokens': 1}}


def job(state, error=None, **extra):
    return {'version': 1, 'job': {'request_key': 'k', 'business_key': BODY['model'],
                                  'state': state, 'response': ANSWER, 'error': error, **extra}}


async def run(statuses, cancel=None, **kwargs):
    seen = []

    def handle(req):
        seen.append(req.url.path)
        if req.url.path == '/v1/queue/cancel':
            code, body = cancel
            return httpx.Response(code, json=body)
        polls = [p for p in seen if p != '/v1/queue/cancel']
        return httpx.Response(200, json=statuses[min(len(polls), len(statuses)) - 1])
    result = await gq.execute_queued('http://model-gateway:39000', 'token', 'k', BODY,
                                     transport=httpx.MockTransport(handle), poll_seconds=0,
                                     held_poll_seconds=0, **kwargs)
    return result, seen


def classifier():
    from utils.model_attempt_journal import NeedsReconciliation as NR
    source = (ROOT / 'kg_hub_server.py').read_text('utf-8')
    fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
              and n.name == 'classify_extract_error')
    ns = {'NeedsReconciliation': NR}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<classifier>', 'exec'), ns)
    return ns['classify_extract_error']


class RetryWithNewKeyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gq._provider_wait.clear()

    async def test_definite_provider_5xx_is_a_settled_failure(self):
        with self.assertRaises(gq.QueueRetryWithNewKey) as caught:
            await run([job('failed', {'code': 'provider_error', 'http_status': 502,
                                      'disposition': 'retry', 'same_key': False})])
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(classifier()(caught.exception), 'upstream_error')

    async def test_queue_expiry_is_a_settled_failure(self):
        with self.assertRaises(gq.QueueRetryWithNewKey) as caught:
            await run([job('failed', {'code': 'queue_wait_expired', 'disposition': 'retry',
                                      'same_key': False})])
        self.assertEqual(caught.exception.status_code, 503)

    async def test_provider_429_still_classifies_as_rate_limited(self):
        with self.assertRaises(gq.QueueProviderRefused) as caught:
            await run([job('failed', {'code': 'provider_error', 'http_status': 429,
                                      'disposition': 'retry', 'same_key': False})])
        self.assertEqual(classifier()(caught.exception), 'rate_limited')

    async def test_only_new_key_retries_count_as_settled(self):
        # A failed job that claims the same key may be retried is not proof of
        # "no result": keep the conservative path.
        with self.assertRaises(gq.QueueOutcomeError) as caught:
            await run([job('failed', {'code': 'gateway_busy', 'http_status': 503,
                                      'disposition': 'retry', 'same_key': True})])
        self.assertNotIsInstance(caught.exception, gq.QueueRetryWithNewKey)

    async def test_unknown_and_terminal_outcomes_are_not_retried(self):
        for error in ({'code': 'provider_error', 'disposition': 'reconciliation'},
                      {'code': 'invalid_request', 'http_status': 400, 'disposition': 'failed'}):
            with self.assertRaises(gq.QueueOutcomeError) as caught:
                await run([job('failed' if error['disposition'] == 'failed' else 'reconciliation', error)])
            self.assertNotIsInstance(caught.exception, gq.QueueRetryWithNewKey)


class PausedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gq._provider_wait.clear()

    async def test_any_paused_hold_is_waited_out_and_signalled(self):
        views = []

        async def sleep(_):
            views.append(gq.provider_wait())
        held = job('queued', {'code': 'provider_circuit_open', 'disposition': 'paused'}, ready_at=99.0)
        with mock.patch.object(gq.asyncio, 'sleep', sleep):
            result, _ = await run([held] * 3 + [job('succeeded')], wait_seconds=0)
        self.assertEqual(result.content[0].text, 'answer')
        self.assertEqual(views[0]['retry_at'], 99.0)
        self.assertIsNone(gq.provider_wait())

    async def test_same_key_requeue_is_an_ordinary_wait(self):
        requeued = job('queued', {'code': 'provider_error', 'disposition': 'retry', 'same_key': True})
        with self.assertRaises(TimeoutError):
            await run([requeued], cancel=(404, {'error': {'code': 'not_found'}}), wait_seconds=0)
        self.assertIsNone(gq.provider_wait())


class CancelOnGiveUpTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_withdrawn_job_is_a_settled_failure(self):
        cancelled = job('failed', {'code': 'cancelled_by_caller', 'disposition': 'failed'})
        with self.assertRaises(gq.QueueCancelled) as caught:
            _, seen = await run([job('queued')], cancel=(200, cancelled), wait_seconds=0)
        self.assertEqual(caught.exception.job['error']['code'], 'cancelled_by_caller')

    async def test_a_running_job_is_waited_for_not_abandoned(self):
        result, seen = await run([job('queued'), job('running'), job('succeeded')],
                                 cancel=(409, {'error': {'code': 'idempotency_conflict'}}),
                                 wait_seconds=0)
        self.assertEqual(result.content[0].text, 'answer')
        self.assertIn('/v1/queue/cancel', seen)

    async def test_unknown_cancel_outcome_keeps_the_old_timeout(self):
        for answer in ((404, {'error': {'code': 'not_found'}}), (500, {})):
            with self.assertRaises(TimeoutError):
                await run([job('queued')], cancel=answer, wait_seconds=0)

    async def test_a_job_cancelled_by_someone_else_is_settled(self):
        with self.assertRaises(gq.QueueCancelled):
            await run([job('failed', {'code': 'cancelled_by_caller', 'disposition': 'failed'})])


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_settled_failures_are_not_reviewed(self):
        import model_gateway_client as mgc
        from utils.model_attempt_journal import summarize_attempts
        for exc_job, status in (({'state': 'failed', 'error': {'code': 'provider_error', 'http_status': 502,
                                  'disposition': 'retry', 'same_key': False}}, 502),
                                ({'state': 'failed', 'error': {'code': 'cancelled_by_caller',
                                  'disposition': 'failed'}}, 503)):
            with tempfile.TemporaryDirectory() as root:
                journal = ModelAttemptJournal(Path(root) / 'j.sqlite3')
                exc = (gq.QueueCancelled if exc_job['error']['code'] == 'cancelled_by_caller'
                       else gq.QueueRetryWithNewKey)(exc_job)

                async def queued(*args, exc=exc):
                    raise exc
                client = SimpleNamespace(messages=SimpleNamespace(create=mock.AsyncMock()))
                mgc.install_gateway_request_contract(client, queue_transport=queued)
                lookup = mock.Mock(side_effect=AssertionError('no reconciliation lookup'))
                with mock.patch.object(mgc, 'journal_from_backup_env', return_value=journal), \
                     mock.patch.object(mgc, 'gateway_token', return_value='fixture'), \
                     mock.patch.object(mgc, 'query_gateway_attempt_status', lookup):
                    with mgc.model_business_task('s', '1'), mgc.model_operation('ingest.episode', '1'):
                        with self.assertRaises(gq.QueueRetryWithNewKey):
                            await client.messages.create(**BODY)
                row = journal.find_task('s', '1')[0]
                self.assertEqual((row['phase'], row['provider_call_started']), ('failed', 0))
                self.assertEqual(summarize_attempts([row], deadline_seconds=180)['failed_calls_total'], 0)


class AdmissionRetryTests(unittest.TestCase):
    def rejection(self, error):
        return SimpleNamespace(status_code=429, body={'error': error})

    def test_fields_decide_when_present(self):
        import model_gateway_client as mgc
        self.assertTrue(mgc.is_local_admission_rejection(self.rejection(
            {'code': 'cost_limit_exceeded', 'message': 'whatever', 'disposition': 'retry', 'same_key': True})))
        self.assertFalse(mgc.is_local_admission_rejection(self.rejection(
            {'code': 'cost_limit_exceeded', 'message': '该业务并发请求已达到本地上限',
             'disposition': 'paused'})))
        self.assertFalse(mgc.is_local_admission_rejection(self.rejection(
            {'code': 'provider_error', 'disposition': 'retry', 'same_key': False})))

    def test_older_gateways_keep_the_message_check(self):
        import model_gateway_client as mgc
        self.assertTrue(mgc.is_local_admission_rejection(self.rejection(
            {'code': 'cost_limit_exceeded', 'message': '该业务并发请求已达到本地上限'})))


if __name__ == '__main__':
    unittest.main()
