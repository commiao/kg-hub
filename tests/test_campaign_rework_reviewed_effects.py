import copy
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools import campaign_rework_reviewed_effects as effects
from tools.campaign_rework_evidence import digest


class ReviewedEffectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.c = self.root/'campaign'; self.c.mkdir()
        self.releases = self.root/'releases'; (self.releases/'revision').mkdir(parents=True)
        self.sessions = self.root/'sessions'; self.sessions.mkdir()
        start={'id':'item_1','type':'command_execution','command':'reviewed command'}
        end={**start,'status':'completed','exit_code':0}
        self.rows=[{'type':'item.started','item':start},{'type':'item.completed','item':end}]
        self.events=b'\n'.join(effects.encoded(r) for r in self.rows)+b'\n'
        (self.c/'events.jsonl').write_bytes(self.events)
        (self.c/'ledger.json').write_bytes(b'{}')
        (self.releases/'revision/module.py').write_bytes(b'# reviewed source\n')
        identity={'release':'revision'}
        manifest={'identity':identity,'files':{'events':{'anchor':'campaign','path':'events.jsonl','sha256':digest(self.events)}}}
        self.context={'identity':identity,'manifest':manifest}
        self.profile={'schema':'reviewed-rework-source-effects-v1','restart_allowed':False,
          'identity_sha256':digest(effects.encoded(identity)),
          'manifest_sha256':digest(effects.encoded(manifest)),
          'events_sha256':digest(self.events),
          'commands':[{'item':'item_1','command_sha256':digest(start['command'].encode()),
                       'classification':'explicit_public_repair_apply',
                       'native_terminal':{'status':'completed','exit_code':0}}],
          'file_changes_sha256':digest(effects.encoded([])),'file_change_count':0,
          'release_files':{'module.py':digest(b'# reviewed source\n')},
          'campaign_files':{'ledger.json':digest(b'{}')},
          'repair_inventory':[{'item':'item_1'}]}

    def inspect(self):
        return effects._inspect(self.context,self.c,self.releases,self.sessions,self.profile)

    def test_bound_source_review_does_not_authorize_restart(self):
        result=self.inspect()
        self.assertEqual(result['command_count'],1)
        self.assertTrue(result['source_effect_review_bound'])
        for key in ('restart_allowed','semantic_acceptance','actual_process_locks_validated',
                    'live_repairs_reconciled','original_process_import_trace_available'):
            self.assertIs(result[key],False)

    def test_public_reader_requires_fixed_private_profile_bytes(self):
        profile_path=self.c/effects.PROFILE_RELATIVE
        profile_path.parent.mkdir()
        raw=effects.encoded(self.profile);profile_path.write_bytes(raw)
        with mock.patch.object(effects,'PROFILE_SHA256',digest(raw)):
            result=effects.inspect(self.context,self.c,self.releases,self.sessions)
            self.assertIs(result['restart_allowed'],False)
            profile_path.write_bytes(raw+b' ')
            with self.assertRaises(ValueError):
                effects.inspect(self.context,self.c,self.releases,self.sessions)

    def test_changed_identity_or_manifest_cannot_self_pin(self):
        for target in ('identity','manifest'):
            with self.subTest(target=target):
                old=copy.deepcopy(self.context)
                self.context[target]['injected']='new approval'
                with self.assertRaises(ValueError):self.inspect()
                self.context=old

    def test_original_command_and_terminal_cannot_be_replaced(self):
        for replacement in (b'{}',self.events.replace(b'"exit_code":0',b'"exit_code":-1'),
                            self.events.replace(b'reviewed command',b'unknown write')):
            with self.subTest(replacement=replacement[:20]):
                (self.c/'events.jsonl').write_bytes(replacement)
                with self.assertRaises(ValueError):self.inspect()
        (self.c/'events.jsonl').write_bytes(self.events)

    def test_review_must_cover_every_command_and_graph_write(self):
        for mutate in (lambda p:p['commands'].clear(),
                       lambda p:p['commands'].append(copy.deepcopy(p['commands'][0])),
                       lambda p:p['repair_inventory'].clear(),
                       lambda p:p['repair_inventory'].append({'item':'item_1'}),
                       lambda p:p['commands'][0].update(command_sha256='0'*64),
                       lambda p:p.update(file_change_count=1)):
            with self.subTest(mutate=mutate):
                old=copy.deepcopy(self.profile);mutate(self.profile)
                with self.assertRaises(ValueError):self.inspect()
                self.profile=old

    def test_changed_retained_module_or_local_ledger_rejected(self):
        for path in (self.releases/'revision/module.py',self.c/'ledger.json'):
            with self.subTest(path=path):
                old=path.read_bytes();path.write_bytes(b'changed')
                with self.assertRaises(ValueError):self.inspect()
                path.write_bytes(old)

    def test_symlink_retained_source_rejected_even_same_bytes(self):
        path=self.releases/'revision/module.py'
        real=self.root/'same.py';real.write_bytes(path.read_bytes())
        path.unlink();path.symlink_to(real)
        with self.assertRaises((ValueError,OSError)):self.inspect()


if __name__=='__main__':unittest.main()
