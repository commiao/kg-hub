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

    def run_rework(self):
        return rework.run(self.c['id'], 'm', 1, batches=[self.bid], assignment=self.assignment,
                          reason='original single owner repair', spawn=self.spawn,
                          preflight=lambda *a: {'ok': True})

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


if __name__ == '__main__':
    unittest.main()
