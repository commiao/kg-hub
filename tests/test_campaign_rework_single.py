"""Legacy single-worker receipts must support drained same-session rework."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_campaign_parallel as parallel
from tools import campaign_rework as rework, extract_campaign as ec
from utils import process_receipts as receipts


class SingleReworkTests(unittest.TestCase):
    tearDown = parallel.ParallelCampaignTests.tearDown
    claim, finish = parallel.ParallelCampaignTests.claim, parallel.ParallelCampaignTests.finish

    def setUp(self):
        parallel.ParallelCampaignTests.setUp(self)
        self.c['parallel'] = 1
        self.c['batch'] = 2
        ec.save(self.d, self.c)
        self.bid = self.claim('m', 1)['batch']
        self.finish(self.bid)
        self.c = ec.load(self.d)
        self.c['dispatch'] = {'state': 'draining'}
        ec.save(self.d, self.c)
        self.worker = ec.supervisor_dir(self.d, 'm')
        self.sid = 'original-single-session'
        self.identity = {'campaign': self.c['id'], 'model': 'm'}
        with receipts.attempt(self.worker, kind='supervisor', identity=self.identity) as update:
            update(session=self.sid, exit_code=2, phase='coordinator_required')
        self.events = self.worker / 'original.events.jsonl'
        self.events.write_text(json.dumps({'type': 'thread.started', 'thread_id': self.sid}) + '\n' +
                               json.dumps({'type': 'turn.completed'}) + '\n')
        receipts.write(self.worker / 'worker.json', {'session': self.sid, 'events': str(self.events)})
        with receipts.attempt(self.worker / 'native', kind='native_cli') as update:
            update(exit_code=0)
        self.assignment = self.root / 'assignment.md'
        self.assignment.write_text('Review the original submitted sources only.')
        self.spawned = []

    def spawn(self, args, **kwargs):
        self.assertEqual(args[args.index('resume') + 1], self.sid)
        self.assertEqual(kwargs['env']['KG_HUB_CAMPAIGN_SLOT'], '1')
        with mock.patch.dict(os.environ, kwargs['env']):
            c = ec.load(self.d)
            self.assertIsNone(ec.worker_slot(c, 'm'))
            ec.assert_worker_batch(c, ec.find_batch(c, self.bid))
            outside = dict(ec.find_batch(c, self.bid), id='outside')
            with self.assertRaisesRegex(SystemExit, 'outside explicitly approved'):
                ec.assert_worker_batch(c, outside)
        self.spawned.append(args)
        with receipts.attempt(self.worker / 'native', kind='native_cli') as update:
            kwargs['stdout'].write(self.events.read_bytes())
            kwargs['stdout'].flush()
            update(exit_code=0)
        return mock.Mock(wait=lambda: 0)

    def run_rework(self, **kwargs):
        return rework.run(self.c['id'], 'm', 1, batches=[self.bid], assignment=self.assignment,
                          reason='original single owner repair', spawn=self.spawn,
                          preflight=lambda *a: {'ok': True}, **kwargs)

    def test_single_identity_session_scope_and_history_survive_rework(self):
        original = {p: p.read_bytes() for p in (self.d / 'campaign.json', self.d / 'log.jsonl', self.events)}
        result = self.run_rework()
        self.assertEqual(result['state'], 'rework_turn_completed')
        self.assertIsNone(result['slot'])
        self.assertEqual(receipts.read(self.worker / 'latest.json')['identity'], self.identity)
        self.assertEqual(len(self.spawned), 1)
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)
        # The same public entry can continue another bounded rework turn.
        self.run_rework()
        self.assertEqual(len(self.spawned), 2)

    def test_wrong_event_session_is_rejected(self):
        self.events.write_text(self.events.read_text().replace(self.sid, 'foreign-session'))
        with self.assertRaisesRegex(ValueError, 'clean rework events'):
            self.run_rework()
        self.assertFalse(self.spawned)

    def test_duplicate_session_in_another_model_is_rejected(self):
        receipts.write(ec.supervisor_dir(self.d, 'n') / 'worker.json', {'session': self.sid})
        with self.assertRaisesRegex(ValueError, 'clean idle'):
            self.run_rework()
        self.assertFalse(self.spawned)

    def test_failed_native_and_live_owner_are_rejected(self):
        with receipts.attempt(self.worker / 'native', kind='native_cli'):
            with self.assertRaises(receipts.Busy):
                self.run_rework()
        with receipts.attempt(self.worker / 'native', kind='native_cli') as update:
            update(exit_code=1)
        with self.assertRaisesRegex(ValueError, 'clean idle'):
            self.run_rework()
        self.assertFalse(self.spawned)


class StartupRetryTests(SingleReworkTests):
    def fail_startup(self):
        def failed(args, **kwargs):
            with receipts.attempt(self.worker / 'native', kind='native_cli') as update:
                kwargs['stderr'].write(b'Error: failed to initialize in-process app-server client: Operation not permitted (os error 1)\n')
                kwargs['stderr'].flush()
                update(exit_code=1, child_pid=12345)
            return mock.Mock(wait=lambda: 1)
        with mock.patch.object(self, 'spawn', failed):
            out = self.run_rework()
        self.assertEqual(out['state'], 'blocked')
        return Path(out['evidence_dir'])

    def test_explicit_retry_preserves_failure_and_uses_original_clean_anchor(self):
        job = self.fail_startup()
        old = {p: p.read_bytes() for p in job.iterdir() if p.is_file()}
        failure = (self.worker / 'native/latest.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'clean idle'):
            self.run_rework()
        out = self.run_rework(retry_startup=True)
        self.assertEqual(out['state'], 'rework_turn_completed')
        new = Path(out['evidence_dir'])
        self.assertEqual((new / 'previous-native.json').read_bytes(), failure)
        self.assertTrue((new / 'startup-retry.json').is_file())
        from tools.campaign_ownership_recovery import _ref
        for ref in receipts.read(new / 'startup-retry.json').values():
            self.assertEqual(_ref(ref['path']), ref)
        for path, data in old.items():
            self.assertEqual(path.read_bytes(), data)

    def test_any_native_event_or_other_stderr_refusal_is_not_a_startup_retry(self):
        job = self.fail_startup()
        events, stderr = job / 'events.jsonl', job / 'stderr.log'
        original = stderr.read_bytes()
        mutations = [(events, b'{"type":"thread.started","thread_id":"original-single-session"}\n'),
                     (events, b'{"type":"error","message":"capacity"}\n'),
                     (stderr, b'This action was rejected due to unacceptable risk\n' + original),
                     (stderr, b'Error: provider denied request\n')]
        for path, data in mutations:
            old = path.read_bytes();path.write_bytes(data)
            with self.subTest(data=data):
                with self.assertRaises(ValueError):self.run_rework(retry_startup=True)
            path.write_bytes(old)
        self.assertFalse(self.spawned)

    def test_changed_scope_assignment_or_failed_anchor_is_rejected(self):
        job = self.fail_startup()
        for filename, key, value in [('scope.json', 'session', 'foreign'),
                                     ('scope.json', 'batches', ['foreign']),
                                     ('exit-status.json', 'exit_code', 0),
                                     ('previous-native.json', 'exit_code', 1)]:
            path=job / filename;old=path.read_bytes();row=json.loads(old);row[key]=value;receipts.write(path,row)
            with self.subTest(filename=filename, key=key):
                with self.assertRaises(ValueError):self.run_rework(retry_startup=True)
            path.write_bytes(old)
        self.assignment.write_text('different scope')
        with self.assertRaises(ValueError):self.run_rework(retry_startup=True)
        self.assertFalse(self.spawned)

    def test_explicit_retry_cannot_loop_or_adopt_another_invocation(self):
        self.fail_startup()
        with self.assertRaisesRegex(ValueError, 'cannot adopt'):
            self.run_rework(retry_startup=True, adopt_events=self.events, adopt_exit=self.events)
        # Another failed startup lacks a preceding clean anchor; no retry loop.
        row=receipts.read(self.worker / 'worker.json');job=Path(row['events']).parent
        previous=receipts.read(job / 'previous-native.json');previous['exit_code']=1
        receipts.write(job / 'previous-native.json',previous)
        with self.assertRaises(ValueError):self.run_rework(retry_startup=True)
        self.assertFalse(self.spawned)


if __name__ == '__main__':
    unittest.main()
