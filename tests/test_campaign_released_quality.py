"""Released invalid local work never becomes accepted quality or a write replay."""
import json
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_campaign_rework_maintenance import MaintenanceTests
from tools import campaign_released_quality as released,extract_campaign as ec,campaign_supervision as supervision
from tools.campaign_continuation import worker_reconciled
from utils import process_receipts as receipts


class ReleasedTests(unittest.TestCase):
    setUpBase,tearDown=MaintenanceTests.setUpBase,MaintenanceTests.tearDown
    claim,finish=MaintenanceTests.claim,MaintenanceTests.finish
    write_events=MaintenanceTests.write_events
    save_rows=MaintenanceTests.save_rows
    preview,apply=MaintenanceTests.preview,MaintenanceTests.apply

    def setUp(self):
        MaintenanceTests.setUp(self)
        c=ec.load(self.d);b=ec.find_batch(c,self.bid);b['status']='claimed'
        c['source_decisions']={'version':'source-decisions-v1','reason':'explicit fixture policy'};ec.save(self.d,c)
        log=self.d/'log.jsonl';log.write_text(''.join(x+'\n' for x in log.read_text().splitlines() if json.loads(x).get('batch')!=self.bid))
        ec.cmd_release(c['id'],self.bid,reason='quarantine all invalid local quality')
        self.command=shlex.join([str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser()),'campaign','prepare','--id',c['id'],'--batch',self.bid])
        for row in self.rows[1:3]:row['item']['command']=self.command
        self.rows[2]['item']['aggregated_output']=json.dumps({'stage':'quality_sheet','ready':False})
        self.rows[3]['item']['text']=json.dumps({'model':'m','batch':self.bid,'recovery_kind':'quality_process_violation','submitted':{'ok':0,'empty':0,'error':0}})
        self.save_rows()
        w=receipts.read(self.root2/'worker.json');w.pop('mode');w.pop('batches');receipts.write(self.root2/'worker.json',w)
        with receipts.attempt(self.d/'operations'/self.bid,kind='dryrun') as update:update(may_have_written=False,exit_code=0)
        artifact=self.d/'reviews'/f'{self.bid}.sheet.json';artifact.parent.mkdir(exist_ok=True);artifact.write_text('{"verdict":"invalid"}')
        result=self.d/'results'/f'{self.bid}.json';result.write_text('{"items":[]}')
        refs=[artifact,result,self.d/'batches'/f'{self.bid}.json',self.owner_events,self.root2/'worker.json',self.root2/'native/latest.json']
        self.quarantine=self.root/'quarantine.json'
        receipts.write(self.quarantine,{'batch':self.bid,'model':'m','slot':2,'session':self.owner_session,'formal_ok':0,'official_empty':0,
          'source_ids':b['ids'],'public_release':{'exit_code':0,'released':len(b['ids']),'reply':{'batch':self.bid,'released':len(b['ids'])}},'invalid_quality_evidence':{'entire_batch_quarantined':True,'invalid_sheet_accepted_as_pass':False},
          'refs':[{'file':str(p),'sha256':released._ref(p)['sha256']} for p in refs]})
        self.review=self.root/'released-review.json'

    def make_preview(self):return released.preview(self.c['id'],'m',2,self.bid,quarantine=self.quarantine,out=self.review)

    def approve(self):
        self.make_preview();doc=receipts.read(self.review);doc.update(reviewer='fixture coordinator',reason='Discard the exact invalid unsubmitted batch only',
           quarantine_verdict='discarded_invalid_no_formal_write',quarantine_note='The whole invalid local batch was publicly released and has no formal write.')
        for row in doc['failed_reviews']:row.update(verdict='discarded_with_released_batch',note='Exact local preparation failed; this released batch is discarded and never retried.')
        receipts.write(self.review,doc)

    def test_explicit_quarantine_keeps_failure_and_does_not_accept_old_quality(self):
        out=self.make_preview();self.assertEqual(out['approval'],'pending')
        with self.assertRaises(ValueError):released.apply(self.c['id'],review=self.review)
        self.approve();old=self.owner_events.read_bytes()
        frozen={str(p):p.read_bytes() for p in [self.d/'campaign.json',self.d/'results'/f'{self.bid}.json',self.d/'reviews'/f'{self.bid}.sheet.json',self.root2/'worker.json',self.root2/'native/latest.json']}
        result=released.apply(self.c['id'],review=self.review)
        self.assertFalse(result['old_quality_accepted']);self.assertFalse(result['dispatch_resumed'])
        self.assertEqual(self.owner_events.read_bytes(),old)
        self.assertTrue(all(Path(p).read_bytes()==value for p,value in frozen.items()))
        self.assertTrue(worker_reconciled(ec.load(self.d),self.d,'m',2))
        self.assertFalse(supervision.normal_recovery('quality_process_violation'))
        c=ec.load(self.d);c['dispatch']={'state':'running','id':'new-legitimate-generation'};ec.save(self.d,c)
        with (self.d/'log.jsonl').open('a') as f:f.write(json.dumps({'batch':'different-new-batch','id':999,'status':'ok'})+'\n')
        self.assertTrue(worker_reconciled(ec.load(self.d),self.d,'m',2))
        native=receipts.read(self.root2/'native/latest.json');native['attempt']='new-owner-attempt';receipts.write(self.root2/'native/latest.json',native)
        self.assertFalse(worker_reconciled(ec.load(self.d),self.d,'m',2))

    def test_persistent_manager_can_schedule_original_owner_only_after_explicit_proof(self):
        from tools import campaign_recovery_manager as manager
        manager.enable(self.c['id'],reason='fixture explicit durable recovery policy')
        self.approve();released.apply(self.c['id'],review=self.review)
        c=ec.load(self.d);c['dispatch']={'state':'running','id':'legitimate-new-generation'};ec.save(self.d,c)
        plan=manager.snapshot(self.c['id']);row=next(x for x in plan['slots'] if (x['model'],x['slot'])==('m',2))
        self.assertEqual(row['action'],'supervise');self.assertEqual(row['session'],self.owner_session)
        self.assertTrue(row['published_continuation_valid'])

    def test_formal_ack_partial_release_or_missing_enforced_policy_is_not_quarantine(self):
        original=ec.load(self.d)
        for kind in ['submitted','partial','policy','formal_ack']:
            c=json.loads(json.dumps(original));b=ec.find_batch(c,self.bid)
            if kind=='submitted':b['status']='submitted'
            if kind=='partial':b['released_ids']=b['released_ids'][:-1]
            if kind=='policy':c.pop('source_decisions')
            if kind=='formal_ack':
                with (self.d/'log.jsonl').open('a') as f:f.write(json.dumps({'batch':self.bid,'id':b['ids'][0],'status':'ok'})+'\n')
            ec.save(self.d,c)
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.make_preview()
        ec.save(self.d,original)

    def test_submit_uncertain_and_real_NAS_or_permission_refusal_never_qualify(self):
        op=self.d/'operations'/self.bid/'latest.json';old=receipts.read(op)
        for change in [{'kind':'submit'},{'kind':'unknown-write'},{'may_have_written':True},{'state':'uncertain'}]:
            receipts.write(op,{**old,**change})
            with self.subTest(change=change),self.assertRaises(ValueError):self.make_preview()
        receipts.write(op,old)
        row=json.loads(json.dumps(self.rows[2]))
        for code,output in [(75,'NAS unavailable'),(1,'HTTP Error 403: Forbidden')]:
            self.rows[2]['item'].update(exit_code=code,aggregated_output=output);self.save_rows()
            with self.assertRaises(ValueError):self.make_preview()
        self.rows[2]=row;self.save_rows()

    def test_stale_quality_and_missing_failed_review_cannot_activate(self):
        self.approve();doc=receipts.read(self.review);doc['failed_reviews']=[];receipts.write(self.review,doc)
        with self.assertRaises(ValueError):released.apply(self.c['id'],review=self.review)
        self.approve();q=receipts.read(self.quarantine);Path(q['refs'][0]['file']).write_text('changed')
        with self.assertRaises(ValueError):released.apply(self.c['id'],review=self.review)
        self.assertFalse(worker_reconciled(ec.load(self.d),self.d,'m',2))

    def test_failed_graph_write_other_batch_and_dynamic_process_cannot_be_discarded(self):
        for command in [self.command.replace('prepare','submit'),self.command.replace(self.bid,'another-batch'),
                        'python3 - <<\'PY\'\nimport subprocess\nsubprocess.run(["true"])\nPY',
                        'python3 - <<\'PY\'\nimport pathlib\npathlib.Path("/etc/config").write_text("bad")\nPY']:
            with self.subTest(command=command),self.assertRaises(ValueError):released._preparation(command,self.c['id'],self.bid)

    def test_preview_cannot_replace_old_quality_or_quarantine_evidence(self):
        for output in [self.d/'results'/f'{self.bid}.json',self.quarantine]:
            before=output.read_bytes()
            with self.assertRaises(ValueError):released.preview(self.c['id'],'m',2,self.bid,quarantine=self.quarantine,out=output)
            self.assertEqual(output.read_bytes(),before)


if __name__=='__main__':unittest.main()
