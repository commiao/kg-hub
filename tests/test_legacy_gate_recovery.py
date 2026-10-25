"""Real files/leases, synthetic native transcripts and fake HTTP; no model calls."""
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import campaign_recovery as recovery, campaign_supervision as supervision
from tools import extract_campaign as ec
from utils import process_receipts as receipts
import test_extract_campaign as fixtures


class LegacyGateRecoveryTest(unittest.TestCase):
    setUp, tearDown, claim = fixtures.CampaignTest.setUp, fixtures.CampaignTest.tearDown, fixtures.CampaignTest.claim

    def prepare(self, dry_run=False):
        campaign = ec.load(ec.campaign_dir('t1'))
        campaign['quota'] = 10000
        campaign.pop('start_contract', None)
        ec.save(ec.campaign_dir('t1'), campaign)
        bid = self.claim('DeepSeek-Flash')['batch']
        self.d = ec.campaign_dir('t1')
        c = ec.load(self.d); b = ec.find_batch(c, bid); b['status'] = 'dry_run'; c['executor'] = 'codex'; ec.save(self.d, c)
        root = Path(self.tmp.name)
        self.exit = root / 'unique-exit-status.json'
        self.events = root / 'unique-stdout.jsonl'
        self.console = root / 'gate.log'
        kind = 'dryrun' if dry_run else 'submit'
        self.console.write_text('排队等锁超过 3600 秒，未放行 ' + kind + '\n')
        entry = str(Path(os.environ.get('KG_HUB_CODE', '~/.local/share/kg-hub/current')).expanduser() / 'tools/extract')
        cmd = f'{entry} campaign submit --id t1 --batch {bid}' + (' --dry-run' if dry_run else '')
        cmd += f' > {self.console} 2>&1'
        self.rows = [{'type': 'thread.started', 'thread_id': 'same-session'},
                     {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'help', 'command': entry + ' campaign submit --help', 'exit_code': 0}},
                     {'type': 'item.started', 'item': {'type': 'command_execution', 'id': 'op1', 'command': cmd}},
                     {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'op1', 'command': cmd, 'exit_code': 76}},
                     {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"recovery_kind":"nas_refused"}'}},
                     {'type': 'turn.completed'}]
        self.write_events()
        self.exit.write_text(json.dumps({'exit_code': 0, 'ended_at': datetime.now(timezone.utc).isoformat()}))
        return bid

    def write_events(self):
        self.events.write_text(''.join(json.dumps(r) + '\n' for r in self.rows))

    def reconcile(self, bid, check=None, **overrides):
        args = dict(url='http://fake', token=None, legacy_exit=self.exit, legacy_events=self.events,
                    session='same-session', check=check or (lambda rows, *a: {r['source_obs_id']: None for r in rows}))
        args.update(overrides)
        return ec.cmd_reconcile_operation('t1', bid, **args)

    def test_verified_legacy_timeout_can_resume_same_session_and_retains_original76(self):
        bid = self.prepare(False)
        original = self.events.read_bytes()
        self.assertTrue(self.reconcile(bid)['confirmed_not_written'])
        op = ec.cmd_operation_status('t1', bid)
        self.assertEqual(op['action'], 'review_and_preview_before_submit')
        self.assertEqual(op['operation']['exit_code'], 76)
        self.assertEqual(self.events.read_bytes(), original)
        c = ec.load(self.d)
        self.assertTrue(recovery.stops_reconciled(c, self.d, 'DeepSeek-Flash', self.events, supervision.events(self.events)))
        calls = []
        def spawn(args, **kwargs):
            calls.append(args)
            self.assertEqual(args[args.index('resume') + 1], 'same-session')
            raise InterruptedError('verified resume reached; no real model')
        with mock.patch.object(ec, 'tracking_guard'), mock.patch.object(ec.ax, 'api_token', return_value=None):
            with self.assertRaises(InterruptedError):
                supervision.run('t1', 'DeepSeek-Flash', session='same-session', adopt_exit=self.exit,
                    adopt_events=self.events, poll=.01, spawn=spawn, preflight=lambda *a: {'ok': True})
        self.assertEqual(len(calls), 1)

    def test_read_only_preview_timeout_can_be_adopted_with_the_same_checks(self):
        bid = self.prepare(True)
        self.assertTrue(self.reconcile(bid)['confirmed_not_written'])
        self.assertEqual(ec.cmd_operation_status('t1', bid)['action'], 'review_and_preview_before_submit')

    def test_incomplete_or_existing_service_keys_never_create_an_operation_approval(self):
        for kind in ['incomplete', 'present']:
            with self.subTest(kind=kind):
                bid = self.prepare()
                check = (lambda *a: {}) if kind == 'incomplete' else lambda rows, *a: {r['source_obs_id']: 'ok' for r in rows}
                if kind == 'incomplete':
                    with self.assertRaises(ValueError): self.reconcile(bid, check)
                else:
                    self.assertFalse(self.reconcile(bid, check)['confirmed_not_written'])
                self.assertFalse((self.d / 'operations' / bid / 'latest.json').exists())

    def test_live_or_existing_receipt_cannot_be_overwritten(self):
        bid = self.prepare(); directory = self.d / 'operations' / bid
        check = mock.Mock(side_effect=AssertionError('must not query while live'))
        with receipts.attempt(directory, kind='submit'):
            with self.assertRaises(receipts.Busy): self.reconcile(bid, check)
        original = (directory / 'latest.json').read_bytes()
        with self.assertRaises(ValueError): self.reconcile(bid, check)
        self.assertEqual((directory / 'latest.json').read_bytes(), original)

    def test_wrong_session_inflight_wrong_scope_and_non_gate_fail_before_service_query(self):
        for bad in ['session', 'pending', 'scope', 'non_gate', 'console', 'compound', 'pair', 'supervisor', 'owner']:
            with self.subTest(bad=bad):
                bid = self.prepare(); kw = {}
                if bad == 'session': kw['session'] = 'different-session'
                elif bad == 'pending': self.rows.pop(3)
                elif bad == 'scope':
                    for row in self.rows:
                        if 'item' in row and row['item'].get('type') == 'command_execution':
                            row['item']['command'] = row['item']['command'].replace('--id t1', '--id other')
                elif bad == 'non_gate': self.rows[3]['item']['exit_code'] = 1
                elif bad == 'console': self.console.write_text('HTTP write failed with unknown terminal\n')
                elif bad == 'compound':
                    for row in self.rows:
                        if 'item' in row and row['item'].get('type') == 'command_execution': row['item']['command'] += '; echo done'
                elif bad == 'pair': kw['legacy_events'] = self.exit
                elif bad == 'supervisor': receipts.write(self.d / 'supervisors/DeepSeek-Flash/latest.json', {'session': 'other'})
                else: receipts.write(self.d / 'supervisors/DeepSeek-Flash/latest.json', {'identity': {'campaign': 'other', 'model': 'DeepSeek-Flash'}})
                self.write_events()
                check = mock.Mock(side_effect=AssertionError('invalid proof must not query service'))
                with self.assertRaises((ValueError, KeyError)): self.reconcile(bid, check, **kw)
                check.assert_not_called()

    def test_proof_or_frozen_batch_mutation_invalidates_supervisor_adoption(self):
        for part in ['events', 'console', 'exit', 'batch']:
            with self.subTest(part=part):
                bid = self.prepare(); self.reconcile(bid)
                if part == 'batch':
                    p = self.d / 'batches' / (bid + '.json'); b = json.loads(p.read_text()); b['items'][0]['episode_body'] += ' changed'; p.write_text(json.dumps(b))
                else:
                    p = getattr(self, part); p.write_text(p.read_text() + '\n')
                self.assertFalse(recovery.stops_reconciled(ec.load(self.d), self.d, 'DeepSeek-Flash', self.events, supervision.events(self.events)))

    def test_source_check_cannot_race_and_replace_original_evidence(self):
        bid = self.prepare()
        def changed(rows, *args):
            self.console.write_text('changed during verification\n')
            return {r['source_obs_id']: None for r in rows}
        with self.assertRaises(ValueError): self.reconcile(bid, changed)
        self.assertFalse((self.d / 'operations' / bid / 'latest.json').exists())

    def test_old_hard_stop_cannot_be_hidden_by_a_large_tail(self):
        self.prepare()
        self.rows.insert(-1, {'type': 'padding', 'text': 'x' * 300000}); self.write_events()
        self.assertEqual(supervision.events(self.events)['hard_stop'], 76)

    def test_earlier_writer_failure_is_not_cleared_by_a_later_gate_timeout(self):
        bid = self.prepare()
        command = self.rows[2]['item']['command']
        self.rows[2:2] = [
            {'type': 'item.started', 'item': {'type': 'command_execution', 'id': 'earlier', 'command': command}},
            {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'earlier', 'command': command, 'exit_code': 1}}]
        self.write_events()
        check = mock.Mock(side_effect=AssertionError('absence cannot prove a previous writer stopped'))
        with self.assertRaisesRegex(ValueError, 'earlier legacy write'):
            self.reconcile(bid, check)
        check.assert_not_called()


if __name__ == '__main__':
    unittest.main()
