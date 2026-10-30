"""Synthetic authority files; no caller-provided manifest grants a continuation."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tools import campaign_rework_context as context
import test_campaign_rework_original_evidence as fixture_module


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture_module.OriginalEvidenceTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;self.d=f.anchors['campaign'];self.sessions=f.anchors['sessions'];self.releases=f.anchors['release'].parent
        batch=json.loads(f.data['batch']);ids=[x['id'] for x in batch['items']]
        self.c={'id':'campaign-test','tool':'extract_campaign','executor':'codex','parallel':1,
                'models':{'model':{'run_id':'run'}},'dispatch':{'state':'draining'},
                'batches':[{'id':'model-001','model':'model','slot':None,'status':'submitted','ids':ids}]}
        self.op={'kind':'submit','state':'completed','exit_code':0,'identity':{'campaign':'campaign-test','batch':'model-001',
                 'extractor_model':'model','extractor_tool':'codex','run_id':'run'},
                 'source_keys':[{k:x[k] for k in ('source_description','source_obs_id')} for x in batch['items']]}
        self.write('campaign.json',self.c);self.write('operations/model-001/latest.json',self.op)
    def write(self,relative,obj):
        p=self.d/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj))
    def capture(self):
        return context.capture(self.d,'campaign-test','model',sessions_root=self.sessions,
                               releases_root=self.releases,workspace='/tmp/excluded-cwd')
    def test_context_derived_from_files_has_no_lock_or_retry_permission(self):
        result=self.capture();self.assertEqual(result['identity']['session'],'session-a')
        self.assertEqual(result['identity']['turn'],'turn-a')
        self.assertEqual(result['identity']['builder_patch_call'],'builder-call')
        self.assertEqual(result['evidence']['raw_evidence_files_verified'],11)
        self.assertEqual(len(result['authority']),13)
        for k in ('restart_allowed','actual_process_locks_validated','historical_deployment_attested'):
            self.assertIs(result[k],False)
    def test_state_or_ownership_change_rejected(self):
        mutations=[lambda c:c['dispatch'].update(state='running'),lambda c:c.update(parallel=2),
            lambda c:c.update(parallel=True),lambda c:c.update(executor='other'),
            lambda c:c.update(id='foreign'),lambda c:c.update(tool='foreign'),
            lambda c:c['batches'][0].update(model='foreign'),lambda c:c['batches'][0].update(status='claimed'),
            lambda c:c['batches'][0].update(slot=1),lambda c:c['batches'][0]['ids'].pop(),
            lambda c:c.update(slot_sessions={'model':{'1':'session-a'}})]
        for mutate in mutations:
            c=copy.deepcopy(self.c);mutate(c);self.write('campaign.json',c)
            with self.subTest(mutation=mutate),self.assertRaises(ValueError):self.capture()
        c=copy.deepcopy(self.c);c['models']['peer']={'run_id':'other'};self.write('campaign.json',c)
        self.write('supervisors/peer/worker.json',{'session':'session-a'})
        with self.assertRaises(ValueError):self.capture()
    def test_operation_scope_mismatch_rejected(self):
        mutations=[lambda o:o.update(exit_code=1),lambda o:o.update(exit_code=False),
            lambda o:o.update(state='running'),lambda o:o.update(kind='prepare'),
            lambda o:o['identity'].update(run_id='foreign'),lambda o:o['source_keys'].pop(),
            lambda o:o['source_keys'][0].update(source_obs_id='foreign')]
        for mutate in mutations:
            op=copy.deepcopy(self.op);mutate(op);self.write('operations/model-001/latest.json',op)
            with self.subTest(mutation=mutate),self.assertRaises(ValueError):self.capture()
    def test_ambiguous_or_symlinked_rollout_rejected(self):
        original=self.sessions/self.fixture.paths['rollout'];duplicate=original.with_name('rollout-2026-01-01T01-00-00-session-a.jsonl')
        duplicate.write_bytes(original.read_bytes())
        with self.assertRaises(ValueError):self.capture()
        duplicate.unlink();target=original.with_name('archive');original.rename(target);original.symlink_to(target)
        with self.assertRaises((ValueError,OSError)):self.capture()
    def test_authority_drift_during_capture_rejected(self):
        real=context.capture_regular;count=0
        def changing(anchor,relative,**kwargs):
            nonlocal count
            raw=real(anchor,relative,**kwargs)
            if relative=='supervisors/model/worker.json':
                count+=1
                if count==2:
                    obj=json.loads(raw);obj['session']='changed';return json.dumps(obj).encode()
            return raw
        with patch.object(context,'capture_regular',side_effect=changing),self.assertRaisesRegex(ValueError,'authority changed'):
            self.capture()
    def test_missing_native_invocation_is_not_inferred(self):
        original=json.loads(self.fixture.data['native'])
        for field,value in (('attempt',None),('attempt','wrong'),('child_pid',None),('child_pid',True),('child_pid',0)):
            obj=copy.deepcopy(original);obj[field]=value;self.write(self.fixture.paths['native'],obj)
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):self.capture()


if __name__=='__main__':unittest.main()
