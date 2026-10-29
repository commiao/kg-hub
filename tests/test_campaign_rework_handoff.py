"""Accepted rework can return to its exact pending owner without erasing stops."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_campaign_recovery_manager as fixtures
from tools import campaign_rework_handoff as handoff, campaign_recovery_manager as manager
from tools import campaign_supervision as supervision, extract_campaign as ec
from utils import process_receipts as receipts


class ReworkHandoffTests(unittest.TestCase):
    setUp, tearDown, claim, finish, prepare, write_turn, row = (
        fixtures.RecoveryManagerTests.setUp, fixtures.RecoveryManagerTests.tearDown,
        fixtures.RecoveryManagerTests.claim, fixtures.RecoveryManagerTests.finish,
        fixtures.RecoveryManagerTests.prepare, fixtures.RecoveryManagerTests.write_turn,
        fixtures.RecoveryManagerTests.row)
    def setup_handoff(self):
        self.prepare()
        self.finish(self.bid)
        self.original = self.bid
        self.pending = self.claim()['batch']
        self.event = self.owner / 'quality-reworks/job/events.jsonl'
        self.event.parent.mkdir(parents=True)
        self.write_turn()
        worker = receipts.read(self.owner / 'worker.json')
        worker.update(mode='quality_rework', batches=[self.original])
        receipts.write(self.owner / 'worker.json', worker)
        self.audit = self.d / 'audit/accepted'; self.audit.mkdir(parents=True)
        ids = ec.find_batch(ec.load(self.d), self.original)['ids']
        scopes = {}
        for i in ids:
            p = self.audit / f'{i}.json'; receipts.write(p, {'source': i, 'accepted': True})
            scopes[str(i)] = str(p)
        self.acceptance = self.audit / 'acceptance.json'
        receipts.write(self.acceptance, {'batch': self.original, 'current_quality_rework_scope_accepted': True,
                                      'sources': len(ids), 'source_scopes': scopes})
        row = self.row()
        self.block = self.d / 'recovery/events/old-block/result.json'
        receipts.write(self.block, {'model': 'm', 'slot': 1, 'state': 'blocked',
            'reason': 'no_formal_progress', 'conditions_sha256': row['conditions_sha256']})

    def approve(self):
        return handoff.approve(self.c['id'], 'm', 1, evidence=[self.acceptance], reason='independently accepted')

    def test_old_block_requires_explicit_acceptance_and_preserves_originals(self):
        self.setup_handoff()
        self.assertEqual(self.row()['action'], 'coordinator_rework_handoff_required')
        before = {p: p.read_bytes() for p in [self.block, self.owner / 'worker.json', self.d / 'campaign.json']}
        value = self.approve()
        self.assertEqual(self.approve(), value)
        row = self.row()
        self.assertEqual(row['action'], 'supervise')
        self.assertEqual(row['accepted_rework_handoff'], value['id'])
        for p, raw in before.items(): self.assertEqual(p.read_bytes(), raw)
        calls = []
        manager.tick(self.c['id'], spawn=lambda args, **kw: (calls.append(args) or mock.Mock(pid=1)))
        self.assertEqual(len(calls), 1)
        with mock.patch.object(supervision, 'run', return_value={'state': 'drained'}) as run:
            manager.step(self.c['id'], calls[0][-1])
            manager.step(self.c['id'], calls[0][-1])
            run.assert_called_once_with(self.c['id'], 'm', slot=1)

    def test_missing_partial_and_wrong_batch_acceptance_rejected(self):
        for field, value in [('current_quality_rework_scope_accepted', False), ('sources', 49),
                             ('source_scopes', {}), ('batch', 'foreign')]:
            with self.subTest(field=field):
                self.setup_handoff()
                doc = receipts.read(self.acceptance); doc[field] = value; receipts.write(self.acceptance, doc)
                with self.assertRaisesRegex(ValueError, 'explicit full'): self.approve()
                # Each scenario uses a separate synthetic state directory.
                self.tearDown(); self.setUp()

    def test_changed_evidence_or_owner_invalidates_receipt(self):
        self.setup_handoff(); self.approve()
        scope = next(self.audit.glob('[0-9]*.json')); scope.write_text('{}')
        self.assertEqual(self.row()['action'], 'coordinator_rework_handoff_required')

    def test_active_original_operation_and_unknown_write_rejected(self):
        self.setup_handoff()
        op = self.d / 'operations' / self.pending
        with receipts.attempt(op, kind='dryrun'):
            with self.assertRaises(receipts.Busy): self.approve()
        with receipts.attempt(op, kind='submit') as update: update(may_have_written=True)
        with self.assertRaisesRegex(ValueError, 'safely idle'): self.approve()

    def test_live_native_and_real_refusal_rejected(self):
        self.setup_handoff()
        with receipts.attempt(self.owner / 'native', kind='native_cli'):
            with self.assertRaises(receipts.Busy): self.approve()
        self.write_turn('approval_rejected')
        worker = receipts.read(self.owner / 'worker.json'); worker.update(mode='quality_rework', batches=[self.original])
        receipts.write(self.owner / 'worker.json', worker)
        with self.assertRaisesRegex(ValueError, 'clean original'): self.approve()

    def test_worker_cannot_authorize_and_other_slot_cannot_adopt(self):
        self.setup_handoff()
        with mock.patch.dict(os.environ, {'KG_HUB_CAMPAIGN_SLOT': '1'}):
            with self.assertRaisesRegex(ValueError, 'coordinator'): self.approve()
        with self.assertRaisesRegex(ValueError, 'clean original'):
            handoff.approve(self.c['id'], 'm', 2, evidence=[self.acceptance], reason='wrong owner')

    def test_duplicate_acceptance_cannot_clear_a_new_no_progress_stop(self):
        self.setup_handoff(); first = self.approve(); row = self.row()
        receipts.write(self.d / 'recovery/events/new-block/result.json', {
            'model': 'm', 'slot': 1, 'state': 'blocked', 'reason': 'no_formal_progress',
            'conditions_sha256': row['conditions_sha256']})
        self.assertEqual(self.approve(), first)
        self.assertEqual(self.row()['action'], 'unchanged_blocked')
        with self.assertRaisesRegex(ValueError, 'differs'):
            handoff.approve(self.c['id'], 'm', 1, evidence=[self.acceptance], reason='reset budget')

    def test_new_native_or_generation_does_not_reuse_old_handoff(self):
        self.setup_handoff(); self.approve()
        with receipts.attempt(self.owner / 'native', kind='native_cli') as update: update(exit_code=0)
        self.assertEqual(self.row()['action'], 'coordinator_rework_handoff_required')
        with self.assertRaisesRegex(ValueError, 'differs'): self.approve()

    def test_live_peer_is_not_a_global_handoff_barrier(self):
        self.setup_handoff()
        peer = ec.supervisor_dir(self.d, 'm', 2)
        with receipts.attempt(peer / 'native', kind='native_cli'):
            self.approve()
            self.assertEqual(self.row()['action'], 'supervise')

    def test_actual_supervisor_returns_to_production_in_original_session(self):
        self.setup_handoff(); self.approve(); calls = []
        def spawn(args, **kwargs):
            calls.append((args, kwargs['env']))
            self.assertIn('resume', args)
            self.assertEqual(args[args.index('resume') + 1], 'original-session')
            self.assertNotIn('KG_HUB_REWORK_BATCHES', kwargs['env'])
            directory = Path(args[args.index('--receipt-dir') + 1])
            with receipts.attempt(directory, kind='native_cli') as update:
                for event in [{'type': 'thread.started', 'thread_id': 'original-session'},
                              {'type': 'turn.completed'}]:
                    kwargs['stdout'].write((json.dumps(event) + '\n').encode())
                kwargs['stdout'].flush()
                self.finish(self.pending)
                c = ec.load(self.d); c['dispatch']['state'] = 'draining'; ec.save(self.d, c)
                update(exit_code=0)
            return mock.Mock(poll=lambda: 0, wait=lambda: 0)
        result = supervision.run(self.c['id'], 'm', slot=1, poll=.01, sleep=lambda _: None,
                                 spawn=spawn, preflight=lambda *a: {'ok': True})
        self.assertEqual(result['state'], 'drained')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('mode', receipts.read(self.owner / 'worker.json'))
        self.assertTrue(self.block.exists())

if __name__ == '__main__':
    unittest.main()
