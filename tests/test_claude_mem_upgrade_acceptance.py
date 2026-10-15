"""Upgrade checks are read-only; notifications and all state use isolated fixtures."""
import contextlib
import copy
import fcntl
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import claude_mem_upgrade_acceptance as A

ROOT = Path(__file__).resolve().parents[1]


def receipt(**updates):
    data = {"ok": True, "mode": "dry-run", "production_state_used": False,
            "messages": [{"simulated": True, "delivered": False}] * 2,
            "duplicate_fault_suppressed": True, "duplicate_recovery_suppressed": True,
            "final_pending": {}}
    data.update(updates)
    return data


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.code = self.home / "code"
        self.code.mkdir()
        for name in ("drill_codex_hooks.py", "codex_hook_drill_runner.py", "claude_mem_upgrade_acceptance.py"):
            (self.code / name).write_text("runner source")
        self.state_dir = self.home / ".kg-hub/state/claude-mem-upgrade-acceptance"
        self.prod = self.home / ".kg-hub/state/watchdog.json"
        self.prod.parent.mkdir(parents=True)
        self.prod.write_text('{"pending_alerts":{"real":{"severity":"fire"}}}')
        self.source = self.home / "installed/hooks/codex-hooks.json"
        self.source.parent.mkdir(parents=True)
        self.source.write_text('{"version":"1"}')
        self.hook = {"key": "claude-mem@local:post_compact:0:0", "currentHash": "sha256:one",
                     "trustStatus": "trusted", "sourcePath": str(self.source), "enabled": True}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.native = self.stack.enter_context(patch.object(A, "read_hooks", side_effect=lambda home: {"hooks": [copy.deepcopy(self.hook)]}))
        self.drill = self.stack.enter_context(patch.object(A, "run_default_drill", return_value={"ok": True, "reason": None, "receipt": "isolated/receipt.json"}))
        self.notify = self.stack.enter_context(patch.object(A, "notify_failure", return_value=True))
        self.before = self.prod.read_bytes()

    def run_check(self, **kw):
        rc, state = A.accept(self.home, self.code, self.state_dir, **kw)
        self.assertEqual(self.prod.read_bytes(), self.before)
        self.assertEqual(self.source.read_text(), '{"version":"1"}')
        return rc, state

    def load(self):
        return json.loads((self.state_dir / "acceptance.json").read_text())

    def test_first_run_and_changes_are_accepted_unchanged_is_silent(self):
        self.assertEqual(self.run_check()[0], 0)
        self.assertEqual(self.run_check()[1]["status"], "unchanged")
        self.assertEqual(self.drill.call_count, 1)
        self.hook.update(currentHash="sha256:two", sourcePath=str(self.source))
        self.assertEqual(self.run_check()[0], 0)
        self.assertEqual(self.drill.call_count, 2)
        self.notify.assert_not_called()

    def test_worker_release_and_drill_update_trigger_acceptance(self):
        self.run_check()
        release = self.home / ".local/share/claude-mem/current/RELEASE.json"
        release.parent.mkdir(parents=True)
        release.write_text('{"commit":"new-release"}')
        self.run_check()
        (self.code / "codex_hook_drill_runner.py").write_text("upgraded runner")
        self.run_check()
        self.assertEqual(self.drill.call_count, 3)

    def test_force_never_uses_live_but_rechecks(self):
        self.run_check()
        self.run_check(force=True)
        self.assertEqual(self.drill.call_count, 2)
        self.assertEqual(self.drill.call_args.args, (self.code, self.state_dir / "runs"))
        self.notify.assert_not_called()

    def test_untrusted_modified_disabled_or_missing_fails(self):
        for status, enabled in (("untrusted", True), ("modified", True), ("trusted", False)):
            self.hook.update(trustStatus=status, enabled=enabled)
            rc, state = self.run_check()
            self.assertEqual(rc, 1)
            self.assertIn("native-hook-trust-unhealthy", state["reasons"])
            self.assertEqual(state["status"], "failed")
        self.native.side_effect = lambda home: {"hooks": []}
        rc, state = self.run_check()
        self.assertEqual(rc, 1)
        self.assertIn("claude-mem-hooks-missing", state["reasons"])
        self.assertEqual(self.drill.call_count, 4)

    def test_failure_pending_retries_and_dedup_survives_next_run(self):
        self.drill.return_value = {"ok": False, "reason": "drill-receipt-missing"}
        self.notify.side_effect = [False, True]
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.load()["pending_alert"]["attempts"], 1)
        self.assertEqual(self.run_check()[0], 1)
        self.assertIsNone(self.load()["pending_alert"])
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(self.drill.call_count, 3)
        self.assertEqual((self.state_dir / "acceptance.json").stat().st_mode & 0o777, 0o600)

    def test_pending_is_on_disk_before_notification(self):
        self.drill.return_value = {"ok": False, "reason": "drill-timeout"}
        def notified(home, message):
            checkpoint = self.load()
            self.assertEqual(checkpoint["pending_alert"]["attempts"], 1)
            self.assertEqual(checkpoint["status"], "failed")
            return True
        self.notify.side_effect = notified
        self.run_check()
        self.assertIsNone(self.load()["pending_alert"])

    def test_success_discards_obsolete_pending_and_rearms_next_incident(self):
        self.drill.return_value = {"ok": False, "reason": "drill-timeout"}
        self.notify.return_value = False
        self.run_check()
        self.drill.return_value = {"ok": True, "reason": None}
        self.assertEqual(self.run_check()[0], 0)
        self.assertIsNone(self.load()["pending_alert"])
        self.notify.assert_called_once()
        self.drill.return_value = {"ok": False, "reason": "drill-timeout"}
        self.run_check(force=True)
        self.assertEqual(self.notify.call_count, 2)

    def test_new_fingerprint_failure_supersedes_old_pending(self):
        self.drill.return_value = {"ok": False, "reason": "drill-timeout"}
        self.notify.return_value = False
        self.run_check()
        old = self.load()["fingerprint"]
        self.hook["currentHash"] = "sha256:new"
        self.run_check()
        state = self.load()
        self.assertNotEqual(old, state["pending_alert"]["fingerprint"])
        self.assertEqual(state["pending_alert"]["attempts"], 1)

    def test_native_inspection_failure_is_not_cached_as_success(self):
        self.native.side_effect = OSError("secret https://webhook/token")
        rc, state = self.run_check()
        self.assertEqual(rc, 1)
        self.assertIn("native-inspection-OSError", state["reasons"])
        self.assertNotIn("secret", json.dumps(state))

    def test_lock_prevents_concurrent_drills(self):
        self.state_dir.mkdir()
        with (self.state_dir / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc, state = self.run_check()
        self.assertEqual((rc, state["status"]), (75, "busy"))
        self.drill.assert_not_called()
        self.notify.assert_not_called()

    def test_corrupt_state_preserved_and_controller_error_notified(self):
        self.state_dir.mkdir()
        path = self.state_dir / "acceptance.json"
        path.write_text("corrupt evidence")
        with patch.object(A.Path, "home", return_value=self.home), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(A.main(["--state-dir", str(self.state_dir)]), 2)
        self.assertEqual(path.read_text(), "corrupt evidence")
        self.notify.assert_called_once()


class DefaultDrill(unittest.TestCase):
    def test_subprocess_uses_default_and_rejects_bad_receipts(self):
        for update in ({}, {"mode": "live"}, {"production_state_used": True},
                       {"messages": [{"delivered": True, "simulated": False}]},
                       {"duplicate_fault_suppressed": False}, {"final_pending": {"bad": {}}}):
            with self.subTest(update=update), tempfile.TemporaryDirectory() as td:
                runs = Path(td) / "runs"
                def child(argv, **kw):
                    self.assertNotIn("--live", argv)
                    self.assertNotIn("shell", kw)
                    p = Path(argv[argv.index("--output-dir") + 1]) / "unique-run"
                    p.mkdir(parents=True)
                    (p / "receipt.json").write_text(json.dumps(receipt(**update)))
                    return subprocess.CompletedProcess(argv, 0, "", "")
                with patch.object(A.subprocess, "run", side_effect=child):
                    result = A.run_default_drill(Path(td), runs)
                self.assertEqual(result["ok"], not bool(update))

    def test_missing_receipt_nonzero_and_timeout_fail_closed(self):
        for failure in (subprocess.CompletedProcess([], 1, "", "secret"), subprocess.TimeoutExpired([], 150)):
            with tempfile.TemporaryDirectory() as td:
                kw = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(A.subprocess, "run", **kw):
                    result = A.run_default_drill(Path(td), Path(td)/"runs")
                self.assertFalse(result["ok"])
                self.assertNotIn("secret", json.dumps(result))

    def test_entrypoint_rejects_live_without_any_delivery(self):
        with patch.object(A, "accept") as accept, patch.object(A, "notify_failure") as notify, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
            A.main(["--live"])
        self.assertEqual(exit.exception.code, 2)
        accept.assert_not_called()
        notify.assert_not_called()

    def test_launchd_is_default_only_and_runs_every_two_minutes(self):
        p = ROOT / "deploy/mac/agents/com.kg-hub.claude-mem-upgrade-acceptance.plist"
        data = plistlib.loads(p.read_bytes())
        self.assertEqual(data["StartInterval"], 120)
        self.assertTrue(data["RunAtLoad"])
        self.assertNotIn("--live", data["ProgramArguments"])
        self.assertEqual(len(data["ProgramArguments"]), 2)


class Notification(unittest.TestCase):
    def test_installed_skill_receives_named_channel_no_webhook_in_argv(self):
        home = Path("/isolated/home")
        with patch.object(A.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertFalse(A.notify_failure(home, "failure"))
        argv = run.call_args.args[0]
        self.assertEqual(argv[1], str(home / ".claude/skills/feishu-notify/scripts/send.py"))
        self.assertEqual(argv[argv.index("--webhook") + 1], "kg-hub")
        self.assertNotIn("--live", argv)


if __name__ == "__main__":
    unittest.main()
