"""Historical failure shapes with real leases; no model or NAS requests."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_campaign_parallel as fixtures
from tools import campaign_recovery_manager as manager, campaign_supervision as supervision
from tools import extract_campaign as ec
from utils import process_receipts as receipts


class RecoveryManagerTests(unittest.TestCase):
    setUp, tearDown, claim, finish = (fixtures.ParallelCampaignTests.setUp,
        fixtures.ParallelCampaignTests.tearDown, fixtures.ParallelCampaignTests.claim,
        fixtures.ParallelCampaignTests.finish)

    def prepare(self, *, draining=False):
        self.bid = self.claim()['batch']
        c = ec.load(self.d)
        c['dispatch'] = {'state': 'draining' if draining else 'running', 'id': 'generation-1'}
        ec.save(self.d, c)
        self.owner = ec.supervisor_dir(self.d, 'm', 1)
        supervision.bind_slot_session(self.d, 'm', 1, 'original-session')
        self.owner.mkdir(parents=True, exist_ok=True)
        self.event = self.owner / 'native.events.jsonl'
        self.write_turn()
        manager.enable(self.c['id'], reason='explicit synthetic recovery policy')

    def write_turn(self, kind='none', code=0):
        rows = [{'type': 'thread.started', 'thread_id': 'original-session'},
                {'type': 'item.completed', 'item': {'type': 'agent_message',
                  'text': json.dumps({'recovery_kind': kind})}}, {'type': 'turn.completed'}]
        self.event.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        with receipts.attempt(self.owner / 'native', kind='native_cli') as update:
            update(exit_code=code)
        receipts.write(self.owner / 'worker.json', {'session': 'original-session', 'events': str(self.event)})

    def row(self):
        return next(r for r in manager.snapshot(self.c['id'])['slots'] if (r['model'], r['slot']) == ('m', 1))

    def register(self):
        self.finish(self.bid)
        self.audit = self.d / 'audit/registered'; self.audit.mkdir(parents=True)
        self.assignment = self.audit / 'assignment.md'; self.assignment.write_text('Own-source full rework; no new quota.')
        self.checkpoint = self.audit / 'checkpoint.json'
        self.ids = ec.find_batch(ec.load(self.d), self.bid)['ids']
        self.progress(0)
        return manager.register_rework(self.c['id'], 'm', 1, batches=[self.bid], assignment=self.assignment,
            checkpoint=self.checkpoint, reason='Original quota rework already authorized')

    def progress(self, n):
        receipts.write(self.checkpoint, {'reviewed': self.ids[:n], 'pending': self.ids[n:],
            'repairs_verified': [], 'in_flight': None})

    def test_dispositions_cover_historical_stops_and_unknowns(self):
        native = {'state': 'completed', 'exit_code': 0, 'ended_at': 't'}
        for kind, action in [('none', 'same_session_continuation'),
                             ('diagnostic_unavailable', 'same_session_continuation'),
                             ('quality_process_violation', 'registered_rework_required'),
                             ('nas_refused', 'nas_reconciliation_required'),
                             ('approval_rejected', 'approval_required'),
                             ('business_permission_denied', 'engineering_reconciliation_required'),
                             ('uncertain_submit', 'engineering_reconciliation_required'),
                             ('infrastructure_failed', 'bounded_infrastructure_continuation'),
                             ('new-unknown-case', 'bounded_unknown_continuation')]:
            with self.subTest(kind=kind):
                self.assertEqual(manager.classify({'terminal': 'turn.completed', 'recovery_kind': kind}, native)[0], action)
        unknown = ({'terminal': 'turn.completed'}, {**native, 'exit_code': 143})
        self.assertEqual(manager.classify(*unknown), ('bounded_unknown_continuation', 'slot'))
        self.assertEqual(manager.classify(*unknown, {'unknown_retries': 2}), ('coordinator_required', 'campaign'))
        self.assertEqual(manager.classify({'terminal': 'turn.completed', 'hard_stop': 76}, native), ('bounded_infrastructure_continuation', 'slot'))
        self.assertEqual(manager.classify({'terminal': 'turn.completed', 'hard_stop': 76}, native,
                                          {'infra_streak_started_at': 1.0}), ('coordinator_required', 'campaign'))
        self.assertEqual(manager.classify({'terminal': 'turn.completed', 'hard_stop': 75}, native), ('nas_reconciliation_required', 'campaign'))
        self.assertEqual(manager.classify({'terminal': 'turn.failed', 'infra_signals': [{'kind': 'http_5xx'}]},
                                          {**native, 'exit_code': 1}), ('bounded_infrastructure_continuation', 'slot'))

    def test_unknown_terminal_is_supervised_within_budget_then_blocked_with_raw_terminal(self):
        self.prepare()
        self.write_turn('new-unknown-case', code=3)
        self.assertEqual(self.row()['action'], 'supervise')
        self.assertFalse(list((self.d / 'recovery/incidents').glob('*.json')))
        stderr = self.owner / 'native.stderr.log'; stderr.write_text('Traceback: nobody saw this')
        receipts.write(self.owner / 'worker.json', {'session': 'original-session', 'events': str(self.event),
                                                    'stderr': str(stderr), 'unknown_retries': 2})
        row = self.row()
        self.assertEqual((row['action'], row['scope']), ('coordinator_required', 'campaign'))
        self.assertEqual((row['blocked']['exit_code'], row['blocked']['recovery_kind'], row['blocked']['unknown_retries']),
                         (3, 'new-unknown-case', 2))
        self.assertIn('nobody saw this', row['blocked']['stderr_tail'])
        with mock.patch.object(manager.subprocess, 'Popen') as spawn:
            manager.tick(self.c['id']); spawn.assert_not_called()
        self.assertEqual(ec.load(self.d)['dispatch']['state'], 'draining')
        self.assertTrue(list((self.d / 'recovery/incidents').glob('*.json')))

    def test_live_original_and_unknown_write_never_launch(self):
        self.prepare()
        with receipts.attempt(self.owner / 'native', kind='native_cli'):
            with mock.patch.object(manager.subprocess, 'Popen') as spawn:
                manager.tick(self.c['id']); spawn.assert_not_called()
        self.write_turn()
        op = self.d / 'operations' / self.bid
        with receipts.attempt(op, kind='submit') as update: update(may_have_written=True)
        self.assertEqual(self.row()['action'], 'unknown_write_reconciliation_required')
        with mock.patch.object(manager.subprocess, 'Popen') as spawn:
            manager.tick(self.c['id']); spawn.assert_not_called()

    def test_observer_restart_same_event_executes_once(self):
        self.prepare()
        spawned = []
        def spawn(args, **kw):
            spawned.append(args)
            return mock.Mock(pid=123)
        # Crash before a child takes its lease may schedule the same event
        # again. The event itself is locked and its result prevents replay.
        manager.tick(self.c['id'], spawn=spawn)
        manager.tick(self.c['id'], spawn=spawn)
        self.assertEqual(spawned[0], spawned[1])
        event = spawned[0][-1]
        with mock.patch.object(supervision, 'run', return_value={'state': 'drained'}) as run:
            manager.step(self.c['id'], event)
            manager.step(self.c['id'], event)
            run.assert_called_once()
        spawned.clear(); manager.tick(self.c['id'], spawn=spawn)
        self.assertEqual(spawned, [])

    def test_stale_event_and_quota_change_do_not_start(self):
        self.prepare()
        calls = []
        manager.tick(self.c['id'], spawn=lambda args, **kw: (calls.append(args) or mock.Mock(pid=1)))
        self.write_turn('approval_rejected')
        with mock.patch.object(supervision, 'run') as run:
            self.assertEqual(manager.step(self.c['id'], calls[0][-1])['state'], 'stale')
            run.assert_not_called()
        c = ec.load(self.d); c['quota'] += 1; ec.save(self.d, c)
        with self.assertRaisesRegex(ValueError, 'contract changed'): manager.snapshot(self.c['id'])

    def test_drained_generation_never_automatically_resumes_without_acceptance(self):
        self.prepare(draining=True)
        self.assertEqual(self.row()['action'], 'dispatch_draining')
        with mock.patch('tools.campaign_dispatch.resume') as resume:
            manager.tick(self.c['id'], spawn=lambda *a, **kw: self.fail('unexpected spawn'))
            resume.assert_not_called()

    def test_rework_progress_is_separate_from_formal_and_does_not_certify_acceptance(self):
        self.prepare(draining=True); self.register()
        self.progress(15)
        before = manager.snapshot(self.c['id'])
        self.assertEqual(self.row()['action'], 'rework')
        self.progress(19)
        self.assertEqual(self.row()['progress']['reviewed'], 19)
        self.assertEqual(before['formal'], manager.snapshot(self.c['id'])['formal'])
        self.progress(50)
        self.assertEqual(self.row()['action'], 'coordinator_semantic_acceptance_required')

    def test_quality_stop_needs_registration_and_preserves_public_rework_validation(self):
        self.prepare(draining=True); self.write_turn('quality_process_violation')
        # No supplied semantic assignment: never invent an automatic approval.
        self.assertNotEqual(self.row()['action'], 'rework')
        self.register()
        calls = []
        manager.tick(self.c['id'], spawn=lambda args, **kw: (calls.append(args) or mock.Mock(pid=1)))
        with mock.patch('tools.campaign_rework.run', side_effect=ValueError('exact public rejection')) as run:
            result = manager.step(self.c['id'], calls[0][-1])
            self.assertEqual(result['state'], 'blocked')
            self.assertEqual(result['message'], 'exact public rejection')
            manager.step(self.c['id'], calls[0][-1])
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs['batches'], [self.bid])
        self.assertEqual(ec.load(self.d)['dispatch']['state'], 'draining')

    def test_released076_cannot_be_registered_as_submitted039_rework(self):
        self.prepare(draining=True); self.register()
        c = ec.load(self.d); ec.find_batch(c, self.bid)['status'] = 'released'; ec.save(self.d, c)
        with self.assertRaisesRegex(ValueError, 'own submitted'):
            manager.register_rework(self.c['id'], 'm', 1, batches=[self.bid], assignment=self.assignment,
                checkpoint=self.checkpoint, reason='cannot use submitted rework to waive released violation')

    def test_assignment_tamper_and_checkpoint_overlap_are_rejected(self):
        self.prepare(draining=True); self.register()
        self.assignment.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'evidence changed'): self.row()
        self.assignment.write_text('Own-source full rework; no new quota.')
        receipts.write(self.checkpoint, {'reviewed': [self.ids[0]], 'pending': self.ids})
        with self.assertRaisesRegex(ValueError, 'disjoint'): self.row()

    def test_original_source_bytes_cannot_change_under_registered_rework(self):
        self.prepare(draining=True); self.register()
        path = self.d / 'batches' / (self.bid + '.json')
        value = json.loads(path.read_text()); value['items'][0]['episode_body'] = 'changed original source'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'evidence changed'): self.row()

    def test_acceptance_is_bound_to_exact_drain_and_evidence_bytes(self):
        self.prepare(draining=True)
        audit = self.d / 'audit'; audit.mkdir(); evidence = audit / 'accepted.json'; evidence.write_text('{"accepted":true}')
        manager.approve_resume(self.c['id'], evidence=[evidence], reason='Coordinator source acceptance')
        c = ec.load(self.d); c['dispatch']['id'] = 'different-stop'; ec.save(self.d, c)
        with mock.patch('tools.campaign_dispatch.resume') as resume:
            manager.tick(self.c['id']); resume.assert_not_called()
        c['dispatch']['id'] = 'generation-1'; ec.save(self.d, c); evidence.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'evidence changed'): manager.tick(self.c['id'])

    def test_real_refusals_are_never_classed_as_local_pool_failures(self):
        for reason in ('native_worker_blocked', 'nas_gate_refused', 'uncertain_submit', 'preflight_failed'):
            self.assertFalse(manager.isolated_slot_failure({'reason': reason}))
        self.assertTrue(manager.isolated_slot_failure({'reason': 'model_capacity_exhausted'}))

    def test_shared_quality_stop_drains_once_from_current_native_not_old_pool(self):
        self.prepare(); self.write_turn('quality_process_violation')
        with mock.patch.object(manager.subprocess, 'Popen') as spawn:
            manager.tick(self.c['id']); spawn.assert_not_called()
        first = copy.deepcopy(ec.load(self.d)['dispatch'])
        self.assertEqual(first['state'], 'draining')
        manager.tick(self.c['id'])
        self.assertEqual(first, ec.load(self.d)['dispatch'])
        self.assertTrue(list((self.d / 'recovery/incidents').glob('*.json')))

    def test_failed_prerequisite_does_not_reset_on_new_native_or_other_slot_progress(self):
        self.prepare();calls=[]
        manager.tick(self.c['id'],spawn=lambda args,**kw:(calls.append(args) or mock.Mock(pid=1)))
        with mock.patch.object(supervision,'run',return_value={'state':'blocked','reason':'no_formal_progress'}) as run:
            manager.step(self.c['id'],calls[0][-1]);run.assert_called_once()
        self.write_turn()  # New invocation ID is not progress or a fix.
        other=self.claim('m',2)['batch'];self.finish(other)
        self.assertEqual(self.row()['action'],'unchanged_blocked')
        manager.disable(self.c['id'],reason='observer restart')
        manager.enable(self.c['id'],reason='same unchanged inputs')
        calls.clear();manager.tick(self.c['id'],spawn=lambda args,**kw:(calls.append(args) or mock.Mock(pid=1)))
        self.assertEqual(calls,[])
        self.assertEqual(ec.load(self.d)['dispatch']['state'],'running')

    def test_changed_source_candidate_can_be_reobserved_after_stall(self):
        self.prepare();calls=[]
        manager.tick(self.c['id'],spawn=lambda args,**kw:(calls.append(args) or mock.Mock(pid=1)))
        with mock.patch.object(supervision,'run',return_value={'state':'blocked','reason':'no_formal_progress'}):
            manager.step(self.c['id'],calls[0][-1])
        self.assertEqual(self.row()['action'],'unchanged_blocked')
        path=self.d/'results'/(self.bid+'.json');path.write_text('{"items":[],"revised":true}')
        self.assertEqual(self.row()['action'],'supervise')

    def test_two_observers_cannot_execute_same_owner_event_concurrently(self):
        self.prepare(); calls = []
        manager.tick(self.c['id'], spawn=lambda args, **kw: (calls.append(args) or mock.Mock(pid=1)))
        entered, finish = threading.Event(), threading.Event()
        def run(*a, **kw):
            entered.set(); self.assertTrue(finish.wait(5)); return {'state': 'drained'}
        with mock.patch.object(supervision, 'run', side_effect=run) as launch, ThreadPoolExecutor(1) as pool:
            first = pool.submit(manager.step, self.c['id'], calls[0][-1])
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(receipts.Busy): manager.step(self.c['id'], calls[0][-1])
            finally: finish.set()
            first.result(timeout=5)
            launch.assert_called_once()

    def test_registration_progress_is_required_even_after_explicit_resume_request(self):
        self.prepare(draining=True); self.register(); self.progress(15)
        evidence = self.audit / 'accepted.json'; evidence.write_text('{}')
        manager.approve_resume(self.c['id'], evidence=[evidence], reason='premature acceptance cannot bypass pending35')
        with mock.patch('tools.campaign_dispatch.resume') as resume:
            plan = manager.tick(self.c['id'], spawn=lambda *a, **kw: mock.Mock(pid=1))
            resume.assert_not_called()
        self.assertEqual(plan['resume']['reason'], 'registered_rework_incomplete')

    def test_observer_recovers_crash_after_public_resume_before_retiring_registration(self):
        self.prepare(draining=True); self.register(); self.progress(50)
        evidence = self.audit / 'accepted.json'; evidence.write_text('{}')
        manager.approve_resume(self.c['id'], evidence=[evidence], reason='source scope accepted')
        c = ec.load(self.d); c['dispatch'] = {'state': 'running', 'id': 'real-resume'}; ec.save(self.d, c)
        receipts.write(self.d / 'dispatch-resumes/real-resume.json', {'previous_dispatch': {'id': 'generation-1'}})
        calls = []
        manager.tick(self.c['id'], spawn=lambda args, **kw: (calls.append(args) or mock.Mock(pid=1)))
        self.assertEqual(self.row()['action'], 'supervise')
        self.assertEqual(len(calls), 1)
        self.assertTrue(list((self.d / 'recovery/reworks').glob('*.accepted-generation-1.json')))

    def test_service_reenters_stable_public_entry_and_disable_leaves_native_intact(self):
        import plistlib
        self.prepare()
        output = self.root / 'service.plist'
        manager.service_plist(self.c['id'], output)
        p = plistlib.loads(output.read_bytes())
        self.assertEqual(p['StartInterval'], 45)
        self.assertIn('/current/tools/extract', p['ProgramArguments'][0])
        self.assertNotIn('LLM_EVAL_DELEGATED_BACKEND', p['EnvironmentVariables'])
        native = (self.owner / 'native/latest.json').read_bytes()
        manager.disable(self.c['id'], reason='explicit finish')
        self.assertEqual(manager.tick(self.c['id'])['state'], 'disabled')
        self.assertEqual(native, (self.owner / 'native/latest.json').read_bytes())

    def test_enabled_pool_keeps_unrelated_slots_after_bounded_local_failure(self):
        self.prepare()
        called = []
        def run(cid, model, **kw):
            called.append(kw['slot'])
            return ({'state': 'blocked', 'reason': 'no_formal_progress'} if kw['slot'] == 1 else
                    {'state': 'complete', 'success': 2000})
        with mock.patch.object(supervision, 'run', side_effect=run):
            result = supervision.run_pool(self.c['id'], 'm', poll=.001, stagger=0)
        self.assertEqual(set(called), {1, 2, 3, 4})
        self.assertEqual(result['state'], 'blocked')

    def test_two_slot_failures_do_not_turn_into_campaign_success(self):
        self.prepare()
        with mock.patch.object(supervision, 'run', return_value={'state': 'blocked', 'reason': 'model_capacity_exhausted'}):
            result = supervision.run_pool(self.c['id'], 'm', poll=.001, stagger=0)
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['success'], 0)

    def test_real_observer_crash_restart_public_step_and_supervisor_finish_original_quota(self):
        self.prepare()
        c = ec.load(self.d); c['quota'] = 50; ec.save(self.d, c)
        manager.enable(self.c['id'], reason='Synthetic quota contract')
        env = dict(os.environ)
        env['PYTHONPATH'] = (str(Path(__file__).resolve().parent.parent) + os.pathsep +
                             str(Path(__file__).parent) + os.pathsep + env.get('PYTHONPATH', ''))
        env['SYNTHETIC_WORKSPACE'] = str(self.workspace)
        command = [sys.executable, str(Path(__file__).resolve()), '--synthetic-observer', self.c['id']]
        crashed = subprocess.run(command + ['crash'], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(crashed.returncode, 17, crashed.stderr)
        self.assertEqual(receipts.status(self.d / 'recovery/observer')['state'], 'uncertain')
        resumed = subprocess.run(command + ['resume'], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(supervision.completed(ec.load(self.d), self.d, 'm'), 50)
        self.assertEqual((self.d / 'synthetic-native-calls.txt').read_text().splitlines(), ['original-session'])
        # Restart once more: no duplicated native, no extra claim/source.
        again = subprocess.run(command + ['resume'], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((self.d / 'synthetic-native-calls.txt').read_text().splitlines(), ['original-session'])
        self.assertEqual(len(ec.load(self.d)['batches']), 1)


def synthetic_observer(cid, mode):
    """Separate OS processes exercise abandoned leases and the real supervisor.

    Only native inference/HTTP preflight are replaced by deterministic fixtures.
    Claims, formal counters, owner/session guards and process receipts are real
    temporary campaign code paths. Nothing targets a service or real campaign.
    """
    from types import SimpleNamespace
    from tools import agent_extract as ax
    ec.tracking_guard = lambda: {'excluded': True}
    ec.workspace_dir = lambda: Path(os.environ['SYNTHETIC_WORKSPACE'])
    ax.api_token = lambda: None
    d = ec.campaign_dir(cid)
    if mode == 'step':
        original = supervision.run
        def fake_native(args, **kw):
            with receipts.attempt(Path(args[args.index('--receipt-dir') + 1]), kind='native_cli') as update:
                update(exit_code=0)
                with ec.locked(d):
                    c = ec.load(d)
                    with (d / 'synthetic-native-calls.txt').open('a') as calls: calls.write('original-session\n')
                    with (d / 'log.jsonl').open('a') as log:
                        for b in c['batches']:
                            for oid in b['ids']:
                                log.write(json.dumps({'batch': b['id'], 'id': oid, 'status': 'ok',
                                    'model': b['model'], 'executor': 'codex', 'run_id': c['models'][b['model']]['run_id']}) + '\n')
                            b['status'] = 'submitted'
                    ec.save(d, c)
                for value in [{'type': 'thread.started', 'thread_id': 'original-session'}, {'type': 'turn.completed'}]:
                    kw['stdout'].write((json.dumps(value) + '\n').encode())
                kw['stdout'].flush()
            return SimpleNamespace(poll=lambda: 0, wait=lambda: 0)
        supervision.run = lambda cid, model, **kw: original(cid, model, **kw, poll=.001,
            spawn=fake_native, preflight=lambda *a: {'ok': True})
        return ec._main(['recovery-step', '--id', cid, '--event', sys.argv[-1]])
    def spawn(args, **kw):
        if mode == 'crash': os._exit(17)
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--synthetic-observer',
                                 cid, 'step', args[-1]], capture_output=True, text=True, timeout=8)
        if result.returncode: raise AssertionError(result.stderr + '\n' + result.stdout)
        return SimpleNamespace(pid=1)
    manager.tick(cid, spawn=spawn)
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--synthetic-observer':
        raise SystemExit(synthetic_observer(sys.argv[2], sys.argv[3]))
    unittest.main()
