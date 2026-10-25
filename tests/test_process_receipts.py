"""Actual process/lease tests, never invoking a provider or a production writer."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import process_receipts as r
from tools import extract_runtime


class ReceiptTests(unittest.TestCase):
    def test_brief_status_reader_does_not_make_native_start_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "lease.lock").touch()
            ready = threading.Event()
            def reader():
                import fcntl
                import time
                with (root / "lease.lock").open() as lock:
                    fcntl.flock(lock, fcntl.LOCK_SH)
                    ready.set()
                    time.sleep(0.02)
            thread = threading.Thread(target=reader)
            thread.start()
            self.assertTrue(ready.wait(5))
            try:
                with r.attempt(root, kind="native_cli"):
                    pass
                self.assertEqual(r.status(root)["state"], "completed")
            finally:
                thread.join(5)

    def test_owner_finishing_between_receipt_read_and_lock_probe_is_not_an_orphan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ready, finish, ended = threading.Event(), threading.Event(), threading.Event()
            def owner():
                with r.attempt(root, kind="native_cli"):
                    ready.set()
                    self.assertTrue(finish.wait(5))
                ended.set()
            thread = threading.Thread(target=owner)
            thread.start()
            self.assertTrue(ready.wait(5))
            original = r.read
            first = True
            def read(path):
                nonlocal first
                record = original(path)
                if first:
                    first = False
                    finish.set()
                    self.assertTrue(ended.wait(5))
                return record
            try:
                with mock.patch.object(r, "read", side_effect=read):
                    state = r.status(root)
                self.assertEqual(state["state"], "completed")
                self.assertEqual(state["exit_code"], 0)
                self.assertFalse(state["lease_held"])
            finally:
                finish.set()
                thread.join(5)

    def test_live_exit_and_single_owner_without_process_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with r.attempt(root, kind="submit") as update:
                update(phase="waiting_nas")
                self.assertEqual(r.status(root)["phase"], "waiting_nas")
                self.assertTrue(r.status(root)["lease_held"])
                with self.assertRaises(r.Busy):
                    with r.attempt(root, kind="submit"):
                        self.fail("duplicate operation ran")
            self.assertEqual(r.status(root)["state"], "completed")
            self.assertEqual(r.status(root)["exit_code"], 0)
            self.assertEqual((root / "latest.json").stat().st_mode & 0o777, 0o600)

    def test_crash_is_uncertain_and_cannot_authorize_a_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code = ("import os; from pathlib import Path; from utils.process_receipts import attempt; "
                    "ctx=attempt(Path(" + repr(temp) + "),kind='submit'); ctx.__enter__(); os._exit(9)")
            done = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent)
            self.assertEqual(done.returncode, 9)
            self.assertEqual(r.status(root)["state"], "uncertain")
            with self.assertRaises(r.Uncertain):
                with r.attempt(root, kind="submit"):
                    self.fail("uncertain submit replayed")

    def test_dispatch_failure_retains_reconciliation_requirement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ConnectionError):
                with r.attempt(root, kind="submit") as update:
                    update(phase="submitting", may_have_written=True)
                    raise ConnectionError("unknown remote result")
            self.assertTrue(r.status(root)["requires_reconciliation"])
            with self.assertRaises(r.Uncertain):
                with r.attempt(root, kind="submit"):
                    self.fail("uncertain remote result replayed")

    def test_runtime_records_real_child_exit_and_status_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "native"
            self.assertEqual(extract_runtime.execute([sys.executable, "-c", "raise SystemExit(7)"],
                                                     proxy_mode="none", receipt_dir=root), 7)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(extract_runtime.main(["status", "--receipt-dir", str(root)]), 0)
            self.assertEqual(json.loads(output.getvalue())["exit_code"], 7)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_missing_status_does_not_create_a_lock_or_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "missing"
            self.assertEqual(r.status(root)["state"], "not_started")
            self.assertFalse(root.exists())

    def test_invalid_optional_ide_environment_is_child_only_and_auth_is_not_dropped(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'environment.json'
            source = "import os,json,pathlib,sys;pathlib.Path(sys.argv[1]).write_text(json.dumps({'ide':os.environ.get('WEBIDE_VM_OPTIONS'),'auth':os.environ.get('OPENAI_API_KEY')}))"
            bad = 'invalid-\udcff'
            with mock.patch.dict(os.environ, {'WEBIDE_VM_OPTIONS':bad,'OPENAI_API_KEY':'synthetic'}):
                self.assertEqual(extract_runtime.execute([sys.executable,'-c',source,str(path)],proxy_mode='none'),0)
                self.assertEqual(os.environ['WEBIDE_VM_OPTIONS'],bad)
                self.assertEqual(json.loads(path.read_text()), {'ide':None,'auth':'synthetic'})
            with mock.patch.dict(os.environ, {'OPENAI_API_KEY':bad}), self.assertRaises(ValueError):
                extract_runtime.execute([sys.executable,'-c','raise AssertionError()'],proxy_mode='none')

    def test_different_owner_cannot_replace_a_completed_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with r.attempt(root,kind='supervisor',identity={'model':'first'}): pass
            original = (root/'latest.json').read_bytes()
            with self.assertRaises(r.Uncertain):
                with r.attempt(root,kind='supervisor',identity={'model':'other'}): pass
            self.assertEqual((root/'latest.json').read_bytes(),original)


if __name__ == "__main__":
    unittest.main()
