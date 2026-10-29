"""Legacy single-owner recovery never changes slot identity or replays writes."""
import json,shutil,sys,copy
from pathlib import Path
from unittest import mock
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import test_campaign_local_recovery as fixture
from tools import campaign_local_recovery as local,extract_campaign as ec,campaign_supervision as s
from utils import process_receipts as r

class SingleLocalTests(unittest.TestCase):
    setUpBase=fixture.LocalRecoveryTests.setUpBase
    tearDown=fixture.LocalRecoveryTests.tearDown
    gather=fixture.LocalRecoveryTests.gather
    post=fixture.LocalRecoveryTests.post
    fail_upload=fixture.LocalRecoveryTests.fail_upload
    worker=fixture.LocalRecoveryTests.worker
    transport_trace=fixture.LocalRecoveryTests.transport_trace
    def setUp(self):
        fixture.LocalRecoveryTests.setUp(self)
        root,event=self.worker('m',2,kind='infrastructure_failed')
        old=event.read_text();temporary=self.root/'saved-worker';shutil.move(str(root),temporary)
        parent=ec.supervisor_dir(self.d,'m');shutil.rmtree(parent);shutil.move(temporary,parent)
        self.event=parent/'original.events.jsonl';self.event.write_text(old)
        worker=r.read(parent/'worker.json');worker['events']=str(self.event);r.write(parent/'worker.json',worker)
        owner=r.read(parent/'latest.json');owner['identity']={'campaign':self.c['id'],'model':'m'};r.write(parent/'latest.json',owner)
        c=ec.load(self.d);c['parallel']=1;c.pop('slot_sessions',None);c.pop('migration',None);c['dispatch']={'state':'running'}
        for b in c['batches']:b.pop('slot',None)
        ec.save(self.d,c);(self.d/'log.jsonl').write_text('')
        b=ec.find_batch(c,self.bid);_,_,_,_,sha=local.cc._inputs(c,self.d,b)
        self.op=self.d/'operations'/self.bid/'latest.json';op=r.read(self.op)
        op.update(kind='dryrun',state='completed',exit_code=0,may_have_written=False,requires_reconciliation=False,
                  by_status={'dry_run':2,'http_502':1},input_sha256=sha,
                  identity={'campaign':c['id'],'batch':self.bid,**ec.batch_identity(c,b)},
                  source_keys=[{k:i[k] for k in ('source_description','source_obs_id')} for i in self.batch['items']])
        r.write(self.op,op)
    def reconcile(self):
        return local.reconcile(self.c['id'],'m',None,reason='reviewed exact original preview',url='invalid',token=None,preflight=lambda *a:{'ok':True})
    def test_original_legacy_recovery_keeps_events_and_does_not_bind_slots(self):
        before=self.event.read_bytes();out=self.reconcile();self.assertEqual(out['kind'],'readonly_preview_recovered')
        c=ec.load(self.d);self.assertNotIn('slot_sessions',c);self.assertTrue(local.valid(c,self.d,'m',None))
        self.assertEqual(self.event.read_bytes(),before)
        # The real supervisor consumes this proof with no slot and resumes the same SID.
        calls=[]
        def spawn(args,**kw):
            calls.append(args);self.assertEqual(args[args.index('resume')+1],'original-m-2')
            raise InterruptedError('synthetic launch boundary')
        with mock.patch.object(ec,'tracking_guard'),mock.patch.object(ec,'workspace_dir',return_value=self.root),self.assertRaisesRegex(InterruptedError,'synthetic'):
            s.run(self.c['id'],'m',session='original-m-2',preflight=lambda *a:{'ok':True},spawn=spawn,poll=.01)
        self.assertEqual(len(calls),1)
    def test_mismatched_or_writable_receipt_is_not_recoverable(self):
        original=r.read(self.op)
        for field in ['source_keys','identity','input_sha256','may_have_written','by_status']:
            changed=copy.deepcopy(original)
            changed[field]={'source_keys':[],'identity':{},'input_sha256':'bad','may_have_written':True,'by_status':{'dry_run':2,'http_500':1}}[field]
            r.write(self.op,changed)
            with self.subTest(field=field),self.assertRaises(ValueError):self.reconcile()
        r.write(self.op,original)
        with r.attempt(self.op.parent,kind='dryrun',identity=original['identity']):
            with self.assertRaises((ValueError,r.Busy)):self.reconcile()
    def test_owner_and_protocol_boundaries(self):
        c=ec.load(self.d);before=copy.deepcopy(c)
        for key,value in [('parallel',2),('slot_sessions',{'m':{'1':'original-m-2'}}),('migration',{'id':'foreign'})]:
            c=copy.deepcopy(before);c[key]=value;ec.save(self.d,c)
            with self.subTest(key=key),self.assertRaises(ValueError):self.reconcile()
        ec.save(self.d,before)
        with self.assertRaises(ValueError):local.reconcile(self.c['id'],'m',None,reason='not allowed',url='invalid',token=None,prior_backend='other')
        content=self.event.read_text();self.event.write_text(content.replace('original-m-2','foreign'))
        with self.assertRaises(ValueError):self.reconcile()
    def test_changed_proof_and_native_lease_block_resume(self):
        self.reconcile();c=ec.load(self.d)
        self.event.write_text(self.event.read_text()+'{}\n');self.assertFalse(local.valid(c,self.d,'m',None))

if __name__=='__main__':unittest.main()
