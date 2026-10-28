"""Maintenance review never replays a failed write or changes semantic acceptance."""
from copy import deepcopy
import json
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_campaign_rework import ReworkTests
from tools import campaign_rework_maintenance as maintenance, extract_campaign as ec
from utils import process_receipts as receipts


class MaintenanceTests(unittest.TestCase):
    setUpBase,tearDown=ReworkTests.setUpBase,ReworkTests.tearDown
    claim,finish=ReworkTests.claim,ReworkTests.finish
    write_events,preview,apply=ReworkTests.write_events,ReworkTests.preview,ReworkTests.apply
    spawn,run_rework=ReworkTests.spawn,ReworkTests.run_rework

    def setUp(self):
        ReworkTests.setUp(self)
        self.audit=self.d/'audit/maintenance';self.audit.mkdir(parents=True)
        original=receipts.read(self.d/'batches'/f'{self.bid}.json');ids=[x['id'] for x in original['items']]
        receipts.write(self.audit/'selector.json',{k:original['items'][1][k] for k in ['source_description','source_obs_id']})
        receipts.write(self.audit/'checkpoint.json',{'batch':self.bid,'reviewed':ids[:1],'pending':ids[1:],'in_flight':None})
        job=self.root2/'quality-reworks/original';job.mkdir(parents=True)
        assignment=job/'assignment.md';assignment.write_text('Audit the original sources only; no fresh quota.')
        receipts.write(job/'scope.json',{'campaign':self.c['id'],'model':'m','slot':2,'session':self.owner_session,
                       'batches':[self.bid],'new_source_quota':0,'assignment':maintenance._ref(assignment)})
        self.owner_events=job/'events.jsonl'
        self.command=shlex.join([str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser()),'repair','preview',
                                 '--file',str(self.audit/'selector.json'),'--out',str(self.audit/'snapshot.json')])
        self.report={'recovery_kind':'infrastructure_failed','batch':self.bid,'model':'m','in_flight':None,
                     'reviewed':1,'unfinished_sources':ids[1:]}
        self.rows=[{'type':'thread.started','thread_id':self.owner_session},
                   {'type':'item.started','item':{'type':'command_execution','id':'read-1','command':self.command}},
                   {'type':'item.completed','item':{'type':'command_execution','id':'read-1','command':self.command,
                                                   'exit_code':1,'aggregated_output':'http.client.RemoteDisconnected: closed'}},
                   {'type':'item.completed','item':{'type':'agent_message','text':json.dumps(self.report)}},
                   {'type':'turn.completed'}]
        self.save_rows()
        receipts.write(self.root2/'worker.json',{'session':self.owner_session,'events':str(self.owner_events),
                       'mode':'quality_rework','batches':[self.bid]})
        native=receipts.read(self.root2/'native/latest.json');native.update(started_at='2026-10-28T00:00:00+00:00',ended_at='2026-10-28T00:02:00+00:00')
        receipts.write(self.root2/'native/latest.json',native)
        receipts.write(job/'exit-status.json',{'session':self.owner_session,'exit_code':0,'ended_at':native['ended_at']})
        self.release={}
        for k,obj in {'batch':{'frozen_commit':'a'*40,'authorization':'fixture authorized maintenance','operator':'fixture','at':'2026-10-28T00:01:00+00:00'},
                      'completed':{'commit':'a'*40,'at':'2026-10-28T00:03:00+00:00'},
                      'capabilities':{'http':200,'body':{'revision':'provider-after'}}}.items():
            p=self.root/(k+'.json');receipts.write(p,obj);self.release[k]=str(p)
        self.spec=self.root/'maintenance-spec.json';receipts.write(self.spec,{'audit_dir':str(self.audit),'release':self.release})
        self.review=self.root/'maintenance-review.json'

    def save_rows(self):self.owner_events.write_text(''.join(json.dumps(x)+'\n' for x in self.rows))

    def sheet(self):
        return maintenance.sheet(self.c['id'],'m',2,evidence=self.spec,out=self.review)

    def approve(self):
        self.sheet();doc=receipts.read(self.review);doc['reviewer']='fixture coordinator'
        for row in doc['reviews']:
            row.update(verdict='readonly_transport_failure',note='Reviewed the exact failed public read; no remote write was initiated.')
        receipts.write(self.review,doc);return doc

    def validate(self):return maintenance.validate(ec.load(self.d),self.d,'m',2,[self.bid],self.review)

    def test_sheet_is_pending_and_review_cannot_grant_quality_or_claims(self):
        before=(self.d/'campaign.json').read_bytes();out=self.sheet()
        self.assertEqual(out['approval'],'pending')
        self.assertEqual((self.d/'campaign.json').read_bytes(),before)
        with self.assertRaises(ValueError):self.validate()
        self.approve();proof=self.validate()
        self.assertEqual(proof['inventory']['semantic_acceptance'],'not_granted')
        self.assertEqual(proof['inventory']['new_source_quota'],0)

    def test_same_owner_public_rework_resumes_and_archives_unchanged_failed_evidence(self):
        self.approve();old=self.owner_events.read_bytes()
        from tools import campaign_rework
        result=campaign_rework.run(self.c['id'],'m',2,batches=[self.bid],assignment=self.assignment,reason='fixture explicit maintenance continuation',
                 maintenance_review=self.review,spawn=self.spawn,preflight=lambda *a:{'ok':True,'server':{'revision':'provider-after'}})
        self.assertEqual(result['state'],'rework_turn_completed')
        self.assertEqual(self.owner_events.read_bytes(),old)
        self.assertTrue((Path(result['evidence_dir'])/'maintenance-continuation.json').exists())
        self.assertEqual(ec.load(self.d)['dispatch']['state'],'draining')
        with self.assertRaises(ValueError):self.validate() # the original native proof was consumed

    def test_default_rework_still_refuses_infrastructure_stop(self):
        with self.assertRaisesRegex(ValueError,'original clean'):self.run_rework()
        self.assertFalse(self.spawned)

    def test_stale_audit_input_transcript_and_review_are_refused(self):
        for name in ['audit','transcript','review']:
            with self.subTest(name=name):
                self.approve()
                p={'audit':self.audit/'selector.json','transcript':self.owner_events,'review':self.review}[name]
                before=p.read_bytes();p.write_bytes(before+b' ')
                if name=='review':
                    doc=receipts.read(p);doc['reviews'][0]['command_sha256']='0'*64;receipts.write(p,doc)
                with self.assertRaises(ValueError):self.validate()
                p.write_bytes(before)

    def test_failed_write_refusal_NAS_timeout_and_inflight_are_never_disposed(self):
        original=deepcopy(self.rows)
        for kind in ['write','permission','nas','unknown_write','duplicate_session','unpaired','provider']:
            with self.subTest(kind=kind):
                self.rows=deepcopy(original)
                if kind=='write':
                    for r in self.rows[1:3]:r['item']['command']=self.command.replace('repair preview','repair apply')
                elif kind=='permission':self.rows[2]['item']['aggregated_output']='HTTP Error 403: Forbidden'
                elif kind=='nas':self.rows[2]['item']['exit_code']=75
                elif kind=='unknown_write':
                    report=dict(self.report,in_flight={'method':'POST'});self.rows[3]['item']['text']=json.dumps(report)
                elif kind=='duplicate_session':self.rows.insert(1,{'type':'thread.started','thread_id':self.owner_session})
                elif kind=='unpaired':self.rows.pop(1)
                elif kind=='provider':self.rows.insert(1,{'type':'error','message':'provider unavailable'})
                self.save_rows()
                with self.assertRaises(ValueError):self.sheet()
        self.rows=original;self.save_rows()

    def test_fresh_matching_preflight_and_immutable_evidence_are_required(self):
        self.approve()
        for flight in [{'ok':False},{'ok':True,'server':{'revision':'old'}}]:
            with self.subTest(flight=flight),self.assertRaises(ValueError):maintenance.restored(self.validate(),flight)
        from tools import campaign_rework
        def change(*a):
            (self.audit/'new-write.json').write_text('{}')
            return {'ok':True,'server':{'revision':'provider-after'}}
        with self.assertRaises(ValueError):campaign_rework.run(self.c['id'],'m',2,batches=[self.bid],assignment=self.assignment,reason='fixture',
                        maintenance_review=self.review,spawn=self.spawn,preflight=change)
        self.assertFalse(self.spawned)

    def test_pending_partial_checkpoint_and_missing_review_row_are_not_accepted(self):
        self.approve();doc=receipts.read(self.review);doc['reviews']=[];receipts.write(self.review,doc)
        with self.assertRaises(ValueError):self.validate()
        cp=receipts.read(self.audit/'checkpoint.json');cp['in_flight']={'operation':'write'}
        receipts.write(self.audit/'checkpoint.json',cp)
        with self.assertRaises(ValueError):self.sheet()

    def test_local_helper_must_not_execute_process_network_dynamic_or_outside_writes(self):
        for code in ['import subprocess\nsubprocess.run(["true"])','import urllib.request',
                     'eval("1+1")','import pathlib\npathlib.Path("/etc/config").write_text("bad")',
                     'import json\njson=print','import pathlib\np=pathlib.Path("../secret")']:
            with self.subTest(code=code),self.assertRaises(ValueError):maintenance._local_script(code,self.audit)

    def test_bounded_local_writes_allowed_but_dynamic_destination_and_rebinding_refused(self):
        good=f'import pathlib\nr=pathlib.Path({str(self.audit)!r})\n(r/"note.json").write_text("fixture")'
        maintenance._local_script(good,self.audit)
        for code in [f'import pathlib,json\nr=pathlib.Path({str(self.audit)!r})\np=json.loads("{{}}")\np.write_text("bad")',
                     f'import pathlib\nr=pathlib.Path({str(self.audit)!r})\ni=int("3")\ni="dynamic"\n(r/f"{{i}}.json").write_text("bad")']:
            with self.subTest(code=code),self.assertRaises(ValueError):maintenance._local_script(code,self.audit)

    def test_other_slot_unknown_command_and_custom_preview_endpoint_refused(self):
        self.approve()
        with self.assertRaises(ValueError):maintenance.validate(ec.load(self.d),self.d,'m',3,[self.bid],self.review)
        original=deepcopy(self.rows)
        for command in ['false',self.command+' --url https://other.invalid',
                        self.command.replace(str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser()),'/private/tmp/other/extract')]:
            self.rows=deepcopy(original)
            for row in self.rows[1:3]:row['item']['command']=command
            self.save_rows()
            with self.subTest(command=command),self.assertRaises(ValueError):self.sheet()
        self.rows=original;self.save_rows()

    def test_review_cannot_adopt_another_invocation(self):
        self.approve()
        with self.assertRaises(ValueError):self.run_rework(maintenance_review=self.review,adopt_events=self.owner_events,adopt_exit=self.owner_events.with_name('exit-status.json'))
        self.assertFalse(self.spawned)

    def test_maintenance_release_must_cover_original_stop(self):
        p=Path(self.release['completed']);obj=receipts.read(p);obj['at']='2026-10-27T00:00:00+00:00';receipts.write(p,obj)
        with self.assertRaises(ValueError):self.sheet()


if __name__=='__main__':unittest.main()
