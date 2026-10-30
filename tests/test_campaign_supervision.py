"""Supervision adverse cases with synthetic native events and fake HTTP only."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import campaign_supervision as s, extract_campaign as ec, agent_extract as ax, nas_gate
from utils import process_receipts as r
import test_extract_campaign as fixtures


class SubmissionLeaseTests(unittest.TestCase):
    setUp, tearDown, claim = fixtures.CampaignTest.setUp, fixtures.CampaignTest.tearDown, fixtures.CampaignTest.claim

    def prepare(self):
        b = self.claim("DeepSeek-Flash")
        bid = b["batch"]
        d = ec.campaign_dir("t1")
        ids = ec.find_batch(ec.load(d), bid)["ids"]
        (d / "results" / (bid + ".json")).write_text(json.dumps({"items": [fixtures.good_item(i) for i in ids]}))
        return bid

    def test_diagnostic_denial_cannot_stop_live_submit_or_allow_duplicate(self):
        bid = self.prepare()
        entered, finish = threading.Event(), threading.Event()
        @contextlib.contextmanager
        def gate(kind):
            entered.set()
            self.assertTrue(finish.wait(5))
            yield
        with mock.patch.object(ax, "submit_results", return_value=[]) as post, ThreadPoolExecutor(1) as pool:
            first = pool.submit(ec.cmd_submit, "t1", bid, dry_run=True, url="http://fake", token=None, gate=gate)
            try:
                self.assertTrue(entered.wait(5))
                with mock.patch.object(s.subprocess, "run", side_effect=PermissionError("ps unavailable")):
                    self.assertEqual(ec.cmd_operation_status("t1", bid)["action"], "wait_for_original")
                    with self.assertRaises(r.Busy):
                        ec.cmd_submit("t1", bid, dry_run=True, url="http://fake", token=None)
                    with self.assertRaises(r.Busy):
                        ec.cmd_release("t1", bid, reason="must not release live submit")
            finally:
                finish.set()
            first.result(timeout=5)
            self.assertEqual(post.call_count, 1)

    def test_reconciliation_requires_dead_owner_and_complete_absent_service_keys(self):
        bid = self.prepare()
        directory = ec.campaign_dir("t1") / "operations" / bid
        with r.attempt(directory, kind="submit", identity={"campaign": "t1", "batch": bid}):
            with self.assertRaises(r.Busy):
                ec.cmd_reconcile_operation("t1", bid, url="http://fake", token=None,
                                           check=lambda *a: self.fail("live request checked"))
        record = r.read(directory / "latest.json")
        record.update(state="running", may_have_written=True)
        r.write(directory / "latest.json", record)
        with self.assertRaises(ValueError):
            ec.cmd_reconcile_operation("t1", bid, url="http://fake", token=None, check=lambda *a: {})
        self.assertEqual(r.status(directory)["state"], "uncertain")
        present = ec.cmd_reconcile_operation("t1", bid, url="http://fake", token=None,
            check=lambda rows, *a: {row["source_obs_id"]: "ok" for row in rows})
        self.assertFalse(present["confirmed_not_written"])
        self.assertEqual(r.status(directory)["state"], "uncertain")
        absent = ec.cmd_reconcile_operation("t1", bid, url="http://fake", token=None,
            check=lambda rows, *a: {row["source_obs_id"]: None for row in rows})
        self.assertFalse(absent["confirmed_not_written"])
        self.assertTrue(absent["sources_absent"])
        self.assertEqual(r.status(directory)["state"], "uncertain")
        # Separate pre-dispatch fixture: no HTTP writer was ever started.
        record.update(may_have_written=False)
        r.write(directory / "latest.json", record)
        absent = ec.cmd_reconcile_operation("t1", bid, url="http://fake", token=None,
            check=lambda rows, *a: {row["source_obs_id"]: None for row in rows})
        self.assertTrue(absent["confirmed_not_written"])
        self.assertEqual(r.status(directory)["state"], "reconciled_not_written")
        with r.attempt(directory, kind="submit", identity={"campaign":"t1","batch":bid}):
            pass
        self.assertEqual(ec.cmd_operation_status("t1", bid)["operation"]["state"], "completed")

    def test_nas_refusal_and_unknown_http_result_are_not_auto_retried(self):
        bid = self.prepare()
        @contextlib.contextmanager
        def refused(kind):
            raise nas_gate.GateRefused(75, "no temperature")
            yield
        with self.assertRaises(SystemExit):
            ec.cmd_submit("t1", bid, dry_run=True, url="http://fake", token=None, gate=refused)
        self.assertEqual(ec.cmd_operation_status("t1", bid)["action"], "stop_nas_gate_refusal")
        with mock.patch.object(ax, "dryrun_missing", return_value=[]), mock.patch.object(ec, "split_guard"), \
                mock.patch.object(ax, "submit_results", side_effect=ConnectionError("unknown")) as post:
            with self.assertRaises(ConnectionError):
                ec.cmd_submit("t1", bid, dry_run=False, url="http://fake", token=None)
            self.assertEqual(ec.cmd_operation_status("t1", bid)["action"], "reconcile_service_before_replay")
            with self.assertRaises(r.Uncertain):
                ec.cmd_submit("t1", bid, dry_run=False, url="http://fake", token=None)
            self.assertEqual(post.call_count, 1)

    def test_campaign_gate_observes_only_completed_http_calls(self):
        bid = self.prepare()
        response_progress = mock.Mock()
        @contextlib.contextmanager
        def gate(kind):
            yield {'response_progress': response_progress}
        def submit(*args, **kwargs):
            self.assertEqual(kwargs['post']('POST', 'http://fake', {}, None), (200, {}))
            return []
        with mock.patch.object(ax, 'submit_results', side_effect=submit):
            ec.cmd_submit('t1', bid, dry_run=True, url='http://fake', token=None,
                          gate=gate, post=lambda *args: (200, {}))
        response_progress.assert_called_once_with()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.env = mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": str(self.root),
            "KG_HUB_CLAIMS_DIR": str(self.root / "claims"), "KG_HUB_NAS_GATE_DIR": str(self.root / "gate")})
        self.env.start()
        self.workspace_patch = mock.patch.object(ec, "workspace_dir", return_value=self.workspace)
        self.workspace_patch.start()
        self.guard = mock.patch.object(ec, "tracking_guard", return_value={"excluded": True})
        self.guard.start()
        self.token = mock.patch.object(ax, 'api_token', return_value=None)
        self.token.start()
        self.d = ec.campaign_dir("synthetic")
        self.d.mkdir(parents=True)
        self.c = {"tool": ec.TOOL, "id": "synthetic", "executor": "codex", "quota": 2, "chain": "c0", "chains": {},
                  "models": {"m": {"run_id": "r"}}, "batches": [
                      {"id": "b", "chain": "c0", "model": "m", "ids": [1, 2], "status": "claimed"}]}
        ec.save(self.d, self.c)
        self.checks = []

    def tearDown(self):
        self.token.stop(); self.guard.stop(); self.workspace_patch.stop(); self.env.stop(); self.temp.cleanup()

    def absent_check(self, items, url, token):
        self.checks.append([i['source_obs_id'] for i in items])
        return {i['source_obs_id']: None for i in items}

    def formal(self, oid, status="ok", **fields):
        with (self.d / "log.jsonl").open("a") as out:
            out.write(json.dumps({"batch": "b", "id": oid, "status": status, "model": "m", "run_id": "r",
                                  "executor": "codex", **fields}) + "\n")

    def native(self, calls, kinds=None, progress=True):
        def spawn(args, **kw):
            calls.append(args)
            directory = Path(args[args.index("--receipt-dir") + 1])
            with r.attempt(directory, kind="native_cli") as update:
                update(exit_code=0)
                if progress and len(calls) > 1:
                    self.formal(1); self.formal(2)
                kind = kinds[min(len(calls)-1, len(kinds)-1)] if kinds else "diagnostic_unavailable"
                for event in [{"type": "thread.started", "thread_id": "same-session"},
                              {"type": "item.completed", "item": {"type": "agent_message",
                               "text": json.dumps({"recovery_kind": kind})}}, {"type": "turn.completed"}]:
                    kw["stdout"].write((json.dumps(event) + "\n").encode())
                kw["stdout"].flush()
            return mock.Mock(poll=lambda: 0)
        return spawn

    def run_supervisor(self, **kwargs):
        kwargs.setdefault('preflight',lambda *a: {'ok':True})
        kwargs.setdefault('health', lambda *a: True)
        kwargs.setdefault('check', getattr(self, 'absent_check', None) or (lambda items, *a: {i['source_obs_id']: None for i in items}))
        if "adopt_exit" not in kwargs:
            exit_file, log = self.root / "old-exit.json", self.root / "old-events.jsonl"
            exit_file.write_text(json.dumps({"exit_code":0,"ended_at":"2026-10-25T00:00:00Z"}))
            log.write_text(json.dumps({"type":"thread.started","thread_id":"same-session"})+'\n'+
                           json.dumps({"type":"turn.completed"})+'\n')
            kwargs.update(session="same-session",adopt_exit=exit_file,adopt_events=log)
        return s.run("synthetic", "m", **kwargs)

    def test_optional_diagnostic_stop_resumes_same_session_promptly_until_real_quota(self):
        calls = []
        out = self.run_supervisor(poll=0.01, sleep=lambda _: None, spawn=self.native(calls))
        self.assertEqual(out["success"], 2)
        self.assertEqual(len(calls), 2)
        second = calls[1]
        self.assertEqual(second[second.index("resume") + 1], "same-session")
        self.assertIn("--approve-for-me", second)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", second)

    def test_real_approval_rejection_and_native_failure_stop_without_resubmission(self):
        calls = []
        out = self.run_supervisor(poll=0.01, sleep=lambda _: None,
                    spawn=self.native(calls, ["approval_rejected"]))
        self.assertEqual(out["state"], "blocked")
        self.assertEqual(len(calls), 1)

    def test_stalled_turns_are_bounded_without_inventing_progress(self):
        calls = []
        out = self.run_supervisor(poll=0.01, sleep=lambda _: None,
                    spawn=self.native(calls, progress=False), max_stalled=2)
        self.assertEqual(out["reason"], "no_formal_progress")
        self.assertEqual(len(calls), 2)

    def test_existing_live_submit_is_waited_not_restarted(self):
        directory = self.d / "operations/b"
        calls = []
        with r.attempt(directory, kind="submit") as update:
            update(phase="waiting_nas")
            def wait(_):
                self.assertFalse(calls)
                raise InterruptedError("end synthetic wait")
            with self.assertRaises(InterruptedError):
                self.run_supervisor(poll=0.01, sleep=wait, spawn=self.native(calls))
        self.assertFalse(calls)

    def test_legacy_worker_is_not_launched_again_before_its_terminal_receipt(self):
        exit_file, log = self.root / "exit.json", self.root / "events.jsonl"
        calls = []
        def wait(_):
            self.assertFalse(calls)
            raise InterruptedError("end synthetic wait")
        with self.assertRaises(InterruptedError):
            s.run("synthetic", "m", session="legacy", adopt_exit=exit_file, adopt_events=log,
                  poll=0.01, sleep=wait, spawn=self.native(calls))
        self.assertFalse(calls)

    def test_skips_released_wrong_identity_and_unconfirmed_empty_never_fill_quota(self):
        self.formal(1, "skipped")
        self.formal(2, "empty")
        self.formal(1, model="other")
        self.assertEqual(s.completed(self.c, self.d, "m"), 0)
        self.formal(1); self.formal(2, "empty", http=200, empty_recorded=True)
        self.assertEqual(s.completed(self.c, self.d, "m"), 2)
        self.c["batches"][0]["released_ids"] = [1]
        self.assertEqual(s.completed(self.c, self.d, "m"), 1)

    def test_counts_ignore_partial_tail_and_read_corrections_next_time(self):
        self.formal(1)
        p = self.d / 'log.jsonl'
        partial = json.dumps({'batch': 'b', 'id': 1, 'status': 'error', 'model': 'm',
                              'run_id': 'r', 'executor': 'codex'})
        with p.open('a') as out:
            out.write(partial)
        self.assertEqual(s.completed_counts(self.c, self.d)[('m', None)], 1)
        with p.open('a') as out:
            out.write('\n')
        self.assertEqual(s.completed_counts(self.c, self.d).get(('m', None), 0), 0)
        with p.open('a') as out:
            out.write('invalid complete record\n')
        with self.assertRaises(ValueError):
            s.completed_counts(self.c, self.d)

    def test_real_fake_executable_resumes_same_session_with_frozen_settings(self):
        binary, code = self.root / "bin", self.root / "code"
        binary.mkdir(); (code / "tools").mkdir(parents=True)
        entry = code / "tools/extract"
        entry.write_text("#!" + sys.executable + "\nimport sys\n"
            "sys.path.insert(0," + repr(str(Path(ec.__file__).resolve().parent.parent)) + ")\n"
            "from tools.extract_cli import main\nraise SystemExit(main())\n")
        entry.chmod(0o700)
        worker = binary / "codex"
        worker.write_text("#!" + sys.executable + "\nimport sys,os,json,pathlib\n"
            "args=sys.argv[1:];assert '--approve-for-me' in args\n"
            "assert 'model_reasoning_effort=high' in args\n"
            "root=pathlib.Path(os.environ['KG_HUB_AGENT_EXTRACT_DIR']);p=root/'calls.json'\n"
            "calls=json.loads(p.read_text()) if p.exists() else [];calls.append(args);p.write_text(json.dumps(calls))\n"
            "if len(calls)>1:\n"
            " assert args[args.index('resume')+1]=='same-session'\n"
            " with (root/'campaigns/synthetic/log.jsonl').open('a') as f:\n"
            "  for i in [1,2]: f.write(json.dumps(dict(id=i,batch='b',model='m',run_id='r',executor='codex',status='ok'))+'\\n')\n"
            "print(json.dumps(dict(type='thread.started',thread_id='same-session')))\n"
            "print(json.dumps(dict(type='item.completed',item=dict(type='agent_message',text=json.dumps(dict(recovery_kind='diagnostic_unavailable'))))))\n"
            "print(json.dumps(dict(type='turn.completed')))\n")
        worker.chmod(0o700)
        self.c['start_contract'] = {'effort':'high','proxy_mode':'none','model_preflight':{'m':{'executor':'codex'}}}
        ec.save(self.d, self.c)
        with mock.patch.dict(os.environ, {'PATH':str(binary)+os.pathsep+os.environ['PATH'],'KG_HUB_CODE':str(code)}):
            out = self.run_supervisor(poll=0.01)
        self.assertEqual(out['state'], 'complete')
        self.assertEqual(out['success'], 2)
        self.assertEqual(len(json.loads((self.root/'calls.json').read_text())), 2)

    def test_existing_unmanaged_worker_requires_terminal_adoption_proof(self):
        calls = []
        with self.assertRaisesRegex(ValueError, 'requires its unique exit/event receipts'):
            s.run('synthetic','m',poll=0.01,spawn=self.native(calls))
        self.assertFalse(calls)

    def test_legacy_native_exit_zero_does_not_override_business_gate_exit76(self):
        exit_file, log = self.root / 'old-exit.json', self.root / 'old-events.jsonl'
        exit_file.write_text(json.dumps({'exit_code':0,'ended_at':'2026-10-25T00:00:00Z'}))
        rows = [{'type':'thread.started','thread_id':'same-session'},
                {'type':'item.completed','item':{'type':'command_execution','exit_code':76,
                 'command':'/stable/tools/extract campaign submit --id synthetic --batch b'}},
                {'type':'turn.completed'}]
        log.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        calls = []
        out = self.run_supervisor(session='same-session',adopt_exit=exit_file,adopt_events=log,
                                  poll=0.01,spawn=self.native(calls))
        self.assertEqual(out['reason'],'nas_gate_refused')
        self.assertEqual(out['business_exit_code'],76)
        self.assertFalse(calls)

    def test_partial_log_tail_does_not_count_as_success_or_stop_supervision(self):
        self.formal(1)
        with (self.d/'log.jsonl').open('a') as out: out.write('{"unfinished":')
        self.assertEqual(s.completed(self.c,self.d,'m'),1)

    def test_preflight_failure_spends_no_native_call(self):
        calls = []
        out = self.run_supervisor(poll=0.01,spawn=self.native(calls),preflight=lambda *a:{'ok':False})
        self.assertEqual(out['reason'],'preflight_failed')
        self.assertFalse(calls)

    def test_second_supervisor_cannot_duplicate_an_owned_worker(self):
        calls = []
        with r.attempt(self.d/'supervisors/m',kind='supervisor'):
            with self.assertRaises(r.Busy):
                self.run_supervisor(poll=0.01,spawn=self.native(calls))
        self.assertFalse(calls)

    def test_unknown_native_terminal_is_never_relaunched(self):
        directory = self.d/'supervisors/m/native'
        with r.attempt(directory,kind='native_cli'): pass
        record = r.read(directory/'latest.json');record['state']='running'
        r.write(directory/'latest.json',record)
        calls = []
        out = self.run_supervisor(poll=0.01,spawn=self.native(calls))
        self.assertEqual(out['reason'],'native_terminal_unverified')
        self.assertFalse(calls)

    def capacity_spawn(self, calls, *, always=False, extra_error=None, unsafe_submit=False, failures=1):
        normal = self.native([])
        def spawn(args, **kw):
            calls.append(args)
            if len(calls) > failures and not always:
                self.formal(1); self.formal(2)
                return normal(args, **kw)
            directory = Path(args[args.index('--receipt-dir') + 1])
            with r.attempt(directory, kind='native_cli') as update:
                update(exit_code=1)
                rows = [{'type': 'thread.started', 'thread_id': 'same-session'},
                        {'type': 'error', 'message': s.CAPACITY_MESSAGE},
                        {'type': 'turn.failed', 'error': {'message': s.CAPACITY_MESSAGE}}]
                if extra_error:
                    rows.insert(-1, {'type': 'error', 'message': extra_error})
                if unsafe_submit:
                    with r.attempt(self.d / 'operations/b', kind='submit') as progress:
                        progress(may_have_written=True, requires_reconciliation=True)
                for row in rows:
                    kw['stdout'].write((json.dumps(row) + '\n').encode())
                kw['stdout'].flush()
            return mock.Mock(poll=lambda: 0)
        return spawn

    def test_exact_capacity_refusal_waits_then_resumes_same_session_without_extra_claim(self):
        calls, sleeps = [], []
        before = ec.load(self.d)['batches']
        with mock.patch.object(s, 'CAPACITY_WAIT_SECONDS', .02):
            out = self.run_supervisor(poll=.01, sleep=sleeps.append, spawn=self.capacity_spawn(calls))
        self.assertEqual(out['state'], 'complete')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][calls[1].index('resume') + 1], 'same-session')
        self.assertEqual(ec.load(self.d)['batches'], before)
        self.assertTrue(sleeps)
        states = list((self.d / 'supervisors/m/native').glob('*.json'))
        self.assertTrue(any(r.read(p).get('exit_code') == 1 for p in states))

    def test_persistent_capacity_is_bounded_and_does_not_invent_formal_progress(self):
        calls = []
        with mock.patch.object(s, 'CAPACITY_WAIT_SECONDS', .01):
            out = self.run_supervisor(poll=.01, sleep=lambda _: None,
                                      spawn=self.capacity_spawn(calls, always=True))
        self.assertEqual(out['reason'], 'model_capacity_exhausted')
        self.assertEqual(len(calls), 1 + s.MAX_CAPACITY_RETRIES)
        self.assertEqual(s.completed(self.c, self.d, 'm'), 0)

    def test_capacity_retry_budget_and_backoff_follow_frozen_campaign_policy(self):
        c = ec.load(self.d)
        c['recovery_policy'] = {'version': s.RECOVERY_POLICY_VERSION, 'capacity_wait_seconds': 1,
                                'capacity_max_wait_seconds': 3, 'capacity_max_retries': 2}
        ec.save(self.d, c)
        calls, sleeps = [], []
        out = self.run_supervisor(poll=10, sleep=sleeps.append, spawn=self.capacity_spawn(calls, always=True))
        self.assertEqual(out['reason'], 'model_capacity_exhausted')
        self.assertEqual(len(calls), 1 + 2)          # 战役策略 2 次，不是模块缺省
        self.assertEqual([x for x in sleeps if x in (1, 2)], [1, 2])   # 1 秒、翻倍到 2 秒，再退就超预算
        self.assertEqual(r.read(self.d / 'supervisors/m/worker.json')['capacity_retries'], 2)

    def test_capacity_policy_defaults_bounds_and_doubling(self):
        policy = s.capacity_policy({})
        self.assertEqual(policy, {'capacity_wait_seconds': 300, 'capacity_max_wait_seconds': 3600,
                                  'capacity_max_retries': 6})
        self.assertEqual([s.capacity_wait(policy, n) for n in range(1, 8)],
                         [300, 600, 1200, 2400, 3600, 3600, 3600])
        self.assertEqual(s.capacity_wait({**policy, 'capacity_wait_seconds': 0}, 3), 0)   # 测试把等待打到 0
        for bad in [{'version': 'other'}, {'version': s.RECOVERY_POLICY_VERSION, 'nope': 1},
                    {'version': s.RECOVERY_POLICY_VERSION, 'capacity_max_retries': 0},
                    {'version': s.RECOVERY_POLICY_VERSION, 'capacity_max_retries': True},
                    {'version': s.RECOVERY_POLICY_VERSION, 'capacity_wait_seconds': 10, 'capacity_max_wait_seconds': 5}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                s.capacity_policy({'recovery_policy': bad})

    def test_capacity_does_not_clear_unknown_write(self):
        calls = []
        out = self.run_supervisor(poll=.01, sleep=lambda _: None,
                                  spawn=self.capacity_spawn(calls, unsafe_submit=True))
        self.assertEqual(out['state'], 'blocked')
        self.assertEqual(len(calls), 1)

    def test_capacity_does_not_override_unrelated_provider_error(self):
        # An unrelated provider error next to the capacity text is not a capacity
        # refusal: no capacity retry is spent. It is class C (unknown): one bounded
        # retry under the frozen policy, then the coordinator with the raw terminal.
        c = ec.load(self.d)
        c['recovery_policy'] = {'version': s.RECOVERY_POLICY_VERSION, 'unknown_backoff_seconds': [1]}
        ec.save(self.d, c)
        calls = []
        out = self.run_supervisor(poll=.01, sleep=lambda _: None,
                                  spawn=self.capacity_spawn(calls, always=True, extra_error='authentication failed'))
        self.assertEqual(out['state'], 'blocked')
        self.assertEqual(out['reason'], 'native_worker_blocked')
        self.assertEqual(len(calls), 2)
        self.assertEqual(r.read(self.d / 'supervisors/m/worker.json')['capacity_retries'], 0)
        self.assertIn('authentication failed', json.dumps(out['detail']['native_errors']))
        self.assertEqual(out['detail']['events_terminal'], 'turn.failed')

    def test_capacity_after_real_cli_reconnect_notice_resumes_same_session(self):
        calls=[]
        notice='Reconnecting... 2/5 (stream disconnected before completion: WebSocket protocol error: Connection reset without closing handshake)'
        with mock.patch.object(s,'CAPACITY_WAIT_SECONDS',.01):
            out=self.run_supervisor(poll=.01,sleep=lambda _:None,
                spawn=self.capacity_spawn(calls,extra_error=notice))
        self.assertEqual(out['state'],'complete')
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[1][calls[1].index('resume')+1],'same-session')

    def test_reconnect_prefix_never_hides_auth_or_noncapacity_terminal(self):
        p=self.root/'reconnect.events'
        for earlier,terminal in [
            ('Reconnecting... 2/5 (authentication failed)',s.CAPACITY_MESSAGE),
            ('Reconnecting... 2/5 (stream disconnected before completion: WebSocket protocol error: Connection reset without closing handshake)','authentication failed')]:
            rows=[{'type':'error','message':earlier},{'type':'turn.failed','error':{'message':terminal}}]
            p.write_text(''.join(json.dumps(x)+'\n' for x in rows))
            self.assertFalse(s.events(p)['capacity_refused'])

    def test_capacity_never_overrides_recorded_approval_denial(self):
        p = self.root / 'denied.events'
        rows = [{'type': 'error', 'message': s.CAPACITY_MESSAGE},
                {'type': 'item.completed', 'item': {'type': 'command_execution', 'exit_code': 1,
                 'aggregated_output': 'This action was rejected due to unacceptable risk.'}},
                {'type': 'turn.failed', 'error': {'message': s.CAPACITY_MESSAGE}}]
        p.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.assertFalse(s.capacity_terminal(s.events(p), {'state': 'completed', 'exit_code': 1, 'ended_at': 'now'}))

    def test_capacity_text_in_agent_reply_is_not_a_native_refusal(self):
        p = self.root / 'quoted.events'
        p.write_text(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
            'text': s.CAPACITY_MESSAGE}}) + '\n' + json.dumps({'type': 'turn.failed'}) + '\n')
        self.assertFalse(s.events(p).get('capacity_refused'))


class ObjectiveClassificationTests(SupervisorTests):
    """Fault injection for the 2026-10-29 recovery redesign (plan §3.1/3.2, acceptance §5).

    Class A (infrastructure/capacity) and class C (unknown) must never produce
    coordinator_required within budget and must never write twice; class B keeps
    stopping exactly as before.
    """
    SUBMIT = '/stable/tools/extract campaign submit --id synthetic --batch b'

    def setUp(self):
        super().setUp()
        (self.d / "batches").mkdir(exist_ok=True)
        (self.d / "batches/b.json").write_text(json.dumps({"items": [
            {"id": 1, "source_description": "s", "source_obs_id": "o1"},
            {"id": 2, "source_description": "s", "source_obs_id": "o2"}]}))
        self.submits = mock.patch.object(ax, 'submit_results', side_effect=AssertionError('graph write attempted'))
        self.submits.start()

    def tearDown(self):
        self.submits.stop()
        super().tearDown()

    def failing(self, calls, rows, *, exit_code=1, failures=1, stderr=b'', before=None):
        normal = self.native([])
        def spawn(args, **kw):
            calls.append(args)
            if len(calls) > failures:
                self.formal(1); self.formal(2)
                return normal(args, **kw)
            directory = Path(args[args.index('--receipt-dir') + 1])
            with r.attempt(directory, kind='native_cli') as update:
                update(exit_code=exit_code)
                if before: before()
                for row in [{'type': 'thread.started', 'thread_id': 'same-session'}, *rows]:
                    kw['stdout'].write((json.dumps(row) + '\n').encode())
                kw['stdout'].flush()
                kw['stderr'].write(stderr); kw['stderr'].flush()
            return mock.Mock(poll=lambda: 0)
        return spawn

    def tracking_sleep(self, sleeps, phases, clock=None):
        """A receipt only keeps its last phase; observe intermediate phases while the supervisor sleeps."""
        def sleep(x):
            sleeps.append(x)
            phases.add(r.read(self.d / 'supervisors/m/latest.json').get('phase'))
            if clock is not None:
                clock[0] += x
        return sleep

    def reset(self):
        self.tearDown(); self.setUp()

    def gate_timeout(self):
        """What cmd_submit leaves behind when the NAS gate refuses with 76 before any write."""
        try:
            with r.attempt(self.d / 'operations/b', kind='submit', identity={'campaign': 'synthetic', 'batch': 'b'}) as update:
                update(phase='waiting_nas', may_have_written=False,
                       source_keys=[{'source_description': 's', 'source_obs_id': 'o1'},
                                    {'source_description': 's', 'source_obs_id': 'o2'}])
                raise SystemExit(76)
        except SystemExit:
            pass

    def assert_automatic(self, out, calls, expected_calls):
        self.assertEqual(out['state'], 'complete')
        self.assertEqual(len(calls), expected_calls)
        for call in calls[1:]:
            self.assertEqual(call[call.index('resume') + 1], 'same-session')
        self.assertEqual(r.read(self.d / 'supervisors/m/latest.json').get('phase'), 'quota_complete')
        self.assertEqual(s.completed(ec.load(self.d), self.d, 'm'), 2)
        worker = r.read(self.d / 'supervisors/m/worker.json')
        self.assertEqual((worker['infra_retries'], worker['unknown_retries'], worker['infra_streak_started_at']), (0, 0, None))

    def test_five_consecutive_capacity_refusals_resume_without_coordinator(self):
        calls, sleeps, phases = [], [], set()
        out = self.run_supervisor(poll=10, sleep=self.tracking_sleep(sleeps, phases), spawn=self.capacity_spawn(calls, failures=5))
        self.assert_automatic(out, calls, 6)
        self.assertGreaterEqual(sum(sleeps), 300 + 600 + 1200 + 2400 + 3600)
        self.assertIn('waiting_model_capacity', phases)
        self.assertNotIn('coordinator_required', phases)

    def test_persistent_http502_backs_off_then_resumes_within_window(self):
        rows = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'exit_code': 1,
                 'command': '/stable/tools/extract campaign prepare --id synthetic --batch b',
                 'aggregated_output': 'urllib.error.HTTPError: HTTP Error 502: Bad Gateway'}},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps({'recovery_kind': 'infrastructure_failed'})}},
                {'type': 'turn.completed'}]
        calls, sleeps, phases, now = [], [], set(), [0.0]
        out = self.run_supervisor(poll=10, sleep=self.tracking_sleep(sleeps, phases, now), clock=lambda: now[0],
                                  spawn=self.failing(calls, rows, exit_code=0, failures=5))
        self.assert_automatic(out, calls, 6)
        self.assertGreaterEqual(sum(sleeps), 300 + 600 + 1200 + 2400 + 3600)
        self.assertLess(now[0], s.INFRA_MAX_WINDOW_SECONDS)
        self.assertIn('waiting_infrastructure', phases)
        self.assertNotIn('coordinator_required', phases)
        self.assertFalse(self.checks)   # nothing failed-before-write to read back; no graph write either way

    def test_infrastructure_window_is_bounded_and_blocks_with_raw_terminal(self):
        c = ec.load(self.d)
        c['recovery_policy'] = {'version': s.RECOVERY_POLICY_VERSION, 'infra_backoff_seconds': [100, 200],
                                'infra_max_window_seconds': 450}
        ec.save(self.d, c)
        rows = [{'type': 'error', 'message': 'ReadTimeout: HTTPSConnectionPool read timed out'},
                {'type': 'turn.failed', 'error': {'message': 'stream disconnected before completion'}}]
        calls, sleeps, phases, now = [], [], set(), [0.0]
        out = self.run_supervisor(poll=10, sleep=self.tracking_sleep(sleeps, phases, now), clock=lambda: now[0],
                                  spawn=self.failing(calls, rows, failures=9, stderr=b'codex: transport failure trace'))
        self.assertEqual((out['state'], out['reason']), ('blocked', 'infrastructure_backoff_exhausted'))
        self.assertEqual(len(calls), 3)          # 100 + 200 spent; a third 200 would pass the 450-second window
        self.assertIn('transport failure trace', out['detail']['stderr_tail'])
        self.assertEqual(out['detail']['infra_signals'][0]['kind'], 'read_timeout')
        self.assertEqual(s.completed(ec.load(self.d), self.d, 'm'), 0)

    def test_nas_gate_timeout_exit76_is_reconciled_read_only_then_resumed(self):
        rows = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'exit_code': 76, 'command': self.SUBMIT,
                 'aggregated_output': 'NAS FIFO 队列超过 3600 秒无进展'}},
                {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps({'recovery_kind': 'nas_refused'})}},
                {'type': 'turn.completed'}]
        calls = []
        out = self.run_supervisor(poll=.01, sleep=lambda _: None, spawn=self.failing(calls, rows, exit_code=0, before=self.gate_timeout))
        self.assert_automatic(out, calls, 2)
        self.assertEqual(self.checks, [['o1', 'o2']])          # one read-only service read-back
        op = r.status(self.d / 'operations/b')
        self.assertEqual((op['state'], op['exit_code'], op['may_have_written']), ('reconciled_not_written', 76, False))

    def test_nas_gate_timeout_with_present_source_is_not_retried(self):
        rows = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'exit_code': 76, 'command': self.SUBMIT}},
                {'type': 'turn.completed'}]
        calls = []
        out = self.run_supervisor(poll=.01, sleep=lambda _: None, check=lambda items, *a: {i['source_obs_id']: 'ok' for i in items},
                                  spawn=self.failing(calls, rows, exit_code=0, before=self.gate_timeout))
        self.assertEqual((out['state'], out['reason'], out['business_exit_code'] if 'business_exit_code' in out else 76),
                         ('blocked', 'nas_gate_refused', 76))
        self.assertEqual(len(calls), 1)
        self.assertEqual(out['reconciliation']['batches']['b']['state'], 'remote_unverified')

    def test_websocket_reset_resumes_same_session(self):
        notice = 'Reconnecting... 3/5 (stream disconnected before completion: WebSocket protocol error: Connection reset without closing handshake)'
        rows = [{'type': 'error', 'message': notice},
                {'type': 'turn.failed', 'error': {'message': 'stream disconnected before completion: WebSocket protocol error: Connection reset without closing handshake'}}]
        calls, sleeps, phases = [], [], set()
        out = self.run_supervisor(poll=.01, sleep=self.tracking_sleep(sleeps, phases), spawn=self.failing(calls, rows))
        self.assert_automatic(out, calls, 2)
        latest = r.read(self.d / 'supervisors/m/latest.json')
        self.assertEqual((latest.get('terminal_class'), latest.get('terminal_signal')), ('clean', None))   # final turn
        self.assertIn('waiting_infrastructure', phases)

    def test_unknown_exit3_backs_off_then_resumes_and_is_bounded(self):
        rows = [{'type': 'turn.failed', 'error': {'message': 'something nobody has seen before'}}]
        calls, sleeps, phases = [], [], set()
        out = self.run_supervisor(poll=10, sleep=self.tracking_sleep(sleeps, phases), spawn=self.failing(calls, rows, exit_code=3))
        self.assert_automatic(out, calls, 2)
        self.assertGreaterEqual(sum(sleeps), 600)
        self.assertIn('waiting_unknown_terminal', phases)
        # Budget: default two unknown retries, then the coordinator with the raw terminal.
        self.reset()
        calls = []
        out = self.run_supervisor(poll=10, sleep=lambda _: None,
                                  spawn=self.failing(calls, rows, exit_code=3, failures=9, stderr=b'Traceback: weird'))
        self.assertEqual((out['state'], out['reason']), ('blocked', 'native_worker_blocked'))
        self.assertEqual(len(calls), 3)
        self.assertEqual((out['detail']['exit_code'], out['detail']['events_terminal']), (3, 'turn.failed'))
        self.assertIn('weird', out['detail']['stderr_tail'])
        self.assertEqual(out['detail']['unknown_retries'], 2)

    def test_unknown_terminal_with_unreachable_nas_waits_for_health(self):
        rows = [{'type': 'turn.failed', 'error': {'message': 'exit without explanation'}}]
        probes = []
        def health(*a):
            probes.append(1)
            return len(probes) > 3
        calls, sleeps, phases = [], [], set()
        out = self.run_supervisor(poll=10, sleep=self.tracking_sleep(sleeps, phases), health=health,
                                  spawn=self.failing(calls, rows, exit_code=2))
        self.assert_automatic(out, calls, 2)
        self.assertIn('waiting_nas_health', phases)
        self.assertEqual(r.read(self.d / 'supervisors/m/latest.json').get('unknown_retries'), 0)
        self.assertGreaterEqual(len(probes), 4)

    def test_nas_fails_after_unknown_classification_has_a_durable_wait_limit(self):
        c = ec.load(self.d)
        c['recovery_policy'] = {'version': s.RECOVERY_POLICY_VERSION,
            'unknown_backoff_seconds': [1], 'infra_backoff_seconds': [1],
            'infra_max_window_seconds': 3}
        ec.save(self.d, c)
        now, calls, probes = [100.0], [], []
        def health(*args):
            probes.append(1)
            return len(probes) == 1  # healthy classification, outage after backoff
        def sleep(seconds):
            now[0] += seconds
            if now[0] > 110:
                self.fail('health wait exceeded its bounded window')
        rows = [{'type': 'turn.failed', 'error': {'message': 'unclassified failure'}}]
        out = self.run_supervisor(poll=1, sleep=sleep, clock=lambda: now[0], health=health,
                                  spawn=self.failing(calls, rows, exit_code=3))
        self.assertEqual(out['reason'], 'infrastructure_backoff_exhausted')
        self.assertEqual(len(calls), 1)
        worker = r.read(self.d / 'supervisors/m/worker.json')
        self.assertIsNotNone(worker['infra_streak_started_at'])
        self.assertTrue(s.backoff_exhausted(worker, s.recovery_policy(c), 'infrastructure', clock=lambda: now[0]))

    def test_timeout_does_not_mask_business_refusal(self):
        for kind in ('approval_rejected', 'business_permission_denied', 'quality_process_violation', 'uncertain_submit'):
            self.assertEqual(s.classify_terminal({'hard_stop': 76, 'recovery_kind': kind}, {})['class'], 'business')
        p = self.root / 'mixed-gates.events'
        p.write_text(''.join(json.dumps({'type': 'item.completed', 'item': {
            'type': 'command_execution', 'command': self.SUBMIT, 'exit_code': code}}) + '\n' for code in (75, 76)))
        self.assertEqual(s.events(p)['hard_stop'], 75)

    def test_business_stops_still_block_at_once(self):
        for kind in ('business_permission_denied', 'quality_process_violation', 'uncertain_submit', 'approval_rejected'):
            with self.subTest(kind=kind):
                self.reset()
                calls = []
                out = self.run_supervisor(poll=.01, sleep=lambda _: None, spawn=self.native(calls, [kind]))
                self.assertEqual((out['state'], out['reason']), ('blocked', 'native_worker_blocked'))
                self.assertEqual(len(calls), 1)
                self.assertEqual(out['detail']['recovery_kind'], kind)
        self.reset()
        rows = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'exit_code': 75, 'command': self.SUBMIT}},
                {'type': 'turn.completed'}]
        calls = []
        out = self.run_supervisor(poll=.01, sleep=lambda _: None, spawn=self.failing(calls, rows, exit_code=0))
        self.assertEqual((out['state'], out['reason'], out['business_exit_code']), ('blocked', 'nas_gate_refused', 75))
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.checks)

    def test_transport_text_inside_agent_prose_is_not_an_infrastructure_signal(self):
        p = self.root / 'prose.events'
        rows = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'the body mentions HTTP Error 502 and a WebSocket'}},
                {'type': 'item.completed', 'item': {'type': 'command_execution', 'exit_code': 0,
                 'command': self.SUBMIT, 'aggregated_output': 'ReadTimeout quoted in a successful command'}},
                {'type': 'item.completed', 'item': {'type': 'command_execution', 'exit_code': 1,
                 'command': 'cat observation.md', 'aggregated_output': 'HTTP Error 503 in an observation body'}},
                {'type': 'turn.completed'}]
        p.write_text(''.join(json.dumps(x) + '\n' for x in rows))
        self.assertNotIn('infra_signals', s.events(p))
        self.assertEqual(s.classify_terminal(s.events(p), {'state': 'completed', 'exit_code': 2, 'ended_at': 't'})['class'], 'unknown')

    def test_recovery_policy_backoff_keys_are_validated(self):
        policy = s.recovery_policy({})
        self.assertEqual((policy['infra_backoff_seconds'], policy['infra_max_window_seconds'], policy['unknown_backoff_seconds']),
                         ([300, 600, 1200, 2400, 3600], 21600, [600, 1800]))
        self.assertEqual([s.backoff_wait(policy['infra_backoff_seconds'], n) for n in (1, 5, 6, 9)], [300, 3600, 3600, 3600])
        for bad in [{'infra_backoff_seconds': []}, {'infra_backoff_seconds': [0]}, {'infra_backoff_seconds': 300},
                    {'unknown_backoff_seconds': [1.5]}, {'infra_max_window_seconds': 10, 'infra_backoff_seconds': [300]},
                    {'infra_max_window_seconds': 0}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                s.recovery_policy({'recovery_policy': {'version': s.RECOVERY_POLICY_VERSION, **bad}})
        self.assertEqual(s.capacity_policy({'recovery_policy': {'version': s.RECOVERY_POLICY_VERSION, 'unknown_backoff_seconds': [5]}}),
                         {'capacity_wait_seconds': 300, 'capacity_max_wait_seconds': 3600, 'capacity_max_retries': 6})


if __name__ == "__main__":
    unittest.main()
