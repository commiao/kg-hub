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
        with r.attempt(directory, kind="submit"):
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

    def tearDown(self):
        self.token.stop(); self.guard.stop(); self.workspace_patch.stop(); self.env.stop(); self.temp.cleanup()

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


if __name__ == "__main__":
    unittest.main()
