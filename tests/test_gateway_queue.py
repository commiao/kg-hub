import json
import tempfile
import unittest
from pathlib import Path
import httpx
from utils.gateway_queue import execute_queued, QueueOutcomeError, request_body, body_digest
from utils.model_attempt_journal import ModelAttemptJournal, NeedsReconciliation

BODY = {'model':'kg_hub.entity_extract','max_tokens':100,'messages':[{'role':'user','content':'hello'}]}
ANSWER = {'id':'msg_1','type':'message','role':'assistant','model':'logical',
          'content':[{'type':'text','text':'answer'}], 'stop_reason':'end_turn',
          'stop_sequence':None, 'usage':{'input_tokens':2,'output_tokens':1}}

class QueueClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_poll_and_restore_sdk_message_without_direct_model_call(self):
        seen=[]
        async def handle(req):
            seen.append(req.url.path)
            state = 'queued' if len(seen) == 1 else 'succeeded'
            return httpx.Response(202 if len(seen)==1 else 200,
                json={'version':1,'job':{'request_key':'k','business_key':BODY['model'],
                     'state':state,'response':ANSWER}})
        digest=[]
        async def before(value): digest.append(value)
        result=await execute_queued('http://model-gateway:39000','test-token','k',BODY,
            transport=httpx.MockTransport(handle), poll_seconds=0, before_submit=before)
        self.assertEqual(result.content[0].text,'answer')
        self.assertEqual(seen,['/v1/queue/submit','/v1/queue/status'])
        self.assertEqual(digest,[body_digest(BODY)])

    async def test_reconciliation_and_timeout_do_not_resubmit(self):
        for state in ['reconciliation','queued']:
            seen=[]
            def handle(req):
                seen.append(req.url.path)
                return httpx.Response(202,json={'version':1,'job':{'request_key':'k',
                    'business_key':BODY['model'],'state':state}})
            with self.assertRaises(QueueOutcomeError if state=='reconciliation' else TimeoutError):
                await execute_queued('http://model-gateway:39000','token','k',BODY,
                    transport=httpx.MockTransport(handle),wait_seconds=0)
            # Giving up asks the gateway to withdraw the job; it never resubmits.
            self.assertEqual(seen,['/v1/queue/submit'] if state=='reconciliation'
                             else ['/v1/queue/submit','/v1/queue/cancel'])

    def test_extra_body_and_transport_parameters(self):
        body=request_body({**BODY,'extra_headers':{'Authorization':'secret'},
                           'timeout':42,'extra_body':{'thinking':{'type':'disabled'}}})
        self.assertNotIn('extra_headers',body)
        self.assertNotIn('timeout',body)
        self.assertEqual(body['thinking'],{'type':'disabled'})

    def test_only_queue_owned_intents_can_resume_automatically(self):
        with tempfile.TemporaryDirectory() as root:
            journal=ModelAttemptJournal(Path(root)/'attempts.sqlite3')
            args=dict(key='queue-k',business_key=BODY['model'],source_description='test',
                      source_obs_id='1',step_id='step',request_digest='digest')
            journal.prepare(**args,queue_owned=True)
            reopened=ModelAttemptJournal(journal.path)
            self.assertIsNone(reopened.prepare(**args,queue_owned=True))
            with self.assertRaises(RuntimeError):
                reopened.prepare(**{**args,'request_digest':'changed'},queue_owned=True)
            legacy={**args,'key':'legacy','step_id':'legacy-step'}
            journal.prepare(**legacy)
            journal.start_http('legacy')
            with self.assertRaises(NeedsReconciliation):
                reopened.prepare(**legacy,queue_owned=True)
            journal.complete('queue-k',json.dumps(ANSWER))
            self.assertEqual(json.loads(reopened.prepare(**args,queue_owned=True)),ANSWER)

class SubmitRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """2026-10-09: submits accepted after kg-hub gave up were paid and orphaned."""

    def job(self, state):
        return {'version': 1, 'job': {'request_key': 'k', 'business_key': BODY['model'],
                                      'state': state, 'response': ANSWER}}

    async def submit_and_wait(self, handle):
        return await execute_queued('http://model-gateway:39000', 'token', 'k', BODY,
            transport=httpx.MockTransport(handle), poll_seconds=0, submit_backoff=(0,))

    async def test_lost_submit_answer_is_found_by_status_not_resubmitted(self):
        seen = []
        def handle(req):
            seen.append(req.url.path)
            if req.url.path.endswith('/submit'):
                raise httpx.ReadTimeout('gateway answered after 15s', request=req)
            return httpx.Response(200, json=self.job('succeeded'))
        result = await self.submit_and_wait(handle)
        self.assertEqual(result.content[0].text, 'answer')
        self.assertEqual(seen, ['/v1/queue/submit', '/v1/queue/status'])

    async def test_submit_that_never_landed_is_resubmitted_with_identical_content(self):
        submits = []
        def handle(req):
            if req.url.path.endswith('/submit'):
                submits.append((req.headers['Idempotency-Key'], json.loads(req.content)))
                if len(submits) == 1:
                    raise httpx.ConnectError('connection reset', request=req)
                return httpx.Response(202, json=self.job('succeeded'))
            return httpx.Response(404, json={'error': {'code': 'not_found'}})
        result = await self.submit_and_wait(handle)
        self.assertEqual(result.content[0].text, 'answer')
        self.assertEqual(len(submits), 2)
        self.assertEqual(submits[0], submits[1], 'same key and byte-identical content')

    async def test_busy_or_draining_gateway_is_retried(self):
        submits = []
        def handle(req):
            submits.append(req.url.path)
            if len(submits) == 1:
                return httpx.Response(503, json={'error': {'code': 'gateway_busy'}})
            return httpx.Response(202, json=self.job('succeeded'))
        await self.submit_and_wait(handle)
        self.assertEqual(submits, ['/v1/queue/submit', '/v1/queue/submit'])

    async def test_conflicting_content_is_not_retried(self):
        seen = []
        def handle(req):
            seen.append(req.url.path)
            return httpx.Response(409, json={'error': {'code': 'idempotency_conflict'}})
        with self.assertRaises(httpx.HTTPStatusError):
            await self.submit_and_wait(handle)
        self.assertEqual(seen, ['/v1/queue/submit'])

    async def test_retries_are_bounded(self):
        seen = []
        def handle(req):
            seen.append(req.url.path)
            if req.url.path.endswith('/submit'):
                raise httpx.ReadTimeout('slow', request=req)
            raise httpx.ReadTimeout('status also slow', request=req)
        with self.assertRaises(httpx.ReadTimeout):
            await self.submit_and_wait(handle)
        self.assertEqual(seen.count('/v1/queue/submit'), 8)
        self.assertEqual(seen.count('/v1/queue/status'), 8)


    async def test_submit_rides_out_a_gateway_restart(self):
        submits = []
        def handle(req):
            if req.url.path.endswith('/submit'):
                submits.append(1)
                if len(submits) <= 4:
                    raise httpx.ConnectError('All connection attempts failed', request=req)
                return httpx.Response(202, json=self.job('succeeded'))
            raise httpx.ConnectError('All connection attempts failed', request=req)
        with self.assertLogs('kg_hub.gateway_queue', 'WARNING') as logs:
            result = await self.submit_and_wait(handle)
        self.assertEqual(result.content[0].text, 'answer')
        self.assertEqual(len(submits), 5)
        self.assertEqual(sum('[queue:submit_retry]' in line for line in logs.output), 4)

    async def test_gateway_restart_while_polling_does_not_fail_the_task(self):
        polls = []
        def handle(req):
            if req.url.path.endswith('/submit'):
                return httpx.Response(202, json=self.job('queued'))
            polls.append(1)
            if len(polls) == 1:
                raise httpx.ConnectError('All connection attempts failed', request=req)
            if len(polls) == 2:
                return httpx.Response(503, json={'error': {'code': 'draining'}})
            if len(polls) == 3:
                raise httpx.ReadTimeout('slow', request=req)
            return httpx.Response(200, json=self.job('succeeded'))
        with self.assertLogs('kg_hub.gateway_queue', 'WARNING') as logs:
            result = await self.submit_and_wait(handle)
        self.assertEqual(result.content[0].text, 'answer')
        self.assertEqual(len(polls), 4)
        self.assertEqual(sum('[queue:poll_retry]' in line for line in logs.output), 3)

    async def test_polling_still_gives_up_at_the_deadline(self):
        def handle(req):
            if req.url.path.endswith('/submit'):
                return httpx.Response(202, json=self.job('queued'))
            raise httpx.ConnectError('gateway gone', request=req)
        with self.assertRaises(TimeoutError):
            await execute_queued('http://model-gateway:39000', 'token', 'k', BODY,
                transport=httpx.MockTransport(handle), poll_seconds=0, wait_seconds=0.05,
                submit_backoff=(0,))

    async def test_missing_job_while_polling_is_not_swallowed(self):
        def handle(req):
            if req.url.path.endswith('/submit'):
                return httpx.Response(202, json=self.job('queued'))
            return httpx.Response(404, json={'error': {'code': 'not_found'}})
        with self.assertRaises(httpx.HTTPStatusError):
            await self.submit_and_wait(handle)


