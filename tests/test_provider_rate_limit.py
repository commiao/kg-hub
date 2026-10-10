"""Provider 429s: refused calls are not reviewed, held jobs are not abandoned.

2026-10-09 15:40 the shared token plan ran out of quota. Every queued kg-hub
call came back failed/provider_error/429; kg-hub took "the provider call
started" to mean an unknown paid outcome and held 464 tasks for review, while
the refinery kept sending. credvault #67 will instead hold such jobs queued
(error.code=provider_rate_limited, ready_at=next probe).
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from utils import gateway_queue as gq
from utils import refinery_recovery as recovery
from utils.model_attempt_journal import ModelAttemptJournal, NeedsReconciliation

BODY = {'model': 'kg_hub.entity_extract', 'max_tokens': 100,
        'messages': [{'role': 'user', 'content': 'hello'}]}
ANSWER = {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'logical',
          'content': [{'type': 'text', 'text': 'answer'}], 'stop_reason': 'end_turn',
          'stop_sequence': None, 'usage': {'input_tokens': 2, 'output_tokens': 1}}
REFUSED = {'code': 'provider_error', 'http_status': 429, 'message': '供应商调用失败（HTTP 429）'}
HELD = {'code': 'provider_rate_limited', 'http_status': 429, 'message': '供应商正在限流'}


def job(state, error=None, **extra):
    return {'version': 1, 'job': {'request_key': 'k', 'business_key': BODY['model'],
                                  'state': state, 'response': ANSWER, 'error': error,
                                  **extra}}


async def run(states, **kwargs):
    seen = []

    def handle(req):
        seen.append(req.url.path)
        return httpx.Response(200, json=states[min(len(seen), len(states)) - 1])
    result = await gq.execute_queued('http://model-gateway:39000', 'token', 'k', BODY,
                                     transport=httpx.MockTransport(handle), poll_seconds=0,
                                     held_poll_seconds=0, **kwargs)
    return result, seen


class QueueSignalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gq._provider_wait.clear()

    async def test_provider_429_is_a_refusal_with_the_sdk_shape(self):
        with self.assertRaises(gq.QueueProviderRefused) as caught:
            await run([job('failed', REFUSED)])
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.body['error']['code'], 'provider_rate_limited')

    async def test_other_provider_failures_stay_plain_outcomes(self):
        for error in ({**REFUSED, 'http_status': 502}, {'code': 'executor_error'}):
            with self.assertRaises(gq.QueueOutcomeError) as caught:
                await run([job('failed', error)])
            self.assertNotIsInstance(caught.exception, gq.QueueProviderRefused)

    async def test_held_job_is_not_abandoned_at_the_deadline(self):
        result, seen = await run([job('queued', HELD, ready_at=1.0)] * 4
                                 + [job('succeeded')], wait_seconds=0)
        self.assertEqual(result.content[0].text, 'answer')
        self.assertEqual(len(seen), 5)
        self.assertIsNone(gq.provider_wait())   # cleared by the success

    async def test_the_wait_budget_restarts_after_the_hold(self):
        # Each poll pause takes 100s; the budget is 150s. Without the restart
        # the job would time out on the first ordinary poll after the hold.
        now = [0]

        async def sleep(_):
            now[0] += 100
        clock = SimpleNamespace(monotonic=lambda: now[0], time=time.time)
        with mock.patch.object(gq, 'time', clock), mock.patch.object(gq.asyncio, 'sleep', sleep):
            result, _ = await run([job('queued', HELD, ready_at=1.0)] * 3
                                  + [job('queued'), job('succeeded')], wait_seconds=150)
        self.assertEqual(result.content[0].text, 'answer')

    async def test_an_ordinary_queued_job_still_times_out(self):
        with self.assertRaises(TimeoutError):
            await run([job('queued')], wait_seconds=0)
        self.assertIsNone(gq.provider_wait())

    async def test_hold_is_visible_while_waiting_and_expires_unrefreshed(self):
        views = []

        async def sleep(_):
            views.append(gq.provider_wait())
        with mock.patch.object(gq.asyncio, 'sleep', sleep):
            await run([job('queued', HELD, ready_at=1234.0), job('succeeded')])
        self.assertEqual(views[0]['retry_at'], 1234.0)
        gq._note_provider_wait({'ready_at': None})
        with mock.patch.object(gq.time, 'time', return_value=time.time() + gq.PROVIDER_WAIT_TTL + 1):
            self.assertIsNone(gq.provider_wait())


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_refusal_is_journaled_as_no_model_call_and_not_reviewed(self):
        import model_gateway_client as mgc
        from utils.model_attempt_journal import summarize_attempts
        with tempfile.TemporaryDirectory() as root:
            journal = ModelAttemptJournal(Path(root) / 'j.sqlite3')

            async def queued(*args):
                raise gq.QueueProviderRefused({'state': 'failed', 'error': REFUSED})
            client = SimpleNamespace(messages=SimpleNamespace(create=mock.AsyncMock()))
            mgc.install_gateway_request_contract(client, queue_transport=queued)
            status = mock.Mock(side_effect=AssertionError('no reconciliation lookup'))
            with mock.patch.object(mgc, 'journal_from_backup_env', return_value=journal), \
                 mock.patch.object(mgc, 'gateway_token', return_value='fixture'), \
                 mock.patch.object(mgc, 'query_gateway_attempt_status', status):
                with mgc.model_business_task('s', '1'), mgc.model_operation('ingest.episode', '1'):
                    with self.assertRaises(gq.QueueProviderRefused):
                        await client.messages.create(**BODY)
            rows = journal.find_task('s', '1')
            self.assertEqual((rows[0]['phase'], rows[0]['provider_call_started']), ('failed', 0))
            summary = summarize_attempts(rows, deadline_seconds=180)
            self.assertEqual(summary['failed_calls_total'], 0)
            self.assertFalse(summary['in_flight'])

    async def test_other_queue_failures_still_ask_reconciliation(self):
        import model_gateway_client as mgc
        with tempfile.TemporaryDirectory() as root:
            journal = ModelAttemptJournal(Path(root) / 'j.sqlite3')

            async def queued(*args):
                raise gq.QueueOutcomeError({'state': 'failed', 'error': {**REFUSED, 'http_status': 502}})
            client = SimpleNamespace(messages=SimpleNamespace(create=mock.AsyncMock()))
            mgc.install_gateway_request_contract(client, queue_transport=queued)
            with mock.patch.object(mgc, 'journal_from_backup_env', return_value=journal), \
                 mock.patch.object(mgc, 'gateway_token', return_value='fixture'), \
                 mock.patch.object(mgc, 'query_gateway_attempt_status', return_value={
                     'phase': 'failed', 'provider_call_started': True, 'http_status': 502}):
                with mgc.model_business_task('s', '1'), mgc.model_operation('ingest.episode', '1'):
                    with self.assertRaises(NeedsReconciliation):
                        await client.messages.create(**BODY)


class ClassifyTests(unittest.TestCase):
    def test_refusals_classify_as_rate_limited(self):
        import ast
        # kg_hub_server cannot be imported here (no graphiti_core / .env), so
        # execute the classifier itself, as test_upstream_error_classification does.
        source = (Path(__file__).resolve().parents[1] / 'kg_hub_server.py').read_text('utf-8')
        fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                  and n.name == 'classify_extract_error')
        ns = {'NeedsReconciliation': NeedsReconciliation}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<classifier>', 'exec'), ns)
        classify = ns['classify_extract_error']
        refused = gq.QueueProviderRefused({'state': 'failed', 'error': REFUSED})
        self.assertEqual(classify(refused), 'rate_limited')
        held = SimpleNamespace(status_code=429, body={'error': HELD})
        self.assertEqual(classify(held), 'rate_limited')


class RefineryBackoffTests(unittest.TestCase):
    def test_consecutive_pauses_double_to_fifteen_minutes(self):
        state, delays = {}, []
        for _ in range(6):
            recovery.record_failure(state, 'rate_limited', cycle=0, interval=90,
                                    quota_delay=1800, now=0)
            # A sibling refused during the same pause does not escalate.
            recovery.record_failure(state, 'rate_limited', cycle=0, interval=90,
                                    quota_delay=1800, now=0)
            delays.append(state['retry_at'])
            recovery.probe_result(state, True, now=0)
        self.assertEqual(delays, [60, 120, 240, 480, 900, 900])

    def test_a_success_resets_the_backoff(self):
        state = {}
        for _ in range(3):
            recovery.record_failure(state, 'rate_limited', cycle=0, interval=90,
                                    quota_delay=1800, now=0)
            recovery.probe_result(state, True, now=0)
        recovery.note_success(state)
        recovery.record_failure(state, 'rate_limited', cycle=0, interval=90,
                                quota_delay=1800, now=0)
        self.assertEqual(state['retry_at'], 60)


class RefineryPollTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_held_by_gateway_does_not_time_out_and_halts_sending(self):
        import kg_refinery as refinery
        refinery._provider_wait_seen[0] = 0.0
        answers = iter([(200, {'status': 'pending', 'provider_wait': {'since': 1}})] * 5
                       + [(200, {'status': 'ok'})])
        with mock.patch.object(refinery, '_http', lambda *a, **k: next(answers)), \
             mock.patch.object(refinery.asyncio, 'sleep', mock.AsyncMock()):
            self.assertEqual(await refinery.poll_until_done('sd', '1', max_wait=1), 'ok')
        self.assertTrue(refinery.provider_waiting())
        refinery._provider_wait_seen[0] = 0.0
        self.assertFalse(refinery.provider_waiting())

    async def test_plain_pending_still_times_out(self):
        import kg_refinery as refinery
        with mock.patch.object(refinery, '_http', lambda *a, **k: (200, {'status': 'pending'})), \
             mock.patch.object(refinery.asyncio, 'sleep', mock.AsyncMock()):
            self.assertEqual(await refinery.poll_until_done('sd', '1', max_wait=1), 'timeout')


if __name__ == '__main__':
    unittest.main()
