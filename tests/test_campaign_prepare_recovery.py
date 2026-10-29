"""A legacy single worker may continue only its reconciled prepare NAS refusal."""
import json
import os
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_campaign_supervision as fixtures
from tools import campaign_supervision as s, campaign_recovery as recovery, extract_campaign as ec
from utils import process_receipts as r


class PrepareRecoveryTests(unittest.TestCase):
    setUp, tearDown = fixtures.SupervisorTests.setUp, fixtures.SupervisorTests.tearDown
    formal, native, run_supervisor = (fixtures.SupervisorTests.formal, fixtures.SupervisorTests.native,
                                     fixtures.SupervisorTests.run_supervisor)

    def fixture(self):
        self.assertNotIn('slot_sessions', self.c)
        self.exit, self.event = self.root/'old-exit.json', self.root/'old-events.jsonl'
        self.exit.write_text(json.dumps({'exit_code':0,'ended_at':'2026-10-25T00:00:00Z'}))
        command = '"${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract" campaign prepare --id synthetic --batch b'
        self.command = '/bin/zsh -lc ' + shlex.quote(command)
        rows = [{'type':'thread.started','thread_id':'same-session'},
                {'type':'item.completed','item':{'type':'command_execution','id':'gate1','exit_code':76,'command':self.command}},
                {'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'recovery_kind':'nas_refused'})}},
                {'type':'turn.completed'}]
        self.event.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.directory = self.d/'operations/b'
        keys=[{'source_description':'fixture','source_obs_id':str(i)} for i in (1,2)]
        (self.d/'batches').mkdir()
        (self.d/'batches/b.json').write_text(json.dumps({'items':keys}))
        with self.assertRaises(SystemExit):
            with r.attempt(self.directory, kind='dryrun', identity={'campaign':'synthetic','batch':'b',**ec.batch_identity(self.c,self.c['batches'][0])}) as update:
                update(source_keys=keys, may_have_written=False, phase='waiting_nas')
                raise SystemExit(76)
        self.before=self.event.read_bytes()

    def reconcile(self):
        record=r.read(self.directory/'latest.json')
        record.update(state='reconciled_not_written',requires_reconciliation=False,
                      reconciliation={'confirmed_not_written':True,'attempt':record['attempt']})
        r.write(self.directory/'latest.json',record)
        return record

    def test_legacy_same_session_supervisor_resumes_only_after_exact_reconciliation(self):
        self.fixture()
        calls=[]
        args=dict(session='same-session',adopt_exit=self.exit,adopt_events=self.event,
                  poll=.01,sleep=lambda _:None,spawn=self.native(calls))
        out=self.run_supervisor(**args)
        self.assertEqual(out['reason'],'nas_gate_refused')
        self.assertFalse(calls)
        self.reconcile()
        out=self.run_supervisor(**args)
        self.assertEqual(out['success'],2)
        for call in calls:self.assertEqual(call[call.index('resume')+1],'same-session')
        self.assertEqual(self.event.read_bytes(),self.before)
        self.assertEqual(r.read(self.directory/'latest.json')['exit_code'],76)
        self.assertNotIn('slot_sessions',ec.load(self.d))

    def test_prepare_requires_full_matching_nonwriting_dryrun_proof(self):
        self.fixture()
        original=self.reconcile()
        outcome=s.events(self.event)
        self.assertEqual(outcome['hard_stop'],76)
        self.assertTrue(recovery.stops_reconciled(self.c,self.d,'m',self.event,outcome))
        for field in ['attempt','kind','source','identity','written']:
            with self.subTest(field=field):
                record=json.loads(json.dumps(original))
                if field=='attempt':record['reconciliation']['attempt']='other'
                if field=='kind':record['kind']='submit'
                if field=='source':record['source_keys'].pop()
                if field=='identity':record['identity']['batch']='other'
                if field=='written':record['may_have_written']=True
                r.write(self.directory/'latest.json',record)
                self.assertFalse(recovery.stops_reconciled(self.c,self.d,'m',self.event,outcome))

    def test_prepare_parser_is_narrow(self):
        self.fixture()
        self.assertEqual(recovery.command_scope(self.command)['kind'],'dryrun')
        entry=str(Path(os.environ.get('KG_HUB_CODE','~/.local/share/kg-hub/current')).expanduser()/'tools/extract')
        base=f'{entry} campaign prepare --id synthetic --batch b'
        for suffix in [' --dry-run',' --fresh',' --sheet /tmp/a','; touch /tmp/no',' && true',' --batch other']:
            with self.subTest(suffix=suffix),self.assertRaises(ValueError):recovery.command_scope(base+suffix)
        self.assertIsNone(recovery.command_scope(base.replace('prepare','preparation')))
        self.assertEqual(recovery.command_scope(base.replace('prepare','submit'))['kind'],'submit')
        self.assertEqual(recovery.command_scope(base.replace('prepare','submit')+' --dry-run')['kind'],'dryrun')


if __name__=='__main__': unittest.main()