class BusinessReceiptTests(unittest.TestCase):
    def test_only_durable_model_results_generate_receipts_and_receipts_survive_restart(self):
        with tempfile.TemporaryDirectory() as root:
            journal=ModelAttemptJournal(Path(root)/'journal.sqlite3')
            args=dict(business_key=BODY['model'],source_description='s',source_obs_id='1',request_digest='d')
            journal.prepare(key='complete',step_id='a',queue_owned=True,**args)
            journal.prepare(key='pending',step_id='b',queue_owned=True,**args)
            journal.prepare(key='legacy',step_id='c',**args)
            journal.complete('complete',json.dumps(ANSWER))
            journal.complete('legacy',json.dumps(ANSWER))
            journal.queue_business_receipts('s','1','neo4j:episode:1')
            reopened=ModelAttemptJournal(journal.path)
            pending=reopened.pending_queue_receipts()
            self.assertEqual([p['idempotency_key'] for p in pending],['complete'])
            self.assertEqual(pending[0]['receipt'],{'state':'completed','reference':'neo4j:episode:1'})
            reopened.acknowledge_queue_receipt('complete')
            self.assertEqual(reopened.pending_queue_receipts(),[])

class CrashGapTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_commit_receipt_gap_recovers_without_a_model_request(self):
        from utils.gateway_queue import recover_business_receipts
        with tempfile.TemporaryDirectory() as root:
            journal=ModelAttemptJournal(Path(root)/'j.sqlite3')
            for sid in ['1','2']:
                journal.prepare(key=sid,business_key=BODY['model'],source_description='s',
                    source_obs_id=sid,step_id=sid,request_digest=sid,queue_owned=True)
                journal.complete(sid,json.dumps(ANSWER))
            checked=[]
            async def verify(sd,sid):
                checked.append((sd,sid))
                return 'neo4j:episode:2' if sid=='2' else None
            cursor=await recover_business_receipts(ModelAttemptJournal(journal.path),verify)
            self.assertEqual(cursor,('s','2'))
            self.assertEqual(checked,[('s','1'),('s','2')])
            self.assertEqual([r['idempotency_key'] for r in journal.pending_queue_receipts()],['2'])
            self.assertEqual(await recover_business_receipts(journal,verify,cursor),('', ''))

    async def test_queue_failure_records_authoritative_call_status(self):
        import model_gateway_client as mgc
        from unittest import mock
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            journal=ModelAttemptJournal(Path(root)/'j.sqlite3')
            direct=mock.AsyncMock(side_effect=AssertionError('direct model execution forbidden'))
            async def queued(*args):
                raise QueueOutcomeError({'state':'failed','error':{'code':'input_too_large'}})
            client=SimpleNamespace(messages=SimpleNamespace(create=direct))
            mgc.install_gateway_request_contract(client,queue_transport=queued)
            with mock.patch.object(mgc,'journal_from_backup_env',return_value=journal), \
                 mock.patch.object(mgc,'gateway_token',return_value='fixture'), \
                 mock.patch.object(mgc,'query_gateway_attempt_status',return_value={
                     'phase':'preflight','provider_call_started':False,'http_status':400}):
                with mgc.model_business_task('s','1'), mgc.model_operation('ingest.episode','1'):
                    with self.assertRaises(QueueOutcomeError):
                        await client.messages.create(**BODY)
            self.assertEqual(journal.find_task('s','1')[0]['provider_call_started'],0)
            direct.assert_not_called()
            journal.queue_business_receipts('s','1','sqlite:task:1','failed')
            self.assertEqual(journal.pending_queue_receipts()[0]['receipt']['state'],'failed')

class LifespanReceiptWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_lifespan_callback_uses_status_driver_and_requires_persisted_result(self):
        import ast
        import asyncio
        from contextlib import asynccontextmanager
        from unittest import mock
        import utils.gateway_queue as queue
        import utils.task_execution as tasks
        import utils.loop_block_probe as probe
        source = Path(__file__).resolve().parents[1] / 'kg_hub_server.py'
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == '_application_lifespan')
        module = ast.Module(body=[function], type_ignores=[])
        row = {'episode_uuid': 'stored-episode'}
        for stored, verified, expected in [(None, False, None), (row, False, None),
                                           (row, True, 'neo4j:ingest:stored-episode')]:
            driver = object()
            observed = asyncio.get_running_loop().create_future()
            async def receipt_loop(factory, url, token, *, verify_result):
                try:
                    observed.set_result(await verify_result('source', 'observation'))
                except Exception as exc:
                    observed.set_exception(exc)
                await asyncio.Future()
            verify = mock.AsyncMock(return_value=verified)
            namespace = {'asyncio': asyncio, 'asynccontextmanager': asynccontextmanager,
                         '_start_reconciliation_mailbox': mock.AsyncMock(),
                         '_stop_reconciliation_mailbox': mock.AsyncMock(),
                         'INGEST_BACKUP_PATH': '/owned/fixture',
                         'get_status_driver': mock.Mock(return_value=driver),
                         '_persisted_business_result': verify,
                         'journal_from_backup_env': mock.Mock(),
                         'gateway_base_url': lambda: 'http://fixture',
                         'gateway_token': lambda: 'fixture'}
            exec(compile(module, str(source), 'exec'), namespace)
            with mock.patch.object(queue, 'receipt_loop', receipt_loop), \
                 mock.patch.object(tasks, 'read_task', mock.AsyncMock(return_value=stored)) as read, \
                 mock.patch.object(probe, 'start_probe'), \
                 mock.patch.object(probe, 'stop_probe', mock.AsyncMock()):
                async with namespace['_application_lifespan'](None):
                    self.assertEqual(await asyncio.wait_for(observed, 2), expected)
                read.assert_awaited_once_with(driver, 'source', 'observation')
                namespace['get_status_driver'].assert_called_once_with()
                if stored:
                    verify.assert_awaited_once_with(driver, stored)
                else:
                    verify.assert_not_awaited()

