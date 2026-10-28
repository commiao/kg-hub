"""Original capacity terminal + real operation locks; no model/NAS calls."""
import contextlib
import copy
import json
from pathlib import Path
import sys
import unittest
import subprocess
import os
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from test_campaign_continuation import ContinuationTests
from tools import campaign_settlement as settlement, campaign_continuation as cc
from tools import extract_campaign as ec, campaign_supervision as supervision
from utils import process_receipts as receipts


class SettlementTests(unittest.TestCase):
    setUpBase=ContinuationTests.setUpBase
    tearDown=ContinuationTests.tearDown
    gather=ContinuationTests.gather
    post=ContinuationTests.post
    fail_upload=ContinuationTests.fail_upload
    worker=ContinuationTests.worker
    transport_trace=ContinuationTests.transport_trace
    readback=ContinuationTests.readback
    def setUp(self):
        ContinuationTests.setUp(self)
        log=self.d/'log.jsonl';rows=log.read_text().splitlines();log.write_text(rows[0]+'\n')
        root,event=self.worker('m',2,submit=True,capacity=True)
        rows=[json.loads(s) for s in event.read_text().splitlines()]
        rows=[r for r in rows if not (r['type']=='item.completed' and r.get('item',{}).get('type')=='command_execution')]
        event.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        p=self.d/'operations'/self.bid/'latest.json';op=receipts.read(p)
        op.update(state='running',phase='submitting',may_have_written=True)
        op.pop('ended_at',None);op.pop('exit_code',None)
        receipts.write(p,op);receipts.write(p.parent/(op['attempt']+'.json'),op)
        self.original_bytes=(p.parent/(op['attempt']+'.json')).read_bytes()
        self.original_receipt=op
        self.original_log=log.read_bytes()

    def remote(self,keys,fields):
        *_,bodies=settlement.payloads(ec.load(self.d),self.d,ec.find_batch(ec.load(self.d),self.bid))
        return {'version':'source-settlement-remote-v1','keys':keys,'payload_fields':fields,
            'anchor_count':1,'anchor_request_id':'req-0',
            'health':{'status':'ok','active_extractions':0,'draining':False},
            'read_only':True,'observation_bodies_returned':False,
            'executions':[{**k,'execution_id':'req-'+str(n),'state':'succeeded',
                'started_at':'2099-01-01T00:00:00Z','finished_at':'2099-01-01T00:01:00Z'} for n,k in enumerate(keys[:2])],
            'accepted':[{**k,'request_id':'req-'+str(n),'payload_sha256':settlement.digest(bodies[n])} for n,k in enumerate(keys[:2])]}

    def settle(self,snapshot=None):
        return settlement.reconcile(self.c['id'],self.bid,url='http://invalid',token=None,
            post=self.readback,snapshot=snapshot or self.remote)

    def test_capacity_adopts_settled_prefix_once_and_submits_only_unaccepted_suffix(self):
        self.assertEqual(self.settle()['adopted_settled'],1)
        self.assertTrue(self.settle()['idempotent'])
        records=cc.partial_records(ec.load(self.d),self.d,ec.find_batch(ec.load(self.d),self.bid))
        self.assertEqual(len(records),2);self.assertIsNone(records[1]['http'])
        self.assertEqual(records[1]['receipt_kind'],settlement.VERSION)
        self.assertEqual(supervision.completed(ec.load(self.d),self.d,'m'),2)
        with mock.patch.object(ec,'model_readiness') as probe:
            self.assertEqual(cc.reconcile_worker(self.c['id'],'m',2,reason='exact original settlement')['kind'],'capacity_settled')
            probe.assert_not_called()
        self.assertTrue(cc.worker_reconciled(ec.load(self.d),self.d,'m',2))
        sent=[]
        def post(*args):sent.append(args[2]['source_obs_id']);return self.post(*args)
        result=ec.cmd_submit(self.c['id'],self.bid,dry_run=False,url='http://invalid',token=None,
                             post=post,gate=lambda k:contextlib.nullcontext())
        self.assertEqual(result['by_status'],{'ok':3})
        self.assertEqual(sent,[self.batch['items'][2]['source_obs_id']])
        self.assertEqual(len((self.d/'log.jsonl').read_text().splitlines()),3)
        self.assertTrue((self.d/'log.jsonl').read_bytes().startswith(self.original_log))
        self.assertEqual((self.d/'operations'/self.bid/(self.original_receipt['attempt']+'.json')).read_bytes(),self.original_bytes)

    def test_remote_mismatches_write_nothing(self):
        for change in [lambda v:v['accepted'][1].update(payload_sha256='foreign'),
                       lambda v:v['executions'][1].update(state='running'),
                       lambda v:v['executions'][1].update(execution_id='foreign'),
                       lambda v:v['accepted'].append(copy.deepcopy(v['accepted'][0])),
                       lambda v:v['health'].update(active_extractions=1),
                       lambda v:v['executions'][1].update(started_at='2000-01-01')]:
            def remote(*a):v=self.remote(*a);change(v);return v
            with self.subTest(change=change),self.assertRaises(ValueError):self.settle(remote)
            self.assertEqual((self.d/'log.jsonl').read_bytes(),self.original_log)
            self.assertFalse((self.d/'operations'/self.bid/'settlement-active.json').exists())

    def test_real_live_operation_cannot_be_adopted(self):
        # The actual lease is authoritative, regardless of a terminal native.
        directory=self.d/'operations'/self.bid
        with cc._operation_lock(directory):
            with self.assertRaises(receipts.Busy):self.settle()

    def test_original_event_and_ledger_drift_block_suffix(self):
        self.settle();event=Path(receipts.read(self.d/'operations'/self.bid/'settlement-active.json')['path'])
        proof=cc._read(event);p=Path(proof['events']);p.write_text(p.read_text()+'{}\n')
        with self.assertRaisesRegex(ValueError,'inputs changed'):
            cc.partial_records(ec.load(self.d),self.d,ec.find_batch(ec.load(self.d),self.bid))

    def test_actual_process_crash_after_adopted_log_before_operation_marker_is_idempotent(self):
        *_,bodies=settlement.payloads(ec.load(self.d),self.d,ec.find_batch(ec.load(self.d),self.bid))
        keys=[{k:p[k] for k in ('source_description','source_obs_id')} for p in bodies]
        remote=self.d/'synthetic-remote.json';receipts.write(remote,self.remote(keys,sorted(bodies[0])))
        code='''import json,os,sys
from pathlib import Path
from unittest import mock
from tools import campaign_settlement as s,extract_campaign as ec
from utils import process_receipts as r
cid,bid,path=sys.argv[1:];write=r.write
def crash(p,value):
 if Path(p).name=='latest.json' and value.get('state')=='reconciled_partial':os._exit(17)
 return write(p,value)
def post(method,url,body,token):
 return 200,{'items':[{**k,'status':'ok' if n<2 else None,'episode_uuid':'uuid-'+k['source_obs_id'] if n<2 else None} for n,k in enumerate(body['items'])]}
with mock.patch.object(ec,'tracking_guard'),mock.patch.object(r,'write',side_effect=crash):
 s.reconcile(cid,bid,url='invalid',token=None,post=post,snapshot=lambda *a:json.loads(Path(path).read_text()))
'''
        env=dict(os.environ);env['PYTHONPATH']=os.pathsep.join([str(Path(ec.__file__).resolve().parent.parent),*sys.path])
        p=subprocess.run([sys.executable,'-c',code,self.c['id'],self.bid,str(remote)],env=env,
                          capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,17,p.stderr)
        self.assertEqual(len((self.d/'log.jsonl').read_text().splitlines()),2)
        self.assertTrue(self.settle()['idempotent']);self.assertTrue(self.settle()['idempotent'])
        self.assertEqual(len((self.d/'log.jsonl').read_text().splitlines()),2)
        self.assertTrue(cc.valid_partial(ec.load(self.d),self.d,ec.find_batch(ec.load(self.d),self.bid),
                                       receipts.status(self.d/'operations'/self.bid)))


if __name__=='__main__':unittest.main()
