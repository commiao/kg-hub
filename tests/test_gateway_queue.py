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
            self.assertEqual(seen,['/v1/queue/submit'])

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

if __name__ == '__main__': unittest.main()