class ReceiptAckDispositionTests(unittest.IsolatedAsyncioTestCase):
    """2026-10-10: ack 409 with disposition=failed was retried every 10s for 13h."""
    RECEIPT = {'state':'completed','reference':'neo4j:ingest:1'}

    def setUp(self):
        from utils.gateway_queue import ReceiptBackoff
        self.now = [0.0]
        self.backoff = ReceiptBackoff(clock=lambda: self.now[0])
        self.acked = []
        test = self
        class Journal:
            def pending_queue_receipts(self):
                return [{'idempotency_key':k,'business_key':BODY['model'],'receipt':test.RECEIPT}
                        for k in ('k1',) if k not in test.acked]
            def acknowledge_queue_receipt(self, key): test.acked.append(key)
        self.journal = Journal()
        self.requests = []

    async def run_pass(self, respond):
        import logging
        from utils.gateway_queue import acknowledge_receipts
        async def handle(req):
            self.requests.append(json.loads(req.content))
            return respond(req)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertLogs('kg_hub.queue_receipts', 'WARNING') as logs:
                logging.getLogger('kg_hub.queue_receipts').warning('marker')
                done = await acknowledge_receipts(client, self.journal, 'http://gw', 't',
                                                  self.backoff, logging.getLogger('kg_hub.queue_receipts'))
        return done, [r for r in logs.records if r.getMessage() != 'marker']

    async def test_failed_disposition_parks_the_receipt_without_traceback(self):
        conflict = lambda req: httpx.Response(409, json={'error':{'code':'idempotency_conflict',
            'message':'model request has not reached a known terminal state','disposition':'failed'}})
        done, records = await self.run_pass(conflict)
        self.assertEqual((done, len(self.requests)), ([], 1))
        self.assertEqual(len(records), 1)
        self.assertIn('[receipt:parked]', records[0].getMessage())
        self.assertIsNone(records[0].exc_info)
        self.now[0] = 10**6
        done, records = await self.run_pass(conflict)
        self.assertEqual((len(self.requests), records), (1, []))

    async def test_other_errors_back_off_per_receipt_and_double(self):
        down = lambda req: httpx.Response(503, text='restarting')
        await self.run_pass(down)
        self.now[0] = 9.9
        await self.run_pass(down)
        self.assertEqual(len(self.requests), 1)
        self.now[0] = 10.0
        _, records = await self.run_pass(down)
        self.assertEqual(len(self.requests), 2)
        self.assertIn('next_in=20s', records[0].getMessage())

    async def test_retry_after_seconds_is_honoured(self):
        paused = lambda req: httpx.Response(503, json={'error':{'code':'provider_paused',
            'disposition':'paused','retry_after_seconds':120}})
        _, records = await self.run_pass(paused)
        self.assertIn('next_in=120s', records[0].getMessage())
        self.now[0] = 119
        await self.run_pass(paused)
        self.assertEqual(len(self.requests), 1)

    async def test_transport_error_backs_off(self):
        def boom(req): raise httpx.ConnectError('refused', request=req)
        _, records = await self.run_pass(boom)
        self.assertIn('reason=ConnectError', records[0].getMessage())
        await self.run_pass(boom)
        self.assertEqual(len(self.requests), 1)

    async def test_success_acknowledges_and_clears_backoff(self):
        await self.run_pass(lambda req: httpx.Response(503))
        self.now[0] = 10
        ok = lambda req: httpx.Response(200, json={'version':1,'job':{'request_key':'k1',
            'business_key':BODY['model'],'business_receipt':self.RECEIPT}})
        done, _ = await self.run_pass(ok)
        self.assertEqual((done, self.acked), (['k1'], ['k1']))
        self.assertFalse(self.backoff.failed_before('k1'))

    async def test_identity_mismatch_logs_traceback_once(self):
        wrong = lambda req: httpx.Response(200, json={'version':1,'job':{'request_key':'other'}})
        _, first = await self.run_pass(wrong)
        self.assertIsNotNone(first[0].exc_info)
        self.now[0] = 10
        _, second = await self.run_pass(wrong)
        self.assertIsNone(second[0].exc_info)
        self.assertEqual(self.acked, [])

if __name__ == '__main__': unittest.main()
