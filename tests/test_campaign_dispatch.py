"""Observation-window bounds with real locks; no models or graph writes."""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import test_campaign_migration as fixtures
import test_campaign_parallel as parallel
from tools import campaign_dispatch as dispatch, campaign_migration as migration
from tools import campaign_supervision as supervision, extract_campaign as ec, agent_extract as ax
from utils import process_receipts as receipts


class DispatchTests(unittest.TestCase):
    setUp, tearDown, prepare, pending = (fixtures.MigrationTests.setUp, fixtures.MigrationTests.tearDown,
                                       fixtures.MigrationTests.prepare, fixtures.MigrationTests.pending)
    finish = parallel.ParallelCampaignTests.finish

    def activate(self):
        self.prepare()
        migration.apply(self.c['id'], self.out)
        migration.drain(self.c['id'], 'observation preflight')

    def claim(self, model='m', slot=1):
        with mock.patch.object(ax, 'gather_batch', side_effect=parallel.gather), mock.patch.object(ax, 'load_config', return_value={}):
            return ec.cmd_claim(self.c['id'], model, slot=slot, url='http://invalid', token=None, check=lambda rows: {})

    def test_two_new_batches_only_and_other_six_slots_cannot_launch_or_claim(self):
        self.activate()
        before = ec.load(self.d)
        dispatch.resume(self.c['id'], reason='user confirmed two observations', batches_per_model=1)
        with mock.patch.object(ax, 'gather_batch', side_effect=parallel.gather), mock.patch.object(ax, 'load_config', return_value={}):
            with ThreadPoolExecutor(8) as pool:
                results = list(pool.map(lambda pair: ec.cmd_claim(self.c['id'], pair[0], slot=pair[1],
                    url='http://invalid', token=None, check=lambda rows: {}),
                    [(model, slot) for model in ['m', 'n'] for slot in range(1, 5)]))
        claimed = [o for o in results if o.get('batch')]
        self.assertEqual(len(claimed), 2)
        c = ec.load(self.d)
        self.assertEqual(c['dispatch']['state'], 'draining')
        self.assertEqual((c['quota'], c['models'], c['batches'][:2]),
                         (before['quota'], before['models'], before['batches']))
        self.assertEqual(len({oid for b in c['batches'] for oid in b['ids']}), 102)
        self.assertTrue(self.claim()['paused'])
        for model in ['m', 'n']:
            for slot in [2, 3, 4]:
                with mock.patch.object(supervision, 'subprocess') as child:
                    result = supervision.run(self.c['id'], model, slot=slot, poll=.01,
                        preflight=lambda *a: self.fail('must not probe or launch'))
                    self.assertEqual(result['state'], 'drained')
                    child.Popen.assert_not_called()

    def test_original_pending_is_not_counted_and_resume_cannot_reset_window(self):
        self.pending()
        self.activate()
        first = dispatch.resume(self.c['id'], reason='observe', batches_per_model=1)
        self.assertEqual(self.claim()['batch'], 'm-002')
        self.finish('m-002')
        new = self.claim()
        self.assertEqual(new['batch'], 'm-003')
        migration.drain(self.c['id'], 'infrastructure stopped, preserve window')
        with self.assertRaisesRegex(ValueError, 'existing observation'):
            dispatch.resume(self.c['id'], reason='must not bypass', batches_per_model=None)
        again = dispatch.resume(self.c['id'], reason='same bounded window', batches_per_model=1)
        self.assertEqual(again['dispatch']['window']['id'], first['dispatch']['window']['id'])
        self.assertEqual(self.claim()['batch'], new['batch'])
        self.finish(new['batch'])
        self.assertTrue(self.claim()['paused'])

    def test_resume_rejects_live_uncertain_and_abnormal_workers(self):
        self.activate()
        native = ec.supervisor_dir(self.d, 'm', 1) / 'native'
        with receipts.attempt(native, kind='native_cli'):
            with self.assertRaises(receipts.Busy):
                dispatch.resume(self.c['id'], reason='live', batches_per_model=1)
        state = receipts.read(native / 'latest.json')
        state['requires_reconciliation'] = True
        receipts.write(native / 'latest.json', state)
        with self.assertRaises(receipts.Uncertain):
            dispatch.resume(self.c['id'], reason='unknown', batches_per_model=1)
        state['requires_reconciliation'] = False
        receipts.write(native / 'latest.json', state)
        events = self.proof['workers']['m']['events']
        with open(events, 'a') as stream:
            stream.write(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
                'text': json.dumps({'recovery_kind': 'approval_rejected'})}}) + '\n')
        with self.assertRaisesRegex(ValueError, 'terminal'):
            dispatch.resume(self.c['id'], reason='reject', batches_per_model=1)

    def test_spent_model_stops_supervision_without_spending_an_extra_native_turn(self):
        self.activate()
        dispatch.resume(self.c['id'], reason='observe', batches_per_model=1)
        out = self.claim()
        self.finish(out['batch'])
        with mock.patch.object(ec, 'workspace_dir', return_value=self.root):
            result = supervision.run(self.c['id'], 'm', slot=1, poll=.01,
                preflight=lambda *a: self.fail('spent window must not launch another turn'))
        self.assertEqual(result['state'], 'drained')
        self.assertEqual(ec.load(self.d)['dispatch']['state'], 'running')  # n still has one claim

    def test_clean_event_does_not_override_nonzero_exit_or_wrong_session_binding(self):
        self.activate()
        native = self.proof['workers']['m']['runtime_receipt_dir']
        from pathlib import Path
        path = Path(native) / 'latest.json'
        original = receipts.read(path)
        receipts.write(path, {**original, 'exit_code': 143})
        with self.assertRaisesRegex(ValueError, 'terminal'):
            dispatch.resume(self.c['id'], reason='nonzero must not resume', batches_per_model=1)
        receipts.write(path, original)
        c = ec.load(self.d)
        c['slot_sessions']['m']['1'] = 'wrong-session'
        ec.save(self.d, c)
        with self.assertRaisesRegex(ValueError, 'terminal'):
            dispatch.resume(self.c['id'], reason='binding must not change', batches_per_model=1)

    def test_unbounded_resume_after_completed_window_and_coordinator_only(self):
        self.activate()
        with mock.patch.dict(os.environ, {'KG_HUB_CAMPAIGN_SLOT': '1'}):
            with self.assertRaisesRegex(ValueError, 'coordinator'):
                dispatch.resume(self.c['id'], reason='worker must not resume', batches_per_model=1)
        dispatch.resume(self.c['id'], reason='observe', batches_per_model=1)
        for model in ['m', 'n']:
            out = self.claim(model)
            self.finish(out['batch'])
        result = dispatch.resume(self.c['id'], reason='user-authorized observation passed')
        self.assertNotIn('window', result['dispatch'])
        self.assertEqual(self.claim('n', 4)['slot'], 4)

    def nas_refusal(self):
        from pathlib import Path
        batch = self.pending()
        source = self.d / 'batches/m-002.json'
        frozen = json.loads(source.read_text())
        frozen['items'][0].update(source_description='synthetic observation', source_obs_id='source-3')
        source.write_text(json.dumps(frozen))
        self.activate()
        c = ec.load(self.d)
        keys = [{k: it[k] for k in ('source_description', 'source_obs_id')} for it in frozen['items']]
        directory = self.d / 'operations/m-002'
        with self.assertRaises(SystemExit):
            with receipts.attempt(directory, kind='dryrun', identity={
                    'campaign': c['id'], 'batch': batch['id'], **ec.batch_identity(c, batch)}) as update:
                update(phase='waiting_nas', source_keys=keys, may_have_written=False)
                raise SystemExit(76)
        event_path = Path(self.proof['workers']['m']['events'])
        entry = Path(os.environ.get('KG_HUB_CODE', '~/.local/share/kg-hub/current')).expanduser() / 'tools/extract'
        with event_path.open('a') as stream:
            for event in [
                    {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'gate1',
                        'command': f'{entry} campaign submit --id {c["id"]} --batch m-002 --dry-run', 'exit_code': 76}},
                    {'type': 'item.completed', 'item': {'type': 'agent_message',
                        'text': json.dumps({'recovery_kind': 'nas_refused'})}}, {'type': 'turn.completed'}]:
                stream.write(json.dumps(event) + '\n')
        return directory, event_path

    def test_readback_reconciled_nas_stop_resumes_without_erasing_original76(self):
        directory, events = self.nas_refusal()
        before = events.read_bytes()
        with self.assertRaisesRegex(ValueError, 'terminal'):
            dispatch.resume(self.c['id'], reason='not reconciled')
        proof = ec.cmd_reconcile_operation(self.c['id'], 'm-002', url='http://invalid', token=None,
            check=lambda rows, *a: {row['source_obs_id']: None for row in rows})
        self.assertTrue(proof['confirmed_not_written'])
        result = dispatch.resume(self.c['id'], reason='user authorized; sources absent and NAS now available')
        self.assertEqual(result['dispatch']['state'], 'running')
        self.assertEqual(events.read_bytes(), before)
        self.assertEqual(receipts.read(directory / 'latest.json')['exit_code'], 76)
        self.assertEqual(ec.load(self.d)['quota'], 2000)

    def test_nas_readback_must_be_complete_absent_same_attempt_kind_and_source(self):
        directory, _ = self.nas_refusal()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            ec.cmd_reconcile_operation(self.c['id'], 'm-002', url='http://invalid', token=None, check=lambda *a: {})
        proof = ec.cmd_reconcile_operation(self.c['id'], 'm-002', url='http://invalid', token=None,
            check=lambda rows, *a: {row['source_obs_id']: 'ok' for row in rows})
        self.assertFalse(proof['confirmed_not_written'])
        with self.assertRaisesRegex(ValueError, 'terminal'):
            dispatch.resume(self.c['id'], reason='existing source must not replay')
        ec.cmd_reconcile_operation(self.c['id'], 'm-002', url='http://invalid', token=None,
            check=lambda rows, *a: {row['source_obs_id']: None for row in rows})
        original = receipts.read(directory / 'latest.json')
        for mutation in ['attempt', 'kind', 'source']:
            record = json.loads(json.dumps(original))
            if mutation == 'attempt': record['reconciliation']['attempt'] = 'another-operation'
            if mutation == 'kind': record['kind'] = 'submit'
            if mutation == 'source': record['source_keys'][0]['source_obs_id'] = 'changed-source'
            receipts.write(directory / 'latest.json', record)
            with self.assertRaisesRegex(ValueError, 'terminal'):
                dispatch.resume(self.c['id'], reason='mismatched proof must not authorize replay')
        receipts.write(directory / 'latest.json', original)
        with receipts.attempt(directory, kind='dryrun', identity=original['identity']):
            with self.assertRaises(receipts.Busy):
                dispatch.resume(self.c['id'], reason='live original cannot be resumed')


if __name__ == '__main__':
    unittest.main()
