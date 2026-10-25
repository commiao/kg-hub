"""Real local leases and synthetic frozen history; no graph or provider calls."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from tools import agent_extract as ax, extract_campaign as ec, campaign_migration as m, campaign_supervision as s
from utils import process_receipts as r


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.dict(os.environ, {'KG_HUB_AGENT_EXTRACT_DIR': str(self.root),
            'KG_HUB_CLAIMS_DIR': str(self.root / 'claims')}))
        self.stack.enter_context(mock.patch.object(ec, 'tracking_guard', return_value={'tracked': False}))
        self.c = ec.new_campaign('migration-test', ['m', 'n'], executor='codex', quota=2000, batch=50, after_id=0)
        self.d = ec.campaign_dir(self.c['id'])
        self.d.mkdir(parents=True)
        (self.d / 'batches').mkdir()
        (self.d / 'results').mkdir()
        (self.d / 'prompt.md').write_text(ax.PROMPT_PATH.read_text())
        self.proof = {'workers': {}}
        handoff = {'workers': {}}
        for index, model in enumerate(self.c['models'], 1):
            bid = model + '-001'
            self.c['batches'].append({'id': bid, 'model': model, 'chain': 'c0', 'ids': [index],
                                      'status': 'submitted', 'cost': {}})
            (self.d / 'batches' / (bid + '.json')).write_text(json.dumps({'items': [{'id': index}], 'model': model}))
            (self.d / 'results' / (bid + '.json')).write_text(json.dumps({'items': [{'id': index}]}))
            self.c['cursor'] = index
            with (self.d / 'log.jsonl').open('a') as log:
                log.write(json.dumps({'id': index, 'batch': bid, 'model': model, 'status': 'ok', 'http': 200,
                    'run_id': self.c['models'][model]['run_id'], 'executor': 'codex'}) + '\n')
            root = ec.supervisor_dir(self.d, model)
            native = root / 'coordinator-recovery.native'
            with r.attempt(native, kind='native_cli') as update:
                update(exit_code=0)
            events = root / 'coordinator-recovery.events.jsonl'
            sid = 'original-' + model
            events.write_text(json.dumps({'type': 'thread.started', 'thread_id': sid}) + '\n' +
                              json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
                                  'text': json.dumps({'recovery_kind': 'migration_drained'})}}) + '\n' +
                              json.dumps({'type': 'turn.completed'}) + '\n')
            r.write(root / 'worker.json', {'session': sid, 'events': str(events)})
            with r.attempt(root, kind='supervisor', identity={'campaign': self.c['id'], 'model': model}) as update:
                update(exit_code=2, phase='coordinator_required', reason='historical_recovery')
            self.proof['workers'][model] = {'session': sid, 'runtime_receipt_dir': str(native), 'events': str(events)}
            handoff['workers'][model] = {'native_session_id': sid, 'runtime_receipt_dir': str(native), 'stdout': str(events)}
        ec.save(self.d, self.c)
        r.write(self.d / 'handoff-status.json', handoff)
        self.out = self.root / 'preview.json'

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def prepare(self):
        m.drain(self.c['id'], 'user authorized four slots per model')
        return m.preview(self.c['id'], 4, self.proof, self.out)

    def pending(self):
        c = ec.load(self.d)
        b = {'id': 'm-002', 'model': 'm', 'chain': 'c0', 'ids': [3], 'status': 'claimed', 'cost': {}}
        c['batches'].append(b)
        c['cursor'] = 3
        (self.d / 'batches/m-002.json').write_text(json.dumps({'items': [{'id': 3}], 'model': 'm'}))
        ec.save(self.d, c)
        return b

    def test_freeze_blocks_claim_without_fetch_and_does_not_change_quota_or_history(self):
        before = ec.load(self.d)
        m.drain(self.c['id'], 'handover')
        fetch = mock.Mock(side_effect=AssertionError('must not scan'))
        out = ec.cmd_claim(self.c['id'], 'm', url='http://invalid', token=None, fetch=fetch)
        self.assertTrue(out['paused'])
        self.assertFalse(out['done'])
        fetch.assert_not_called()
        c = ec.load(self.d)
        self.assertEqual((c['quota'], c['models'], c['batches'], c['cursor']),
                         (before['quota'], before['models'], before['batches'], before['cursor']))

    def test_migration_requires_freeze_and_clean_terminal_for_every_original_session(self):
        with self.assertRaisesRegex(ValueError, 'freeze'):
            m.preview(self.c['id'], 4, self.proof, self.out)
        m.drain(self.c['id'], 'handover')
        native = Path(self.proof['workers']['m']['runtime_receipt_dir'])
        with r.attempt(native, kind='native_cli'):
            with self.assertRaisesRegex(ValueError, 'live'):
                m.preview(self.c['id'], 4, self.proof, self.out)
        record = r.read(native / 'latest.json')
        record.update(state='running')
        r.write(native / 'latest.json', record)
        with self.assertRaisesRegex(ValueError, 'terminal'):
            m.preview(self.c['id'], 4, self.proof, self.out)

    def test_apply_preserves_frozen_history_log_identity_and_idempotent_reapply(self):
        batch_bytes = {p.name: p.read_bytes() for p in (self.d / 'batches').iterdir()}
        logs = (self.d / 'log.jsonl').read_bytes()
        old = ec.load(self.d)
        self.prepare()
        out = m.apply(self.c['id'], self.out)
        c = ec.load(self.d)
        self.assertTrue(out['applied'])
        self.assertEqual(c['parallel'], 4)
        self.assertTrue(m.valid_migration(c, self.d))
        self.assertEqual(c['batches'], old['batches'])
        self.assertEqual(m.core(c), m.core(old))
        self.assertEqual((self.d / 'log.jsonl').read_bytes(), logs)
        self.assertEqual({p.name: p.read_bytes() for p in (self.d / 'batches').iterdir()}, batch_bytes)
        for model in c['models']:
            self.assertEqual(s.completed(c, self.d, model), 1)
            self.assertEqual(s.completed(c, self.d, model, 1), 1)
            self.assertEqual(s.completed(c, self.d, model, 2), 0)
            self.assertEqual(r.read(ec.supervisor_dir(self.d, model, 1) / 'worker.json')['session'], 'original-' + model)
        self.assertTrue(m.apply(self.c['id'], self.out)['already_applied'])

    def test_unstarted_original_batch_stays_with_same_session_in_slot_one(self):
        self.pending()
        before = (self.d / 'batches/m-002.json').read_bytes()
        self.prepare()
        m.apply(self.c['id'], self.out)
        c = ec.load(self.d)
        out = ec.cmd_claim(self.c['id'], 'm', slot=1, url='http://invalid', token=None)
        self.assertEqual(out['batch'], 'm-002')
        self.assertTrue(out['resumed'])
        with mock.patch.dict(os.environ, {'KG_HUB_CAMPAIGN_ID': self.c['id'],
                'KG_HUB_CAMPAIGN_MODEL': 'm', 'KG_HUB_CAMPAIGN_SLOT': '1'}):
            ec.assert_worker_batch(c, ec.find_batch(c, 'm-002'))
        with mock.patch.dict(os.environ, {'KG_HUB_CAMPAIGN_ID': self.c['id'],
                'KG_HUB_CAMPAIGN_MODEL': 'm', 'KG_HUB_CAMPAIGN_SLOT': '2'}):
            with self.assertRaisesRegex(SystemExit, 'different worker'):
                ec.assert_worker_batch(c, ec.find_batch(c, 'm-002'))
        self.assertEqual((self.d / 'batches/m-002.json').read_bytes(), before)

    def test_migrated_eight_slots_overlap_resume_originals_and_share_remaining_quota(self):
        from test_campaign_parallel import gather
        c = ec.load(self.d)
        c['quota'] = 201
        ec.save(self.d, c)
        self.pending()
        self.prepare()
        m.apply(self.c['id'], self.out)
        barrier = threading.Barrier(8)
        calls = []
        calls_lock = threading.Lock()

        def spawn(args, **kwargs):
            env = kwargs['env']
            model, slot = env['KG_HUB_CAMPAIGN_MODEL'], int(env['KG_HUB_CAMPAIGN_SLOT'])
            sid = 'original-' + model if slot == 1 else f'new-{model}-{slot}'
            if slot == 1:
                self.assertEqual(args[args.index('resume') + 1], sid)
            else:
                self.assertNotIn('resume', args)
            native = Path(args[args.index('--receipt-dir') + 1])
            with r.attempt(native, kind='native_cli') as update:
                with calls_lock:
                    calls.append((model, slot, sid))
                barrier.wait(timeout=10)
                self.assertEqual(ec.cmd_status(self.c['id'])['active_workers'], 8)
                barrier.wait(timeout=10)
                while True:
                    out = ec.cmd_claim(self.c['id'], model, slot=slot, url='http://invalid', token=None,
                                       check=lambda rows: {})
                    if out['done']:
                        break
                    with ec.locked(self.d):
                        state = ec.load(self.d)
                        b = ec.find_batch(state, out['batch'])
                        b['status'] = 'submitted'
                        with (self.d / 'log.jsonl').open('a') as log:
                            for oid in b['ids']:
                                log.write(json.dumps(dict(id=oid, batch=b['id'], model=model,
                                    executor='codex', run_id=state['models'][model]['run_id'], status='ok')) + '\n')
                        ec.save(self.d, state)
                for event in [dict(type='thread.started', thread_id=sid), dict(type='turn.completed')]:
                    kwargs['stdout'].write((json.dumps(event) + '\n').encode())
                kwargs['stdout'].flush()
                update(exit_code=0)
            return mock.Mock(poll=lambda: 0)

        with mock.patch.object(ax, 'gather_batch', side_effect=gather), \
                mock.patch.object(ax, 'load_config', return_value={}), \
                mock.patch.object(ec, 'workspace_dir', return_value=self.root), \
                ThreadPoolExecutor(max_workers=2) as pools:
            futures = [pools.submit(s.run_pool, self.c['id'], model, poll=.01, stagger=0,
                                    spawn=spawn, preflight=lambda *a: {'ok': True}) for model in self.c['models']]
            outcomes = [f.result(timeout=20) for f in futures]
        self.assertTrue(all(o['state'] == 'complete' and o['success'] == 201 for o in outcomes), outcomes)
        c = ec.load(self.d)
        ids = [oid for b in c['batches'] for oid in b['ids']]
        self.assertEqual((len(ids), len(set(ids)), len(calls)), (402, 402, 8))
        self.assertTrue(m.valid_migration(c, self.d))
        self.assertEqual(len({sid for _, _, sid in calls}), 8)

    def test_original_lease_and_nas_refusal_cannot_be_bypassed(self):
        self.prepare()
        root = ec.supervisor_dir(self.d, 'm')
        with r.attempt(root, kind='supervisor', identity={'campaign': self.c['id'], 'model': 'm'}):
            with self.assertRaisesRegex(ValueError, 'live or uncertain'):
                m.apply(self.c['id'], self.out)
        self.out.unlink()
        events = Path(self.proof['workers']['m']['events'])
        with events.open('a') as stream:
            stream.write(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
                'text': json.dumps({'recovery_kind': 'nas_refused'})}}) + '\n')
        with self.assertRaisesRegex(ValueError, 'terminal'):
            m.preview(self.c['id'], 4, self.proof, self.out)

    def test_changed_state_or_log_after_preview_cannot_be_applied(self):
        self.prepare()
        with (self.d / 'log.jsonl').open('a') as log:
            log.write('\n')
        with self.assertRaisesRegex(ValueError, 'changed after preview'):
            m.apply(self.c['id'], self.out)
        self.assertEqual(ec.parallelism(ec.load(self.d)), 1)

    def test_wrong_or_duplicated_session_and_foreign_proof_paths_are_rejected(self):
        m.drain(self.c['id'], 'handover')
        original = dict(self.proof['workers']['m'])
        for key, value in [('session', 'original-n'), ('events', '/private/tmp/foreign-events')]:
            self.proof['workers']['m'][key] = value
            with self.assertRaises(ValueError):
                m.preview(self.c['id'], 4, self.proof, self.out)
            self.proof['workers']['m'] = dict(original)

    def test_partial_submission_and_unresolved_nas_stop_block_migration(self):
        self.pending()
        m.drain(self.c['id'], 'handover')
        with (self.d / 'log.jsonl').open('a') as log:
            log.write(json.dumps({'batch': 'm-002', 'id': 3, 'status': 'ok'}) + '\n')
        with self.assertRaisesRegex(ValueError, 'partial submit'):
            m.preview(self.c['id'], 4, self.proof, self.out)

    def test_apply_can_resume_after_failure_before_atomic_manifest_activation(self):
        self.prepare()
        real = r.write
        failed = False
        def write(path, data):
            nonlocal failed
            if path == ec.supervisor_dir(self.d, 'n', 1) / 'worker.json' and not failed:
                failed = True
                raise OSError('synthetic interrupted staging')
            return real(path, data)
        with mock.patch.object(r, 'write', side_effect=write), self.assertRaises(OSError):
            m.apply(self.c['id'], self.out)
        self.assertEqual(ec.parallelism(ec.load(self.d)), 1)
        self.assertEqual(ec.load(self.d)['dispatch']['state'], 'draining')
        self.assertTrue(m.apply(self.c['id'], self.out)['applied'])

    def test_rollback_only_before_any_slot_execution_or_new_claim(self):
        self.prepare()
        m.apply(self.c['id'], self.out)
        out = m.rollback(self.c['id'])
        self.assertTrue(out['rolled_back'])
        self.assertEqual(ec.parallelism(ec.load(self.d)), 1)
        self.assertEqual(ec.load(self.d)['dispatch']['state'], 'draining')
        m.apply(self.c['id'], self.out)
        root = ec.supervisor_dir(self.d, 'm', 2) / 'native'
        with r.attempt(root, kind='native_cli'):
            pass
        with self.assertRaisesRegex(ValueError, 'execution history'):
            m.rollback(self.c['id'])

    def test_formal_identity_tampering_and_foreign_destination_worker_are_rejected(self):
        self.prepare()
        r.write(ec.supervisor_dir(self.d, 'm', 2) / 'worker.json', {'session': 'foreign'})
        with self.assertRaisesRegex(ValueError, 'worker identity'):
            m.apply(self.c['id'], self.out)


if __name__ == '__main__':
    unittest.main()
