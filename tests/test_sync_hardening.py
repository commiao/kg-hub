"""Real processes and disposable SQLite copies; no production writes/network."""
import gzip
import hashlib
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'tools/sync_guard.py'


class SyncSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dbdir = self.root / 'db'
        self.dbdir.mkdir()
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        shutil.copy(GUARD, self.root)
        script = (ROOT / 'tools/nas_apply_claude_mem_delta.sh').read_text()
        script = script.replace('/volume2/4T/kg-hub-data/claude-mem', str(self.dbdir))
        script = script.replace('/volume1/public-sync/kg-hub-inbox', str(self.inbox))
        if not shutil.which('sha256sum'):
            script = script.replace('sha256sum ', 'shasum -a 256 ')
        self.script = self.root / 'apply.sh'
        self.script.write_text(script)
        self.db = self.dbdir / 'claude-mem.db'
        self.make_db(self.db, 1)
        delta = self.root / 'delta.db'
        self.make_db(delta, 2)
        self.payload = gzip.compress(delta.read_bytes())

    def tearDown(self):
        self.temp.cleanup()

    def make_db(self, path, row):
        with sqlite3.connect(path) as c:
            c.executescript('CREATE TABLE observations(id INTEGER PRIMARY KEY, memory_session_id TEXT); CREATE TABLE sdk_sessions(memory_session_id TEXT PRIMARY KEY);')
            c.execute('INSERT INTO observations VALUES (?,?)', (row, str(row)))
            c.execute('INSERT INTO sdk_sessions VALUES (?)', (str(row),))
        c.close()

    def apply(self, data=None, *args, mode='merge'):
        return subprocess.run(['/bin/sh', str(self.script), '2', *args],
                              input=self.payload if data is None else data,
                              env=dict(os.environ, MODE=mode), capture_output=True, timeout=8)

    def test_merge_and_replace(self):
        self.assertEqual(self.apply().returncode, 0)
        self.assertEqual(sqlite3.connect(self.db).execute('SELECT COUNT(*) FROM observations').fetchone()[0], 2)
        self.assertEqual(self.apply(mode='replace').returncode, 0)
        self.assertEqual(sqlite3.connect(self.db).execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)

    def test_truncated_stream_keeps_live_database(self):
        before = self.db.read_bytes()
        self.assertNotEqual(self.apply(self.payload[:len(self.payload)//2]).returncode, 0)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.apply().returncode, 0)

    def test_drive_verified_payload(self):
        (self.inbox / 'delta.gz').write_bytes(self.payload)
        result = self.apply(b'', 'delta.gz', str(len(self.payload)), hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_applier_excludes_other_writer_before_cleanup(self):
        held = subprocess.Popen(['/bin/sh', str(self.script), '2'], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 3
            while not (self.dbdir / '.delta.db').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            result = self.apply()
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertTrue((self.dbdir / '.delta.db').exists())
            held.stdin.write(self.payload)
            held.stdin.close()
            self.assertEqual(held.wait(timeout=5), 0)
        finally:
            if held.poll() is None:
                held.terminate(); held.wait(timeout=3)
            held.stdout.close()
            held.stderr.close()

    def test_deadline_kills_group_and_next_run_succeeds(self):
        lock = self.root / 'lock'
        marker = self.root / 'escaped'
        command = ['sh', '-c', f'(sleep 1; touch "{marker}") & wait']
        p = subprocess.run([sys.executable, str(GUARD), str(lock), '.15', *command], timeout=3)
        self.assertEqual(p.returncode, 124)
        time.sleep(1)
        self.assertFalse(marker.exists())
        self.assertEqual(subprocess.run([sys.executable, str(GUARD), str(lock), '2', 'true']).returncode, 0)

    def test_lock_survives_supervisor_kill_until_writer_exits(self):
        lock = self.root / 'inherited-lock'
        marker = self.root / 'writer-started'
        child = f'import pathlib,time; pathlib.Path({str(marker)!r}).touch(); time.sleep(1)'
        held = subprocess.Popen([sys.executable, str(GUARD), str(lock), '5', sys.executable, '-c', child])
        try:
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists())
            held.kill(); held.wait(timeout=2)
            blocked = subprocess.run([sys.executable, str(GUARD), str(lock), '2', 'true'])
            self.assertEqual(blocked.returncode, 75)
            time.sleep(1.1)
            self.assertEqual(subprocess.run([sys.executable, str(GUARD), str(lock), '2', 'true']).returncode, 0)
        finally:
            if held.poll() is None:
                held.terminate(); held.wait(timeout=2)

    def test_missing_drive_rejected_then_stream_recovers(self):
        self.script.write_text(self.script.read_text().replace('WAIT_MAX=180', 'WAIT_MAX=2'))
        before = self.db.read_bytes()
        self.assertEqual(self.apply(b'', 'absent.gz', '10', 'bad').returncode, 10)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.apply().returncode, 0)

    def mac_script(self, source, local_inbox, ssh_body=''):
        """The real Mac-side script, wired to temp paths with a local SSH stand-in."""
        bindir = self.root / 'bin'
        bindir.mkdir(exist_ok=True)
        ssh = bindir / 'ssh'
        ssh.write_text('#!/bin/sh\nwhile [ "$1" = "-o" ]; do shift 2; done\nshift\n'
                       + ssh_body + 'exec /bin/sh -c "$1"\n')
        ssh.chmod(0o755)
        sha = bindir / 'sha256sum'
        sha.write_text('#!/bin/sh\nexec shasum -a 256 "$@"\n')
        sha.chmod(0o755)
        script = (ROOT / 'tools/sync_claude_mem_to_nas.sh').read_text()
        script = script.replace('/Users/mac/.claude-mem/claude-mem.db', str(source))
        script = script.replace('/volume2/4T/kg-hub-data/claude-mem', str(self.dbdir))
        script = script.replace('/Users/mac/.kg-hub/state', str(self.root / 'state'))
        script = script.replace('/Users/mac/public-sync/kg-hub-inbox', str(local_inbox))
        remote = str(self.root / 'nas_apply_claude_mem_delta.sh')
        script = script.replace('/volume1/docker/kg-hub-src/tools/nas_apply_claude_mem_delta.sh', remote)
        # Fixture must not run the production global stale-file janitor.
        script = script.replace('find /tmp -maxdepth', f'find "{self.root}" -maxdepth')
        self.script.write_text(self.script.read_text().replace('WAIT_MAX=180', 'WAIT_MAX=2'))
        shutil.copy(self.script, remote)
        local = self.root / 'sync.sh'
        local.write_text(script)
        return local, dict(os.environ, PATH=str(bindir) + ':' + os.environ['PATH'])

    def nas_max(self):
        with sqlite3.connect(self.db) as c:
            return c.execute('SELECT MAX(id) FROM observations').fetchone()[0]

    def test_row_written_after_watermark_waits_for_next_cycle(self):
        # The bug this pins (2026-09-11): the delta was built with `id > nas_max`
        # and no upper bound, so rows landing after the watermark read rode along
        # and the applier's strict `MAX(id) == expect` rejected the whole round --
        # and full_rebuild, sharing the stale watermark, was rejected right after.
        # The stand-in writes id=3 while answering the probe, i.e. exactly between
        # the watermark read and the delta build. No sleeps, no flakiness.
        source = self.root / 'source.db'
        self.make_db(source, 2)
        once = self.root / 'injected'
        inject = (f'if [ ! -e "{once}" ]; then case "$1" in *integ=*) : > "{once}";'
                  f' sqlite3 "{source}" "INSERT INTO observations VALUES(3,\'3\');'
                  f' INSERT INTO sdk_sessions VALUES(\'3\');";; esac; fi\n')
        local, env = self.mac_script(source, self.inbox, inject)

        first = subprocess.run(['/bin/sh', str(local)], env=env, capture_output=True, timeout=20)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(once.exists(), 'fixture never injected the racing row')
        self.assertNotIn(b'FAIL', first.stdout + first.stderr)
        self.assertIn(b'synced +1', first.stdout)
        self.assertEqual(self.nas_max(), 2, 'row written after the watermark must not ride along')

        second = subprocess.run(['/bin/sh', str(local)], env=env, capture_output=True, timeout=20)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn(b'synced +1', second.stdout)
        self.assertEqual(self.nas_max(), 3, 'the deferred row must arrive next cycle')

    def test_local_sync_drive_fallback_and_next_cycle(self):
        source = self.root / 'source.db'
        self.make_db(source, 2)
        # Distinct local/remote inboxes simulate Drive not delivering its file.
        local, env = self.mac_script(source, self.root / 'local-inbox')
        result = subprocess.run(['/bin/sh', str(local)], env=env, capture_output=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b'synced +1', result.stdout)
        result = subprocess.run(['/bin/sh', str(local)], env=env, capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'skip', result.stdout)


if __name__ == '__main__':
    unittest.main()
