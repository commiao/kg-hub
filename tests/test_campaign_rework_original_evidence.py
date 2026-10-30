"""Synthetic complete file bundle; no real campaign or rollout data."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tools import campaign_rework_original_evidence as loader
from tools.campaign_rework_evidence import digest
from test_campaign_rework_host_receipt import fixture,mutate_script

class OriginalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name).resolve();self.anchors={'campaign':base/'campaign','sessions':base/'sessions','release':base/('c'*40)}
        for p in self.anchors.values():p.mkdir()
        job='supervisors/model/quality-reworks/'+'b'*32;bid='model-001'
        f=fixture();mutate_script(f,lambda s:s.replace('/tmp/campaign',str(self.anchors['campaign'])))
        f['scope']['campaign_root']=str(self.anchors['campaign'])
        self.identity={'campaign':'campaign-test','model':'model','job_relative':job,'batch':bid,
                       'session':'session-a','turn':'turn-a','cwd':'/tmp/excluded-cwd',
                       'release':'c'*40,'native_attempt':'a'*32,'native_child_pid':123,
                       'audit_relative':'audit/review','published_entry':'/tmp/published/tools/extract',
                       'builder_patch_call':'builder-call'}
        self.paths={'events':job+'/events.jsonl','scope':job+'/scope.json','assignment':job+'/assignment.md',
                    'exit':job+'/exit-status.json','batch':'batches/'+bid+'.json','worker':'supervisors/model/worker.json',
                    'native':'supervisors/model/native/latest.json','rollout':'2026/01/01/rollout-2026-01-01T00-00-00-session-a.jsonl',
                    'quality_module':'utils/extraction_quality.py','utils_init':'utils/__init__.py','shell_entry':'tools/extract'}
        assignment="Save fresh audit material under this campaign's audit/review/ directory.\n"
        scope={'campaign':'campaign-test','model':'model','slot':None,'session':'session-a','batches':[bid],'new_source_quota':0,
               'assignment':{'path':str(self.anchors['campaign']/self.paths['assignment']),'sha256':digest(assignment.encode())},
               'preflight':{'ok':True,'revision':'c'*40,'loaded_code_root':str(self.anchors['release'])}}
        native={'attempt':'a'*32,'state':'completed','kind':'native_cli','exit_code':1,'child_pid':123,
                'started_at':f['scope']['native_started_at'],'ended_at':f['scope']['native_ended_at']}
        worker={'session':'session-a','mode':'quality_rework','batches':[bid],
                'events':str(self.anchors['campaign']/self.paths['events'])}
        batch={'items':[{'id':n,'source_description':'synthetic '+str(n),'source_obs_id':str(n)} for n in range(101,151)]}
        patch='*** Begin Patch\n*** Add File: '+str(self.anchors['campaign'])+'/audit/review/build-title-repairs.py\n+pass\n*** End Patch'
        host=[{'type':'session_meta','payload':{'id':'session-a','cwd':'/tmp/excluded-cwd'}}]+f['host_rows']+[
              {'payload':{'type':'custom_tool_call','call_id':'builder-call','internal_chat_message_metadata_passthrough':{'turn_id':'turn-a'},
                          'input':'const patch='+json.dumps(patch)+';\ntext(await tools.apply_patch(patch))\n'}}]
        # This bundle tests loading; separate classifier tests exercise local failures.
        events=[row for row in f['native_rows'] if row.get('item',{}).get('id')!='failed-tool']
        objects={'scope':scope,'native':native,'worker':worker,'batch':batch,'exit':{'session':'session-a','exit_code':1,'ended_at':native['ended_at']}}
        self.data={k:(json.dumps(v)+'\n').encode() for k,v in objects.items()}
        self.data.update(assignment=assignment.encode(),events=('\n'.join(map(json.dumps,events))+'\n').encode(),
                         rollout=('\n'.join(map(json.dumps,host))+'\n').encode(),quality_module=b'import json\n',utils_init=b'',shell_entry=b'#!/bin/sh\n')
        self.manifest={'schema':'offline-rework-original-evidence-v0','identity':self.identity.copy(),'files':{}}
        for role,raw in self.data.items():
            anchor='sessions' if role=='rollout' else 'release' if role in ('quality_module','utils_init','shell_entry') else 'campaign'
            path=self.anchors[anchor]/self.paths[role];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            self.manifest['files'][role]={'anchor':anchor,'path':self.paths[role],'sha256':digest(raw)}
    def load(self,manifest=None,identity=None):
        raw=json.dumps(manifest or self.manifest).encode()
        return loader.load(raw,digest(raw),self.anchors,self.identity if identity is None else identity)
    def change_object(self,role,change):
        obj=json.loads(self.data[role]);change(obj);raw=json.dumps(obj).encode()
        path=self.anchors[self.manifest['files'][role]['anchor']]/self.paths[role];path.write_bytes(raw)
        self.manifest['files'][role]['sha256']=digest(raw)
    def test_synthetic_complete_bundle_has_no_restart_permission(self):
        result=self.load();self.assertEqual(result['raw_evidence_files_verified'],11)
        self.assertEqual(result['host_evidence']['host_exit_code'],-1)
        self.assertIs(result['restart_allowed'],False)
        self.assertIs(result['live_leases_policy_and_writes_validated'],False)
    def test_context_cannot_be_self_asserted_by_manifest(self):
        for key in ('campaign','session','turn','native_attempt','release','cwd','audit_relative'):
            m=copy.deepcopy(self.manifest);m['identity'][key]='foreign'
            with self.subTest(key=key),self.assertRaises(ValueError):self.load(m)
        raw=json.dumps(self.manifest).encode()
        with self.assertRaises(ValueError):loader.load(raw,'0'*64,self.anchors,self.identity)
    def test_missing_extra_or_relocated_role(self):
        for kind in ('missing','extra','relocated'):
            m=copy.deepcopy(self.manifest)
            if kind=='missing':m['files'].pop('assignment')
            elif kind=='extra':m['files']['extra']=m['files']['assignment']
            else:m['files']['assignment']=m['files']['batch']
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.load(m)
    def test_original_scope_and_native_reject_rehashed_inconsistency(self):
        cases=[('scope',lambda d:d.update(new_source_quota=50)),('scope',lambda d:d.update(slot=2)),
               ('scope',lambda d:d['preflight'].update(revision='d'*40)),
               ('native',lambda d:d.update(exit_code=0)),('native',lambda d:d.update(child_pid=456)),
               ('worker',lambda d:d.update(session='foreign')),('batch',lambda d:d['items'].pop())]
        for role,change in cases:
            original=self.data[role];ref=copy.deepcopy(self.manifest['files'][role])
            self.change_object(role,change)
            with self.subTest(role=role),self.assertRaises(ValueError):self.load()
            (self.anchors[ref['anchor']]/ref['path']).write_bytes(original);self.manifest['files'][role]=ref

if __name__=='__main__':unittest.main()
