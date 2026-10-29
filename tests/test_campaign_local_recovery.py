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

    def test_original_optional_null_session_keeps_frozen_model_tool_run_and_owner(self):
        request,review=self.repair();request['replacement']['extractor_session']=None
        out=decisions.record_repair(self.c['id'],self.bid,request,reviewer=review['reviewer'])
        value=receipts.read(Path(out['path']))
        self.assertEqual(value['owner_session'],'original-m-2')
        self.assertIsNone(value['request']['replacement']['extractor_session'])
        request['replacement']['extractor_session']='foreign-campaign'
        with self.assertRaises(ValueError):decisions.record_repair(self.c['id'],self.bid,request,reviewer=review['reviewer'])
        request['replacement']['extractor_session']=None
        request['replacement']['run_id']='foreign-run'
        with self.assertRaises(ValueError):decisions.record_repair(self.c['id'],self.bid,request,reviewer=review['reviewer'])

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

    def test_delegation_rejection_requires_real_public_readback_and_never_replays_prepare(self):
        import shutil
        root,event=self.worker('m',2,kind='business_permission_denied');self.settled_operation()
        shutil.rmtree(self.d/'operations'/self.bid)
        c=ec.load(self.d);ec.find_batch(c,self.bid)['status']='claimed';ec.save(self.d,c)
        (self.d/'log.jsonl').write_text('')
        entry=str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser())
        command=f"/bin/zsh -lc '{entry} campaign prepare --id {self.c['id']} --batch {self.bid}'"
        rows=[{'type':'thread.started','thread_id':'original-m-2'},
            {'type':'item.started','item':{'id':'prepare','type':'command_execution','command':command}},
            {'type':'item.completed','item':{'id':'prepare','type':'command_execution','command':command,'exit_code':1,
                'aggregated_output':'delegated campaign backend identity differs\n'}},
            {'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'recovery_kind':'business_permission_denied'})}},
            {'type':'turn.completed'}]
        event.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        with mock.patch.object(local.subprocess,'run',return_value=mock.Mock(returncode=1,stdout='')):
            with self.assertRaisesRegex(ValueError,'readback'):
                local.reconcile(self.c['id'],'m',2,bid=self.bid,prior_backend='b'*40,reason='blocked',url='invalid',token=None,
                                preflight=lambda *a:{'ok':True})
        with mock.patch.object(local.subprocess,'run',return_value=mock.Mock(returncode=0,stdout=json.dumps({'batch':self.bid}))) as probe:
            out=local.reconcile(self.c['id'],'m',2,bid=self.bid,prior_backend='b'*40,reason='verified stable entry',url='invalid',token=None,
                                preflight=lambda *a:{'ok':True})
            self.assertEqual(probe.call_args.args[0][1:3],['campaign','operation-status'])
        self.assertEqual(out['kind'],'delegation_epoch_recovered')
        self.assertTrue(cc.worker_reconciled(ec.load(self.d),self.d,'m',2))

    def test_bundle_core_rejection_uses_known_old_overlay_only_for_readonly_probe(self):
        self.worker('m',2,kind='infrastructure_failed');self.settled_operation();(self.d/'log.jsonl').write_text('')
        home=self.root/'fake-home';entry=home/'.local/share/kg-hub/current/tools/extract';entry.parent.mkdir(parents=True)
        entry.write_text('synthetic released entry');entry.with_name('extract_cli.py').write_text('synthetic verified loader')
        bundle=home/'.local/share/llm-eval/compositions/old/bundle.json';bundle.parent.mkdir(parents=True)
        receipts.write(bundle,{'delegates':{'kg_hub.entity_extract':{'commit':'b'*40}}})
        site=bundle.parent/'venv/lib/python3.13/site-packages';site.mkdir(parents=True)
        root=ec.supervisor_dir(self.d,'m',2);event=Path(receipts.read(root/'worker.json')['events'])
        cmd=f"/bin/zsh -lc '{entry} campaign operation-status --id {self.c['id']} --batch {self.bid}'"
        rows=[{'type':'thread.started','thread_id':'original-m-2'},
            {'type':'item.started','item':{'type':'command_execution','id':'status','command':cmd}},
            {'type':'item.completed','item':{'type':'command_execution','id':'status','command':cmd,'exit_code':2,'aggregated_output':'llm-eval: bundle core differs\n'}},
            {'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'recovery_kind':'infrastructure_failed'})}},
            {'type':'turn.completed'}]
        event.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        def probe(args,**kwargs):
            self.assertEqual(args[1:3],['campaign','operation-status']);self.assertEqual(kwargs['env']['PYTHONPATH'],str(site.resolve()))
            return mock.Mock(returncode=0,stdout=json.dumps({'batch':self.bid}))
        with mock.patch.dict('os.environ',{'HOME':str(home)}):
            out=local.reconcile(self.c['id'],'m',2,bid=self.bid,prior_backend='b'*40,prior_bundle=bundle,
                probe=probe,preflight=lambda *a:{'ok':True},reason='known overlay isolation restored',url='invalid',token=None)
            self.assertEqual(out['kind'],'delegation_epoch_recovered');self.assertTrue(local.valid(ec.load(self.d),self.d,'m',2))

    def capability_stop(self):
        import shutil
        root,event=self.worker('m',2,kind='infrastructure_failed')
        shutil.rmtree(self.d/'operations'/self.bid);(self.d/'log.jsonl').write_text('')
        c=ec.load(self.d);ec.find_batch(c,self.bid)['status']='claimed';ec.save(self.d,c)
        old={'ok':True,'server':{'revision':'old-provider','quality_versions':['extraction-quality-v1']}}
        path=self.d/'audit/original-preflight.json';path.parent.mkdir(exist_ok=True);receipts.write(path,old)
        cmd=f'''/bin/zsh -lc '"${{KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}}/tools/extract" agent preflight > /private/tmp/original-preflight.json
python3 -c "print(1)"' '''
        rows=[{'type':'thread.started','thread_id':'original-m-2'},
            {'type':'item.started','item':{'id':'flight','type':'command_execution','command':cmd}},
            {'type':'item.completed','item':{'id':'flight','type':'command_execution','command':cmd,'exit_code':0,'aggregated_output':json.dumps(old)}},
            {'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'recovery_kind':'infrastructure_failed'})}},
            {'type':'turn.completed'}]
        event.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        return path,event,rows

    def test_capability_restore_binds_real_old_receipt_and_new_provider_without_replaying_work(self):
        path,event,rows=self.capability_stop();before=event.read_bytes()
        args=dict(bid=self.bid,capability_receipt=path,require_capability='evidence_scope_v1',reason='provider published',url='invalid',token=None)
        with self.assertRaisesRegex(ValueError,'still lacks'):
            local.reconcile(self.c['id'],'m',2,preflight=lambda *a:{'ok':True,'server':{}},**args)
        flight={'ok':True,'server':{'title_qualifiers':['evidence_scope_v1']}}
        out=local.reconcile(self.c['id'],'m',2,preflight=lambda *a:flight,**args)
        self.assertEqual(out['kind'],'capability_restored');self.assertEqual(out['graph_writes'],0)
        self.assertTrue(local.valid(ec.load(self.d),self.d,'m',2));self.assertEqual(event.read_bytes(),before)
        receipts.write(path,{'ok':True,'server':{'revision':'fabricated'}})
        self.assertFalse(local.valid(ec.load(self.d),self.d,'m',2))
        with self.assertRaisesRegex(ValueError,'exact original'):
            local.reconcile(self.c['id'],'m',2,preflight=lambda *a:flight,**args)

    def test_capability_restore_never_clears_approval_or_written_operation(self):
        path,event,rows=self.capability_stop()
        args=dict(bid=self.bid,capability_receipt=path,require_capability='evidence_scope_v1',reason='provider published',url='invalid',token=None,
            preflight=lambda *a:{'ok':True,'server':{'title_qualifiers':['evidence_scope_v1']}})
        rows[2]['item'].update(exit_code=1,aggregated_output='This action was rejected due to unacceptable risk')
        event.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        with self.assertRaisesRegex(ValueError,'approval/NAS refusal'):
            local.reconcile(self.c['id'],'m',2,**args)
        rows[2]['item'].update(exit_code=0,aggregated_output=path.read_text());event.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        p=self.d/'operations'/self.bid/'latest.json';p.parent.mkdir(parents=True)
        receipts.write(p,{'state':'uncertain','may_have_written':True,'requires_reconciliation':True})
        with self.assertRaisesRegex(ValueError,'unknown operation'):
            local.reconcile(self.c['id'],'m',2,**args)
        p.unlink();(self.d/'log.jsonl').write_text(json.dumps({'batch':self.bid,'id':1,'status':'ok'})+'\n')
        with self.assertRaisesRegex(ValueError,'unstarted original batch'):
            local.reconcile(self.c['id'],'m',2,**args)

    def summary_handoff(self):
        import shlex
        from tools.campaign_summary_handoff import DENIAL
        request,review=self.repair();root,event=self.worker('m',2,kind='business_permission_denied')
        c=ec.load(self.d);b=ec.find_batch(c,self.bid);sid=b['ids'][0]
        b['reviews']=[{'obs':sid,'verdict':'error','kind':'random'}];ec.save(self.d,c)
        audit=self.d/'audit';audit.mkdir(exist_ok=True)
        def evidence(name,value):
            p=audit/name;receipts.write(p,value)
            return {'path':str(p),'sha256':cc.file_sha(p)}
        scope=evidence('denied-scope.json',{'operation':'summary','node':{'name':'script.sh'},'summary_to':'candidate'})
        preview=evidence('identity.json',{'http':200,'scope':{'operation':'summary','node':{'uuid':'original-node'}},
            'snapshot':{'nodes':[{'uuid':'original-node','props':{'name':'script.sh','summary':'Existing script identity'}}]}})
        accepted=evidence('accepted-source.json',{'accepted':True,'original_random_error_retained':True})
        shell=f'python3 - <<\'PY\'\nprint("preparation")\nPY\n"${{KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}}/tools/extract" nodes preview --file {scope["path"]} --out {audit}/unused.json'
        command='/bin/zsh -lc '+shlex.quote(shell)
        rows=[{'type':'thread.started','thread_id':'original-m-2'},
            {'type':'item.started','item':{'id':'denied','type':'command_execution','command':command}},
            {'type':'item.completed','item':{'id':'denied','type':'command_execution','command':command,'exit_code':1,'aggregated_output':DENIAL+'\n'}},
            {'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'recovery_kind':'business_permission_denied'})}},
            {'type':'turn.completed'}]
        event.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        value={'version':'shared-summary-handoff-v1','campaign':c['id'],'batch':self.bid,'model':'m','slot':2,
            'session':'original-m-2','events_sha256':cc.file_sha(event),'command_id':'denied','reviewer':'coordinator',
            'note':'Read identity and source repair; no shared summary mutation approved','accepted':True,
            'disposition':'identity_only_no_summary_change','scope':scope,'identity_preview':preview,'node_uuid':'original-node',
            'repairs':[{'source_id':sid,'repair_id':request['repair_id'],'acceptance':accepted}]}
        path=audit/'handoff.json';receipts.write(path,value)
        response={'status':'ok','repair':{'state':'applied','repair_id':request['repair_id'],'request':request,'result':{'verified':True}}}
        def http(method,url,payload,token):
            self.assertEqual(method,'GET');self.assertIsNone(payload)
            return 200,response
        args=dict(bid=self.bid,summary_handoff=path,reason='coordinator identity audit complete',url='invalid',token=None,
                  preflight=lambda *a:{'ok':True},http=http)
        return path,value,event,rows,response,args

    def test_summary_handoff_never_replays_governance_and_keeps_original_errors(self):
        path,value,event,rows,response,args=self.summary_handoff()
        before=event.read_bytes();original=ec.load(self.d)['batches']
        out=local.reconcile(self.c['id'],'m',2,**args)
        self.assertEqual(out['kind'],'summary_identity_handoff_accepted');self.assertEqual(out['graph_writes'],0)
        self.assertTrue(local.valid(ec.load(self.d),self.d,'m',2))
        self.assertEqual(event.read_bytes(),before);self.assertEqual(ec.load(self.d)['batches'],original)
        from tools.campaign_dispatch import resume
        self.assertEqual(resume(self.c['id'],reason='exact coordinator handoff accepted')['dispatch']['state'],'running')
        p=Path(value['identity_preview']['path']);p.write_text('{}')
        self.assertFalse(local.valid(ec.load(self.d),self.d,'m',2))

    def test_summary_handoff_rejects_foreign_unverified_or_missing_source_repair(self):
        path,value,event,rows,response,args=self.summary_handoff()
        base=copy.deepcopy(response)
        for change in ['unverified','foreign_source','foreign_model','missing_review']:
            response.clear();response.update(copy.deepcopy(base));candidate=copy.deepcopy(value)
            if change=='unverified':response['repair']['result']['verified']=False
            if change=='foreign_source':response['repair']['request']['source_obs_id']='foreign'
            if change=='foreign_model':response['repair']['request']['replacement']['extractor_model']='foreign'
            if change=='missing_review':candidate['repairs']=[]
            receipts.write(path,candidate)
            with self.subTest(change=change),self.assertRaises(ValueError):local.reconcile(self.c['id'],'m',2,**args)

    def test_summary_handoff_does_not_treat_source_text_as_a_permission_failure(self):
        path,value,event,rows,response,args=self.summary_handoff()
        command='cat frozen-source.json'
        items=[{'type':'item.started','item':{'id':'source','type':'command_execution','command':command}},
               {'type':'item.completed','item':{'id':'source','type':'command_execution','command':command,
                   'exit_code':0,'aggregated_output':'Historical source: diagnostic returned HTTP 401 without its token'}}]
        rows[1:1]=items;event.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        value['events_sha256']=cc.file_sha(event);receipts.write(path,value)
        self.assertEqual(local.reconcile(self.c['id'],'m',2,**args)['graph_writes'],0)

    def test_summary_handoff_rejects_other_denial_apply_and_changed_scope(self):
        path,value,event,rows,response,args=self.summary_handoff()
        for change in ['approval','apply','scope','foreign_session']:
            changed=copy.deepcopy(rows);candidate=copy.deepcopy(value)
            if change=='approval':changed[2]['item']['aggregated_output']='This action was rejected due to unacceptable risk'
            if change=='apply':
                for n in (1,2):changed[n]['item']['command']=changed[n]['item']['command'].replace('nodes preview','nodes apply')
            if change=='scope':candidate['scope']=candidate['identity_preview']
            if change=='foreign_session':candidate['session']='foreign'
            event.write_text(''.join(json.dumps(r)+'\n' for r in changed));candidate['events_sha256']=cc.file_sha(event)
            receipts.write(path,candidate)
            with self.subTest(change=change),self.assertRaises(ValueError):local.reconcile(self.c['id'],'m',2,**args)


if __name__=='__main__':unittest.main()
