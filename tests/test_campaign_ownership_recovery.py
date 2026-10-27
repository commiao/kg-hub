"""Synthetic ownership rejection, real receipt locks; no model/network/graph."""
import copy
import json
import os
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_campaign_parallel as parallel
from tools import extract_campaign as ec, campaign_dispatch as dispatch
from tools import campaign_supervision as supervision, campaign_ownership_recovery as recovery
from utils import process_receipts as receipts


class OwnershipRecoveryTests(unittest.TestCase):
    setUpBase, tearDown = parallel.ParallelCampaignTests.setUp, parallel.ParallelCampaignTests.tearDown
    claim, finish = parallel.ParallelCampaignTests.claim, parallel.ParallelCampaignTests.finish

    def setUp(self):
        self.setUpBase()
        self.c['batch'] = 3
        ec.save(self.d, self.c)
        claimed = self.claim('m', 2)
        self.bid = claimed['batch']
        self.finish(self.bid)
        self.c = ec.load(self.d)
        self.batch = ec.find_batch(self.c, self.bid)
        self.source = receipts.read(Path(claimed['batch_file']))['items'][0]
        self.oid = self.source['id']
        self.c['dispatch'] = {'state': 'draining', 'id': 'original-drain'}
        ec.save(self.d, self.c)
        self.root3 = ec.supervisor_dir(self.d, 'm', 3)
        self.root3.mkdir(parents=True)
        self.session = 'original-m-3'
        supervision.bind_slot_session(self.d, 'm', 3, self.session)
        self.c = ec.load(self.d)
        self.old_event = self.root3 / 'production.events.jsonl'
        self.old_event.write_text(json.dumps({'type': 'thread.started', 'thread_id': self.session}) + '\n' + json.dumps({'type': 'turn.completed'}) + '\n')
        receipts.write(self.root3 / 'worker.json', {'session': self.session, 'events': str(self.old_event)})
        with receipts.attempt(self.root3, kind='supervisor') as update:
            update(exit_code=0, phase='claims_drained')
        with receipts.attempt(self.root3 / 'native', kind='native_cli') as update:
            update(exit_code=1)
        native = receipts.read(self.root3 / 'native/latest.json')
        native.update(pid=123, child_pid=124, started_at='2026-10-27T00:00:00+00:00', ended_at='2026-10-27T00:01:00+00:00')
        receipts.write(self.root3 / 'native/latest.json', native)
        self.job = self.root / 'submitted-peer-invocation'
        self.job.mkdir()
        self.event, self.terminal, self.stop = self.job / 'stdout.jsonl', self.job / 'exit-status.json', self.root / 'stop.json'
        entry = str(Path(os.environ.get('KG_HUB_CODE', '~/.local/share/kg-hub/current')).expanduser() / 'tools/extract')
        command = shlex.join([entry, 'campaign', 'review', '--id', self.c['id'], '--batch', self.bid,
                              '--obs', str(self.oid), '--verdict', 'error', '--kind', 'targeted', '--note', 'actual semantic error'])
        self.rows = [{'type': 'thread.started', 'thread_id': self.session},
                     {'type': 'item.started', 'item': {'id': 'denied', 'type': 'command_execution', 'command': command}},
                     {'type': 'item.completed', 'item': {'id': 'denied', 'type': 'command_execution', 'command': command,
                                                       'exit_code': 1, 'aggregated_output': recovery.MESSAGE + '\n'}}]
        self.write_events()
        receipts.write(self.terminal, {'session': self.session, 'exit_code': 1, 'ended_at': native['ended_at']})
        receipts.write(self.job / 'process.json', {'original_session': self.session, 'model': 'm', 'slot': 3, 'pid': 123,
            'stdout': str(self.event), 'exit_receipt': str(self.terminal), 'started_at': native['started_at'],
            'kind': 'submitted_source_peer_repair_only', 'new_source_quota': 0})
        receipts.write(self.stop, {'original_session': self.session, 'native_attempt': native['attempt'], 'child_pid': 124,
            'signal': 'SIGINT to Codex child PID only; no process group killed', 'at': '2026-10-27T00:00:59+00:00'})
        self.plan = self.root / 'preview.json'

    def write_events(self):
        self.event.write_text(''.join(json.dumps(x) + '\n' for x in self.rows))

    def preview(self):
        return recovery.preview(self.c['id'], 'm', 3, events=self.event, terminal=self.terminal,
                                stop=self.stop, reason='explicit original own-scope recovery', out=self.plan)

    def apply(self, plan=None):
        plan = plan or receipts.read(self.plan)
        return recovery.apply(self.c['id'], plan_file=self.plan, confirm=plan['sha256'],
                              reviewer='actual coordinator', reason='approved frozen command inventory; own slot only')

    def test_preview_approval_resume_and_same_session_do_not_erase_failure_or_replay_review(self):
        original = {p: p.read_bytes() for p in [self.event, self.terminal, self.root3 / 'native/latest.json', self.old_event]}
        worker_before = (self.root3 / 'worker.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'terminal'):
            dispatch.resume(self.c['id'], reason='cannot skip refusal')
        plan = self.preview()
        self.assertEqual((self.root3 / 'worker.json').read_bytes(), worker_before)
        with self.assertRaisesRegex(ValueError, 'digest'):
            recovery.apply(self.c['id'], plan_file=self.plan, confirm='wrong', reviewer='coordinator', reason='wrong approval')
        applied = self.apply(plan)
        self.assertEqual(applied['original_exit_code'], 1)
        self.assertFalse(applied['rejected_review_replayed'])
        self.assertEqual(self.apply(plan), applied)
        self.assertTrue(recovery.valid(ec.load(self.d), self.d, 'm', 3))
        self.assertEqual(dispatch.resume(self.c['id'], reason='explicit approved own-slot continuation')['dispatch']['state'], 'running')
        with mock.patch.dict(os.environ, {'KG_HUB_CAMPAIGN_ID': self.c['id'], 'KG_HUB_CAMPAIGN_MODEL': 'm', 'KG_HUB_CAMPAIGN_SLOT': '3'}):
            with self.assertRaisesRegex(SystemExit, 'different worker slot'):
                ec.assert_worker_batch(ec.load(self.d), self.batch)
        spawned = []
        def spawn(args, **kwargs):
            self.assertEqual(args[args.index('resume') + 1], self.session)
            spawned.append(args)
            from tools.campaign_migration import drain
            drain(self.c['id'], 'synthetic turn ends with no source claim')
            with receipts.attempt(self.root3 / 'native', kind='native_cli') as update:
                for row in [{'type': 'thread.started', 'thread_id': self.session}, {'type': 'turn.completed'}]:
                    kwargs['stdout'].write((json.dumps(row) + '\n').encode())
                kwargs['stdout'].flush()
                update(exit_code=0)
            return mock.Mock(poll=lambda: 0, wait=lambda: 0)
        self.assertEqual(supervision.run(self.c['id'], 'm', slot=3, poll=.01, sleep=lambda _: None,
            spawn=spawn, preflight=lambda *a: {'ok': True})['state'], 'drained')
        self.assertEqual(len(spawned), 1)
        self.assertFalse(recovery.valid(ec.load(self.d), self.d, 'm', 3))
        # latest.json legitimately advances on a new attempt, but the actual
        # original attempt and invocation files never become fake success.
        for path, data in original.items():
            if path != self.root3 / 'native/latest.json':
                self.assertEqual(path.read_bytes(), data)
        c = ec.load(self.d)
        self.assertEqual((c['quota'], c['parallel'], c['batch']), (2000, 4, 3))
        self.assertEqual(ec.find_batch(c, self.bid)['slot'], 2)

    def test_refusal_provider_approval_nas_and_ambiguous_commands_remain_blocked(self):
        original = copy.deepcopy(self.rows)
        cases = [lambda r: r[-1]['item'].update(aggregated_output='Permission denied'),
                 lambda r: r[-1]['item'].update(aggregated_output='This action was rejected due to unacceptable risk'),
                 lambda r: r[-1]['item'].update(exit_code=76),
                 lambda r: r[-1]['item'].update(exit_code=2),
                 lambda r: r.append({'type': 'error', 'message': 'capacity or provider failure'}),
                 lambda r: r.append({'type': 'turn.failed'}),
                 lambda r: r.append({'type': 'thread.started', 'thread_id': self.session}),
                 lambda r: r.append({'type': 'item.started', 'item': {'id': 'unresolved', 'type': 'command_execution', 'command': 'python3 unknown.py'}}),
                 lambda r: [x['item'].update(command=x['item']['command'] + ' ; echo bypass') for x in r if x.get('item')]]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                self.rows = copy.deepcopy(original)
                mutate(self.rows)
                self.write_events()
                with self.assertRaises(ValueError):
                    self.preview()

    def test_model_slot_pairing_timestamps_pending_and_own_batch_cannot_be_waived(self):
        processp = self.job / 'process.json'
        originals = {p: p.read_bytes() for p in [processp, self.terminal, self.stop, self.root3 / 'native/latest.json']}
        for path, patch in [(processp, {'slot': 2}), (processp, {'model': 'other'}),
                            (processp, {'pid': 999}), (processp, {'new_source_quota': 1}),
                            (self.terminal, {'session': 'other'}), (self.terminal, {'exit_code': 0}),
                            (self.terminal, {'ended_at': '2026-10-27T00:01:00'}),
                            (self.stop, {'child_pid': 999}), (self.stop, {'native_attempt': 'other'})]:
            with self.subTest(path=path, patch=patch):
                value = json.loads(originals[path]);value.update(patch);receipts.write(path, value)
                with self.assertRaises(ValueError):self.preview()
                path.write_bytes(originals[path])
        with receipts.attempt(self.root3 / 'native', kind='native_cli'):
            with self.assertRaises(receipts.Busy):self.preview()
        (self.root3 / 'native/latest.json').write_bytes(originals[self.root3 / 'native/latest.json'])
        c = ec.load(self.d);ec.find_batch(c, self.bid)['slot'] = 3;ec.save(self.d, c)
        with self.assertRaisesRegex(ValueError, 'owned by another slot'):self.preview()

    def test_changed_inputs_and_new_native_attempt_revoke_explicit_proof(self):
        self.preview()
        before = self.event.read_bytes()
        self.event.write_bytes(before + b'{"type":"item.completed","item":{"type":"agent_message","text":"changed"}}\n')
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.apply()
        self.event.write_bytes(before)
        self.apply()
        self.assertTrue(recovery.valid(ec.load(self.d), self.d, 'm', 3))
        for ref in ['events', 'exit', 'process', 'stop', 'batch']:
            proof = receipts.read(Path(receipts.read(self.root3 / 'ownership-continuation-active.json')['path']))
            p = Path(proof['evidence'][ref]['path']);old = p.read_bytes();p.write_bytes(old + b' ')
            self.assertFalse(recovery.valid(ec.load(self.d), self.d, 'm', 3));p.write_bytes(old)

    def test_operator_identity_required_and_activation_crash_replays_same_proof_only(self):
        with mock.patch.dict(os.environ, {'KG_HUB_CAMPAIGN_SLOT': '3'}):
            with self.assertRaisesRegex(ValueError, 'coordinator'):self.preview()
        plan = self.preview();write = receipts.write
        def crash(path, value):
            if Path(path) == self.root3 / 'worker.json':raise OSError('interrupted worker activation')
            return write(path, value)
        with mock.patch.object(receipts, 'write', side_effect=crash), self.assertRaises(OSError):
            self.apply(plan)
        self.assertFalse(recovery.valid(ec.load(self.d), self.d, 'm', 3))
        result = self.apply(plan)
        self.assertTrue(recovery.valid(ec.load(self.d), self.d, 'm', 3))
        self.assertEqual(len(list((self.root3 / 'ownership-continuations').glob('*/proof.json'))), 1)
        self.assertEqual(result, self.apply(plan))

    def test_other_slot_live_and_changed_frozen_contract_do_not_authorize_recovery(self):
        other = ec.supervisor_dir(self.d, 'n', 1) / 'native'
        with receipts.attempt(other, kind='native_cli'):
            with self.assertRaises(receipts.Busy):self.preview()
        plan = self.preview()
        for key in ['quota', 'parallel', 'batch']:
            c = ec.load(self.d);before = c[key];c[key] += 1;ec.save(self.d, c)
            with self.assertRaisesRegex(ValueError, 'stale'):self.apply(plan)
            c[key] = before;ec.save(self.d, c)
        self.apply(plan)
        c = ec.load(self.d);c['quota'] += 1;ec.save(self.d, c)
        self.assertFalse(recovery.valid(c, self.d, 'm', 3))

    def test_changed_formal_history_and_operator_acknowledgment_remain_gated(self):
        plan = self.preview()
        with self.assertRaisesRegex(ValueError, 'reviewer'):
            recovery.apply(self.c['id'], plan_file=self.plan, confirm=plan['sha256'], reviewer='', reason='explicit')
        log = self.d / 'log.jsonl';old = log.read_bytes()
        rows = [json.loads(x) for x in old.splitlines()];rows[-1]['model'] = 'unrelated'
        log.write_text(''.join(json.dumps(x) + '\n' for x in rows))
        with self.assertRaisesRegex(ValueError, 'formal source'):self.apply(plan)
        log.write_bytes(old);self.apply(plan)
        row = rows[-1];row['model'] = 'm';row['status'] = 'released'
        with log.open('a') as out:out.write(json.dumps(row) + '\n')
        self.assertFalse(recovery.valid(ec.load(self.d), self.d, 'm', 3))

    def test_changed_run_prompt_chain_executor_or_runtime_revokes_approval(self):
        plan = self.preview()
        original = ec.load(self.d)
        for key, value in [('chain', 'new-chain'), ('prompt_sha256', 'different-prompt'), ('executor', 'other')]:
            with self.subTest(key=key):
                changed = copy.deepcopy(original);changed[key] = value;ec.save(self.d, changed)
                with self.assertRaises(ValueError):self.apply(plan)
                ec.save(self.d, original)
        changed = copy.deepcopy(original);changed['models']['m']['run_id'] = 'other-run';ec.save(self.d, changed)
        with self.assertRaisesRegex(ValueError, 'formal source'):self.apply(plan)
        ec.save(self.d, original);self.apply(plan)
        for change in [lambda c: c['models']['m'].update(run_id='different'), lambda c: c.update(chain='different'),
                       lambda c: c.update(prompt_sha256='different'), lambda c: c.update(executor='other')]:
            c = copy.deepcopy(original);change(c)
            self.assertFalse(recovery.valid(c, self.d, 'm', 3))

    def test_successful_prior_repair_requires_exact_source_request_and_verified_terminal(self):
        entry = str(Path(os.environ.get('KG_HUB_CODE', '~/.local/share/kg-hub/current')).expanduser() / 'tools/extract')
        request = self.job / 'reviewed-request.json';applied = self.job / 'apply.json';status = self.job / 'status.json'
        req = {'repair_id': 'synthetic-original-repair', 'source_description': self.source['source_description'],
               'source_obs_id': self.source['source_obs_id'], 'replacement': {'synthetic': True}}
        receipts.write(request, req)
        receipts.write(applied, {'status': 'repaired', 'verified': True, 'repair_id': req['repair_id']})
        receipts.write(status, {'repair': {'repair_id': req['repair_id'], 'state': 'applied', 'request': req,
                                          'result': {'verified': True, 'episode_uuid': 'original-episode'}}})
        rows = []
        for oid, args in [('apply1', ['repair', 'apply', '--file', str(request), '--out', str(applied)]),
                          ('status1', ['repair', 'status', '--repair-id', req['repair_id'], '--out', str(status)])]:
            cmd = shlex.join([entry, *args])
            rows += [{'type': 'item.started', 'item': {'id': oid, 'type': 'command_execution', 'command': cmd}},
                     {'type': 'item.completed', 'item': {'id': oid, 'type': 'command_execution', 'command': cmd,
                                                       'exit_code': 0, 'aggregated_output': 'receipt saved'}}]
        self.rows[1:1] = rows;self.write_events()
        log = self.d / 'log.jsonl';history = [json.loads(x) for x in log.read_text().splitlines()]
        history[0]['episode_uuid'] = 'original-episode'
        log.write_text(''.join(json.dumps(x) + '\n' for x in history))
        self.assertEqual(len(self.preview()['evidence']['verified_prior_repairs']), 1)
        self.plan.unlink()
        old = req.copy();req['source_obs_id'] = 'another-source';receipts.write(request, req)
        with self.assertRaisesRegex(ValueError, 'rejected source'):self.preview()
        receipts.write(request, old)
        value = receipts.read(status);value['repair']['result']['verified'] = False;receipts.write(status, value)
        with self.assertRaisesRegex(ValueError, 'verified'):self.preview()

    def test_misdispatched_source_repair_is_rejected_before_graph_http(self):
        from tools import extraction_repair
        payload = {'source_description': self.source['source_description'], 'source_obs_id': self.source['source_obs_id']}
        request = self.root / 'source-request.json';receipts.write(request, payload)
        post = mock.Mock(return_value=(200, {'status': 'repaired'}))
        bound = {'KG_HUB_CAMPAIGN_ID': self.c['id'], 'KG_HUB_CAMPAIGN_MODEL': 'm', 'KG_HUB_CAMPAIGN_SLOT': '3'}
        with mock.patch.dict(os.environ, bound), mock.patch.object(extraction_repair, 'parse_request', side_effect=lambda p: p), \
                mock.patch.object(extraction_repair.ax, 'api_token') as token:
            with self.assertRaisesRegex(SystemExit, 'different worker slot'):
                extraction_repair.main(['apply', '--file', str(request)], http=post)
            token.assert_not_called()
        post.assert_not_called()
        with mock.patch.dict(os.environ, {**bound, 'KG_HUB_CAMPAIGN_SLOT': '2'}):
            ec.assert_worker_repair_source(payload)
            with self.assertRaisesRegex(SystemExit, 'exact submitted source'):
                ec.assert_worker_repair_source({**payload, 'source_obs_id': 'not-claimed'})
        with mock.patch.dict(os.environ, {}, clear=True):
            ec.assert_worker_repair_source(payload)  # coordinator path retained
        prompt = supervision.native_args(ec.load(self.d), 'm', self.session, 3)[-1]
        self.assertIn('禁止approve()默认全true', prompt)
        self.assertIn('不能接手其他槽位', prompt)


if __name__ == '__main__':
    unittest.main()
