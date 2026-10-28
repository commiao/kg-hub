"""Separate repair admission and exact read-only recovery with real receipts."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from test_campaign_continuation import ContinuationTests
from test_campaign_prepare import confirm
from tools import campaign_local_recovery as local, campaign_continuation as cc
from tools import campaign_decisions as decisions, extract_campaign as ec, agent_extract as ax
from utils import process_receipts as receipts


class LocalRecoveryTests(unittest.TestCase):
    setUpBase=ContinuationTests.setUpBase
    setUp=ContinuationTests.setUp
    tearDown=ContinuationTests.tearDown
    gather=ContinuationTests.gather
    post=ContinuationTests.post
    fail_upload=ContinuationTests.fail_upload
    worker=ContinuationTests.worker
    transport_trace=ContinuationTests.transport_trace

    def settled_operation(self):
        p=self.d/'operations'/self.bid/'latest.json';v=receipts.read(p)
        v.update(state='completed',exit_code=0,may_have_written=False,requires_reconciliation=False)
        receipts.write(p,v)
        return p,v

    def repair(self):
        c=ec.load(self.d);b=ec.find_batch(c,self.bid);b['status']='submitted';ec.save(self.d,c)
        self.settled_operation()
        batch={**self.batch,'items':[self.batch['items'][0]]}
        results={'items':[self.results['items'][0]]};prompt=ec.batch_rules_text(self.d,c,b)
        review=confirm(ax.quality.review_sheet(batch,results,prompt,ec.batch_identity(c,b)),batch)
        payload=ax.build_payload(batch['items'][0],results['items'][0],model='m',session=c['id'],executor='codex',
            run_id=c['models']['m']['run_id'],prompt_version=ec.episode_prompt_version(c,b['chain']))
        payload.update(quality_review=review,quality_context={'batch':batch,'results':results,'prompt_text':prompt,
                       'prompt_version':payload['prompt_version']})
        request={'repair_id':'synthetic-repair','reason':'reviewed wrong direction','reviewer':review['reviewer'],
            'expected_snapshot_sha256':'a'*64,'remove_edge_uuids':[], 'replacement':payload,
            **{k:batch['items'][0][k] for k in ('source_description','source_obs_id')}}
        return request,review

    def test_repair_registration_preserves_production_sheet_and_checks_frozen_scope(self):
        request,review=self.repair();before=ax.quality_file(self.d,self.bid).read_bytes()
        out=decisions.record_repair(self.c['id'],self.bid,request,reviewer=review['reviewer'])
        self.assertTrue(Path(out['path']).exists());self.assertEqual(ax.quality_file(self.d,self.bid).read_bytes(),before)
        changed=copy.deepcopy(request);changed['replacement']['quality_context']['batch']['lookahead']=[{'title':'foreign'}]
        with self.assertRaises(ValueError):decisions.record_repair(self.c['id'],self.bid,changed,reviewer=review['reviewer'])
        self.assertEqual(out,decisions.record_repair(self.c['id'],self.bid,request,reviewer=review['reviewer']))

    def test_closed_decision_rejection_has_explicit_original_owner_continuation(self):
        request,review=self.repair();root,event=self.worker('m',2,kind='business_permission_denied')
        row=self.d/'audit/repair-row.json';row.parent.mkdir(exist_ok=True);receipts.write(row,review['items'][0])
        command=f"/bin/zsh -lc '{Path('~/.local/share/kg-hub/current/tools/extract').expanduser()} campaign decision --id {self.c['id']} --batch {self.bid} --file {row} --reviewer reviewer'"
        events=[{'type':'thread.started','thread_id':'original-m-2'},
            {'type':'item.started','item':{'id':'rejected','type':'command_execution','command':command}},
            {'type':'item.completed','item':{'id':'rejected','type':'command_execution','command':command,'exit_code':1,
                'aggregated_output':'ValueError: one-source decisions require an opted-in open batch'}},
            {'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'recovery_kind':'business_permission_denied'})}},
            {'type':'turn.completed'}]
        event.write_text(''.join(json.dumps(r)+'\n' for r in events));original=event.read_bytes()
        out=local.reconcile(self.c['id'],'m',2,reason='review exact wrong entry',url='http://invalid',token=None,
             bid=self.bid,repair=request,reviewer=review['reviewer'],preflight=lambda *a:{'ok':True})
        self.assertEqual(out['kind'],'repair_decision_recovered')
        self.assertTrue(cc.worker_reconciled(ec.load(self.d),self.d,'m',2));self.assertEqual(event.read_bytes(),original)
        event.write_text(event.read_text()+'{}\n');self.assertFalse(cc.worker_reconciled(ec.load(self.d),self.d,'m',2))

    def test_readonly_preview_failure_requires_exact_input_and_healthy_service(self):
        root,event=self.worker('m',2,kind='infrastructure_failed')
        p,v=self.settled_operation();v.update(kind='dryrun',by_status={'dry_run':2,'http_502':1});receipts.write(p,v)
        with self.assertRaisesRegex(ValueError,'readiness'):
            local.reconcile(self.c['id'],'m',2,reason='service still broken',url='invalid',token=None,
                preflight=lambda *a:{'ok':False})
        out=local.reconcile(self.c['id'],'m',2,reason='service restored',url='invalid',token=None,
                preflight=lambda *a:{'ok':True})
        self.assertEqual(out['kind'],'readonly_preview_recovered')
        self.assertTrue(cc.worker_reconciled(ec.load(self.d),self.d,'m',2))
        v['may_have_written']=True;v['state']='uncertain';receipts.write(p,v)
        with self.assertRaises(ValueError):local.reconcile(self.c['id'],'m',2,reason='unsafe',url='invalid',token=None,
                preflight=lambda *a:{'ok':True})


if __name__=='__main__':unittest.main()
