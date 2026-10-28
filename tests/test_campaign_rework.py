"""Drained same-owner rework has zero new claims and no model/network in tests."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_campaign_ownership_recovery as ownership
from tools import campaign_rework as rework, extract_campaign as ec, campaign_supervision as supervision
from utils import process_receipts as receipts


class ReworkTests(unittest.TestCase):
    setUpBase, tearDown = ownership.OwnershipRecoveryTests.setUpBase, ownership.OwnershipRecoveryTests.tearDown
    claim, finish = ownership.OwnershipRecoveryTests.claim, ownership.OwnershipRecoveryTests.finish
    write_events = ownership.OwnershipRecoveryTests.write_events
    preview, apply = ownership.OwnershipRecoveryTests.preview, ownership.OwnershipRecoveryTests.apply

    def setUp(self):
        ownership.OwnershipRecoveryTests.setUp(self)
        self.root2 = ec.supervisor_dir(self.d, 'm', 2)
        self.root2.mkdir(parents=True)
        self.owner_session = 'original-m-2'
        supervision.bind_slot_session(self.d, 'm', 2, self.owner_session)
        self.owner_events = self.root2 / 'original.events.jsonl'
        self.owner_events.write_text(json.dumps({'type': 'thread.started', 'thread_id': self.owner_session}) + '\n' + json.dumps({'type': 'turn.completed'}) + '\n')
        receipts.write(self.root2 / 'worker.json', {'session': self.owner_session, 'events': str(self.owner_events)})
        with receipts.attempt(self.root2 / 'native', kind='native_cli') as update:update(exit_code=0)
        self.assignment = self.root / 'assignment.md'
        self.assignment.write_text('Read each original source and lookahead, preserve history, correct verified errors only.')
        self.spawned = []

    def spawn(self, args, **kwargs):
        self.assertEqual(args[args.index('resume') + 1], self.owner_session)
        self.assertEqual(kwargs['env']['KG_HUB_CAMPAIGN_SLOT'], '2')
        self.assertEqual(json.loads(kwargs['env']['KG_HUB_REWORK_BATCHES']), [self.bid])
        self.assertIn('--approve-for-me', args)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
        self.spawned.append(args)
        # Source assignment changes concurrently after freezing; the issued
        # prompt and evidence must remain bound to the saved private copy.
        self.assignment.write_text('Changed after freeze; must not become scope evidence.')
        with receipts.attempt(self.root2 / 'native', kind='native_cli') as update:
            for row in [{'type': 'thread.started', 'thread_id': self.owner_session}, {'type': 'turn.completed'}]:
                kwargs['stdout'].write((json.dumps(row) + '\n').encode())
            kwargs['stdout'].flush();update(exit_code=0)
        return mock.Mock(wait=lambda: 0)

    def run_rework(self, **kwargs):
        return rework.run(self.c['id'], 'm', 2, batches=[self.bid], assignment=self.assignment,
                          reason='explicit owner-only production rework', spawn=self.spawn,
                          preflight=lambda *a: {'ok': True}, **kwargs)

    def test_original_owner_resumes_under_drain_with_frozen_assignment_and_no_claims(self):
        before = (self.d / 'log.jsonl').read_bytes()
        result = self.run_rework()
        self.assertEqual(result['state'], 'rework_turn_completed')
        self.assertEqual(result['batch_semantic_acceptance'], 'pending_coordinator_verification')
        self.assertEqual(result['new_source_quota'], 0)
        self.assertEqual(ec.load(self.d)['dispatch']['state'], 'draining')
        self.assertEqual((self.d / 'log.jsonl').read_bytes(), before)
        self.assertEqual(len(self.spawned), 1)
        job = Path(result['evidence_dir']);scope = receipts.read(job / 'scope.json')
        self.assertEqual(Path(scope['assignment']['path']), job / 'assignment.md')
        from tools.campaign_recovery import file_sha
        self.assertEqual(file_sha(job / 'assignment.md'), scope['assignment']['sha256'])
        self.assertNotEqual(file_sha(self.assignment), scope['assignment']['sha256'])

    def test_wrong_owner_running_dispatch_and_failed_native_never_spawn(self):
        with self.assertRaisesRegex(ValueError, 'owned'):
            rework.run(self.c['id'], 'm', 3, batches=[self.bid], assignment=self.assignment, reason='wrong owner', spawn=self.spawn)
        c = ec.load(self.d);c['dispatch']['state'] = 'running';ec.save(self.d, c)
        with self.assertRaisesRegex(ValueError, 'draining'):self.run_rework()
        c['dispatch']['state'] = 'draining';ec.save(self.d, c)
        native = receipts.read(self.root2 / 'native/latest.json');native['exit_code'] = 1
        receipts.write(self.root2 / 'native/latest.json', native)
        with self.assertRaisesRegex(ValueError, 'clean idle'):self.run_rework()
        self.assertEqual(self.spawned, [])

    def test_rework_scope_blocks_resubmit_random_reviews_and_out_of_scope_repairs(self):
        bound = {'KG_HUB_CAMPAIGN_ID': self.c['id'], 'KG_HUB_CAMPAIGN_MODEL': 'm',
                 'KG_HUB_CAMPAIGN_SLOT': '2', 'KG_HUB_REWORK_BATCHES': json.dumps([self.bid])}
        with mock.patch.dict(os.environ, bound):
            for command in ['claim', 'submit', 'release', 'quality-check']:
                args = ['--id', self.c['id']]
                if command == 'claim':args += ['--model', 'm', '--slot', '2']
                elif command == 'quality-check':args += ['--batch', self.bid, '--review', str(self.assignment)]
                else:args += ['--batch', self.bid]
                if command == 'release':args += ['--reason', 'synthetic prohibited release']
                with self.assertRaisesRegex(SystemExit, 'quality rework cannot'):ec._main([command, *args])
            with self.assertRaisesRegex(SystemExit, 'random denominator'):
                ec._main(['review', '--id', self.c['id'], '--batch', self.bid, '--obs', str(self.oid), '--verdict', 'ok'])
            with mock.patch.dict(os.environ, {'KG_HUB_REWORK_BATCHES': '[]'}):
                with self.assertRaisesRegex(SystemExit, 'outside explicitly approved'):
                    ec.assert_worker_repair_source({'source_description': self.source['source_description'],
                                                   'source_obs_id': self.source['source_obs_id']})
            from tools import agent_extract as ax
            with mock.patch.object(ax, 'api_token') as token:
                for cmd in ['next', 'submit', 'mark-empty']:
                    with self.assertRaisesRegex(SystemExit, 'standalone agent'):ax.main([cmd])
                token.assert_not_called()

    def adoption(self, kind='business_permission_denied', direct_refusal=False):
        job = self.root / 'owner-private-invocation';job.mkdir()
        out, end = job / 'stdout.jsonl', job / 'exit-status.json'
        rows = [{'type': 'thread.started', 'thread_id': self.owner_session},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps({'recovery_kind': kind})}},
                {'type': 'turn.completed'}]
        if direct_refusal:
            rows[1:1] = [{**row, 'item': {**row['item']}} for row in self.rows[1:]]
        out.write_text(''.join(json.dumps(x) + '\n' for x in rows))
        native = receipts.read(self.root2 / 'native/latest.json')
        native.update(pid=456, started_at='2026-10-27T00:00:00+00:00', ended_at='2026-10-27T00:02:00+00:00')
        receipts.write(self.root2 / 'native/latest.json', native)
        receipts.write(end, {'session': self.owner_session, 'exit_code': 0, 'ended_at': native['ended_at']})
        receipts.write(job / 'process.json', {'original_session': self.owner_session, 'model': 'm', 'slot': 2,
            'pid': 456, 'started_at': native['started_at'], 'stdout': str(out), 'exit_receipt': str(end)})
        return {'adopt_events': out, 'adopt_exit': end}

    def test_peer_refusal_report_needs_exact_reconciled_link_and_is_not_a_second_refusal(self):
        adopted = self.adoption()
        with self.assertRaisesRegex(ValueError, 'exact published ownership'):self.run_rework(**adopted)
        self.preview();self.apply()
        result = self.run_rework(**adopted)
        self.assertEqual(result['state'], 'rework_turn_completed')
        self.assertEqual(len(self.spawned), 1)

    def test_actual_refusal_in_owners_events_is_not_cleared_by_a_peer_proof(self):
        self.preview();self.apply()
        adopted = self.adoption(direct_refusal=True)
        with self.assertRaisesRegex(ValueError, 'failed public operation'):self.run_rework(**adopted)
        self.assertEqual(self.spawned, [])

    def test_same_slot_outside_batch_list_is_rejected_and_released_history_does_not_hide_owner(self):
        c = ec.load(self.d);c['dispatch']['state'] = 'running';ec.save(self.d, c)
        extra = self.claim('m', 2);self.finish(extra['batch'])
        c = ec.load(self.d);c['dispatch']['state'] = 'draining'
        # A real release can leave the same source key in a historical batch;
        # only current non-released ownership participates in the write guard.
        import copy
        historical = copy.deepcopy(ec.find_batch(c, self.bid));historical['id'] += '-released';historical['status'] = 'released'
        c['batches'].append(historical);ec.save(self.d, c)
        (self.d / 'batches' / (historical['id'] + '.json')).write_bytes((self.d / 'batches' / (self.bid + '.json')).read_bytes())
        bound = {'KG_HUB_CAMPAIGN_ID': self.c['id'], 'KG_HUB_CAMPAIGN_MODEL': 'm', 'KG_HUB_CAMPAIGN_SLOT': '2',
                 'KG_HUB_REWORK_BATCHES': json.dumps([self.bid])}
        other_source = receipts.read(Path(extra['batch_file']))['items'][0]
        with mock.patch.dict(os.environ, bound):
            ec.assert_worker_repair_source({'source_description': self.source['source_description'], 'source_obs_id': self.source['source_obs_id']})
            with self.assertRaisesRegex(SystemExit, 'outside explicitly approved'):
                ec.assert_worker_repair_source({'source_description': other_source['source_description'], 'source_obs_id': other_source['source_obs_id']})
            with self.assertRaisesRegex(SystemExit, 'outside explicitly approved'):
                ec._main(['review', '--id', self.c['id'], '--batch', extra['batch'], '--obs', str(other_source['id']),
                          '--verdict', 'error', '--kind', 'targeted'])

    def test_live_duplicate_and_adopted_failure_do_not_spawn_a_new_turn(self):
        with receipts.attempt(self.root2 / 'native', kind='native_cli'):
            with self.assertRaises(receipts.Busy):self.run_rework()
        with self.assertRaisesRegex(ValueError, 'unique explicit'):
            rework.run(self.c['id'], 'm', 2, batches=[self.bid, self.bid], assignment=self.assignment,
                       reason='duplicate scope rejected', spawn=self.spawn)
        # Restore a clean real receipt fixture before checking an invalid
        # adoption; do not let a previous failure mask this negative case.
        with receipts.attempt(self.root2 / 'native', kind='native_cli') as update:update(exit_code=0)
        adopted = self.adoption(kind='nas_refused')
        with self.assertRaisesRegex(ValueError, 'exact published ownership'):self.run_rework(**adopted)
        end = receipts.read(adopted['adopt_exit']);end['exit_code'] = 1;receipts.write(adopted['adopt_exit'], end)
        with self.assertRaisesRegex(ValueError, 'successful native'):self.run_rework(**adopted)
        self.assertEqual(self.spawned, [])

    def test_public_cli_reports_blocked_turn_with_nonzero_exit(self):
        with mock.patch.object(rework, 'run', return_value={'state': 'blocked'}) as run:
            self.assertEqual(ec._main(['rework', '--id', self.c['id'], '--model', 'm', '--slot', '2',
                                      '--batches', self.bid, '--assignment', str(self.assignment), '--reason', 'explicit']), 2)
        run.assert_called_once()

    def quality_stop_events(self, command=None, code=0, **report_changes):
        command = command or (f'"${{KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}}/tools/extract" '
                              f'campaign operation-status --id {self.c["id"]} --batch {self.bid}')
        report = {'recovery_kind': 'quality_process_violation', 'model': 'm', 'batch': self.bid,
                  'submitted': {'ok': 0, 'empty': 0, 'error': 0}, 'prepare_runs': 0,
                  'dryrun_mismatch_seen': 0, 'renamed_after_dryrun': 0}
        report.update(report_changes)
        rows = [{'type': 'thread.started', 'thread_id': self.owner_session},
                {'type': 'item.started', 'item': {'id': 'read', 'type': 'command_execution', 'command': command}},
                {'type': 'item.completed', 'item': {'id': 'read', 'type': 'command_execution',
                    'command': command, 'exit_code': code, 'aggregated_output': 'synthetic state read'}},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(report)}},
                {'type': 'turn.completed'}]
        self.owner_events.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return rows

    def test_clean_quality_violation_stays_fatal_for_production_but_enters_scoped_rework(self):
        self.quality_stop_events()
        before = self.owner_events.read_bytes()
        old_native = (self.root2 / 'native/latest.json').read_bytes()
        self.assertFalse(supervision.normal_recovery('quality_process_violation'))
        out = self.run_rework()
        self.assertEqual(out['state'], 'rework_turn_completed')
        job = Path(out['evidence_dir'])
        evidence = receipts.read(job / 'quality-stop-continuation.json')
        self.assertFalse(evidence['production_recovery_authorized'])
        self.assertFalse(evidence['semantic_acceptance'])
        self.assertEqual(evidence['new_source_quota'], 0)
        self.assertEqual(evidence['original_report']['recovery_kind'], 'quality_process_violation')
        self.assertEqual(self.owner_events.read_bytes(), before)
        self.assertEqual((job / 'previous-native.json').read_bytes(), old_native)
        self.assertEqual(ec.load(self.d)['dispatch']['state'], 'draining')

    def test_quality_stop_rejects_nonzero_commands_and_production_activity(self):
        for changes in ({'code': 1}, {'model': 'other'}, {'batch': 'other-batch'},
                        {'prepare_runs': 1}, {'submitted': {'ok': 1, 'empty': 0, 'error': 0}}):
            with self.subTest(changes=changes):
                self.quality_stop_events(**changes)
                with self.assertRaises(ValueError):self.run_rework()
        self.assertEqual(self.spawned, [])

    def test_quality_stop_rejects_writes_hidden_python_and_compound_commands(self):
        entry = str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser())
        commands = [f'{entry} campaign claim --id {self.c["id"]} --model m',
                    f'{entry} campaign rules --id {self.c["id"]}; true',
                    "python3 - <<'PY'\nimport subprocess\nsubprocess.run(['true'])\nPY",
                    "python3 - <<'PY'\nimport json\neval('1')\nPY",
                    f"python3 - <<'PY'\nfrom pathlib import Path\np=Path({str(self.d / 'campaign.json')!r})\np.write_text('changed')\nPY",
                    "python3 - <<'PY'\nimport pathlib\np=pathlib.Path('/private/tmp/unused')\np.unlink()\nPY"]
        for command in commands:
            with self.subTest(command=command):
                self.quality_stop_events(command=command)
                with self.assertRaises(ValueError):self.run_rework()
        self.assertEqual(self.spawned, [])

    def test_quality_stop_accepts_and_binds_private_audit_only_python(self):
        audit = self.d / 'audit' / 'quality-stop'
        audit.mkdir(parents=True)
        source = self.root / 'historical-review.py';source.write_text('historical review only')
        target = audit / 'report.json';target.write_text('{"kind":"quality_process_violation"}')
        command = ("python3 - <<'PY'\nimport ast,hashlib,json,pathlib,datetime\n"
                   f"root=pathlib.Path({str(self.d)!r})\np=pathlib.Path({str(source)!r})\n"
                   "data=p.read_bytes(); tree=ast.parse('x=1'); calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call)]\n"
                   "out=root/'audit/quality-stop';out.mkdir(exist_ok=True)\n"
                   "r={'sha256':hashlib.sha256(data).hexdigest(),'at':datetime.datetime.now(datetime.timezone.utc).isoformat()}\n"
                   "(out/'report.json').write_text(json.dumps(r))\nPY")
        # isoformat is not needed by the validator's read/write policy: time
        # serialization is a local pure call explicitly accepted below.
        self.quality_stop_events(command=command)
        out = self.run_rework()
        evidence = receipts.read(Path(out['evidence_dir']) / 'quality-stop-continuation.json')
        refs = {v['path'] for v in evidence['local_evidence']}
        self.assertIn(str(source.resolve()), refs)
        self.assertIn(str(target.resolve()), refs)

    def test_quality_stop_requires_complete_unique_original_event_lifecycle(self):
        for mutation in ('unfinished', 'post-terminal', 'another-session', 'provider-error'):
            rows = self.quality_stop_events()
            if mutation == 'unfinished':rows.pop(2)
            elif mutation == 'post-terminal':rows.append(rows[1])
            elif mutation == 'another-session':rows[0]['thread_id'] = 'wrong'
            else:rows.insert(1, {'type': 'error', 'message': 'provider refused'})
            self.owner_events.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):self.run_rework()
        self.assertEqual(self.spawned, [])

    def test_quality_stop_cannot_enter_through_alternate_adoption(self):
        adopted = self.adoption(kind='quality_process_violation')
        with self.assertRaisesRegex(ValueError, 'exact published ownership'):
            self.run_rework(**adopted)
        self.assertEqual(self.spawned, [])

    def test_quality_stop_rechecks_bound_evidence_after_preflight(self):
        self.quality_stop_events()
        def flight(*args):
            self.owner_events.write_text(self.owner_events.read_text() + '\n')
            return {'ok': True}
        with self.assertRaisesRegex(ValueError, 'changed before native startup'):
            rework.run(self.c['id'], 'm', 2, batches=[self.bid], assignment=self.assignment,
                       reason='explicit owner rework', spawn=self.spawn, preflight=flight)
        self.assertEqual(self.spawned, [])


class OriginalOwnerContinuationReworkTests(unittest.TestCase):
    """An already approved failed owner may rework only its own submitted batch."""
    setUpBase, tearDown = ownership.OwnershipRecoveryTests.setUpBase, ownership.OwnershipRecoveryTests.tearDown
    claim, finish = ownership.OwnershipRecoveryTests.claim, ownership.OwnershipRecoveryTests.finish
    write_events = ownership.OwnershipRecoveryTests.write_events
    preview, apply = ownership.OwnershipRecoveryTests.preview, ownership.OwnershipRecoveryTests.apply

    def setUp(self):
        ownership.OwnershipRecoveryTests.setUp(self)
        c = ec.load(self.d)
        drained = dict(c['dispatch'])
        c['dispatch']['state'] = 'running';ec.save(self.d, c)
        own = self.claim('m', 3)
        self.own_bid = own['batch']
        self.finish(self.own_bid)
        c = ec.load(self.d);c['dispatch'] = drained;ec.save(self.d, c)
        self.c = c
        # Match the actual managed supervisor identity in this synthetic fixture.
        original = receipts.read(self.root3 / 'latest.json')
        original['identity'] = {'campaign': c['id'], 'model': 'm', 'slot': 3}
        receipts.write(self.root3 / 'latest.json', original)
        self.assignment = self.root / 'own-submitted-rework.md'
        self.assignment.write_text('Review the selected original own-slot submitted sources only; no new quota.')
        self.spawned = []

    def spawn(self, args, **kwargs):
        self.assertEqual(args[args.index('resume') + 1], self.session)
        self.assertEqual(kwargs['env']['KG_HUB_CAMPAIGN_SLOT'], '3')
        self.assertEqual(json.loads(kwargs['env']['KG_HUB_REWORK_BATCHES']), [self.own_bid])
        self.spawned.append(args)
        with receipts.attempt(self.root3 / 'native', kind='native_cli') as update:
            for row in [{'type': 'thread.started', 'thread_id': self.session}, {'type': 'turn.completed'}]:
                kwargs['stdout'].write((json.dumps(row) + '\n').encode())
            kwargs['stdout'].flush();update(exit_code=0)
        return mock.Mock(wait=lambda: 0)

    def run_own_rework(self, **kwargs):
        return rework.run(self.c['id'], 'm', 3, batches=[self.own_bid], assignment=self.assignment,
                          reason='explicit same owner submitted-source rework', spawn=self.spawn,
                          preflight=lambda *a: {'ok': True}, **kwargs)

    def test_registered_owner_refusal_allows_original_session_own_batch_and_preserves_failure(self):
        self.preview();self.apply()
        c = ec.load(self.d)
        # The approved protocol remains valid under the exact caller idle locks.
        from tools.campaign_ownership_recovery import valid
        with receipts.idle(self.root3), receipts.idle(self.root3 / 'native'):
            self.assertTrue(valid(c, self.d, 'm', 3))
        before = {p: p.read_bytes() for p in [self.event, self.terminal, self.root3 / 'native/latest.json',
                                             self.root3 / 'worker.json']}
        outcome = self.run_own_rework()
        self.assertEqual(outcome['state'], 'rework_turn_completed')
        self.assertEqual(outcome['new_source_quota'], 0)
        self.assertEqual(ec.load(self.d)['dispatch']['state'], 'draining')
        self.assertEqual(len(self.spawned), 1)
        job = Path(outcome['evidence_dir'])
        self.assertEqual((job / 'previous-native.json').read_bytes(), before[self.root3 / 'native/latest.json'])
        self.assertEqual((job / 'previous-worker.json').read_bytes(), before[self.root3 / 'worker.json'])
        self.assertEqual(receipts.read(job / 'previous-native.json')['exit_code'], 1)
        self.assertTrue((job / 'ownership-continuation.json').is_file())
        self.assertEqual(self.event.read_bytes(), before[self.event])
        self.assertEqual(self.terminal.read_bytes(), before[self.terminal])
        self.assertEqual(ec.batch_slot(ec.load(self.d), ec.find_batch(ec.load(self.d), self.bid)), 2)
        self.assertFalse(valid(ec.load(self.d), self.d, 'm', 3))

    def test_failed_owner_without_registered_proof_cannot_start_rework(self):
        with self.assertRaisesRegex(ValueError, 'clean idle'):
            self.run_own_rework()
        self.assertEqual(self.spawned, [])

    def test_changed_native_or_ownership_proof_cannot_start_rework(self):
        self.preview();self.apply()
        active = receipts.read(self.root3 / 'ownership-continuation-active.json')
        for path in [self.root3 / 'native/latest.json', Path(active['path'])]:
            original = path.read_bytes()
            if path == self.root3 / 'native/latest.json':
                value = json.loads(original);value['attempt'] = 'different-native-attempt'
                receipts.write(path, value)
            else:
                path.write_bytes(original + b' ')
            with self.subTest(path=path.name):
                with self.assertRaisesRegex(ValueError, 'clean idle'):
                    self.run_own_rework()
            path.write_bytes(original)
        self.assertEqual(self.spawned, [])

    def test_ownership_proof_never_transfers_peer_batch_or_adopts_another_invocation(self):
        self.preview();self.apply()
        with self.assertRaisesRegex(ValueError, 'owned'):
            rework.run(self.c['id'], 'm', 3, batches=[self.bid], assignment=self.assignment,
                       reason='must reject peer batch', spawn=self.spawn)
        with self.assertRaisesRegex(ValueError, 'cannot adopt'):
            self.run_own_rework(adopt_events=self.event, adopt_exit=self.terminal)
        self.assertEqual(self.spawned, [])


if __name__ == '__main__':unittest.main()
