"""Real process leases and eight concurrent callers; fake SSH/HTTP, no NAS."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import subprocess
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import nas_gate as gate
from tools.nas_queue import reserve


class FairQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {'lock_dir': str(self.root), 'max_c': 90, 'samples': 1,
                    'sample_interval_s': 0, 'gap_s': {'dryrun': 0, 'submit': 0},
                    'max_wait_s': .5, 'max_misses': 2, 'recheck_s': .01}

    def tearDown(self):
        self.tmp.cleanup()

    def hold(self):
        return gate.hold('dryrun', settings=self.cfg, read=lambda _: 70, log=lambda _: None)

    def test_eight_callers_fifo_and_progressing_queue_can_exceed_one_wait_budget(self):
        order, release, first = [], threading.Event(), threading.Event()
        def work(index):
            start = time.monotonic()
            with self.hold() as info:
                order.append(index)
                post = gate.track_responses(lambda: (200, {}), info)
                if index == 0:
                    first.set()
                    while not release.wait(.02):
                        post()  # synthetic responses, never a timer-only heartbeat
                time.sleep(.09)
                post()
            return time.monotonic() - start
        with mock.patch.object(gate, 'LOCK_POLL_S', .005), ThreadPoolExecutor(8) as pool:
            futures = [pool.submit(work, 0)]
            try:
                self.assertTrue(first.wait(3))
                for index in range(1, 8):
                    futures.append(pool.submit(work, index))
                    end = time.monotonic() + 3
                    while len(list((self.root / 'nas-heavy.queue').glob('*.ticket'))) < index + 1:
                        self.assertLess(time.monotonic(), end)
                        time.sleep(.002)
            finally:
                release.set()
            durations = [f.result(timeout=5) for f in futures]
        self.assertEqual(order, list(range(8)))
        self.assertGreater(durations[-1], self.cfg['max_wait_s'])
        self.assertEqual(list((self.root / 'nas-heavy.queue').glob('*.ticket')), [])

    def test_live_head_without_real_progress_times_out_without_pruning_its_lease(self):
        self.cfg['max_wait_s'] = .04
        with reserve(self.root, 'submit', time.time) as live:
            with mock.patch.object(gate, 'LOCK_POLL_S', .005), self.assertRaises(gate.GateRefused) as error:
                with self.hold():
                    self.fail('stalled queue must not execute')
            self.assertEqual(error.exception.code, 76)
            self.assertTrue(live.path.exists())

    def test_dead_ticket_retired_by_lease_not_pid_and_live_cannot_be_overtaken(self):
        q = self.root / 'nas-heavy.queue'
        q.mkdir()
        stale = q / ('00000000000000000001-dead.ticket')
        stale.write_text('partial crash metadata')
        with reserve(self.root, 'dryrun', time.time) as first:
            self.assertFalse(stale.exists())
            with reserve(self.root, 'submit', time.time) as second:
                self.assertEqual(first.position()[0], 0)
                self.assertEqual(second.position()[0], 1)
        self.assertEqual(list(q.glob('*.ticket')), [])

    def test_callback_updates_only_after_http_returns_and_preserves_result_on_observer_error(self):
        progress = mock.Mock()
        post = mock.Mock(side_effect=TimeoutError('unknown result'))
        with self.assertRaises(TimeoutError):
            gate.track_responses(post, {'response_progress': progress})()
        progress.assert_not_called()
        progress.side_effect = OSError('disk full')
        result = (200, {'status': 'ok'})
        self.assertIs(gate.track_responses(lambda: result, {'response_progress': progress})(), result)

    def test_counter_corruption_fails_closed(self):
        q = self.root / 'nas-heavy.queue'
        q.mkdir()
        (q / 'sequence').write_text('invalid')
        with self.assertRaises(ValueError):
            with self.hold():
                self.fail('corrupt ordering must not run')

    def test_process_crash_releases_ticket_without_blocking_followers(self):
        source = str(Path(gate.__file__).resolve().parents[1])
        code = 'from tools.nas_queue import reserve;import os,time;from pathlib import Path\nwith reserve(Path(' + repr(str(self.root)) + '),"submit",time.time):os._exit(0)\n'
        subprocess.run([sys.executable, '-c', code], env={**os.environ, 'PYTHONPATH': source}, check=True)
        self.assertEqual(len(list((self.root / 'nas-heavy.queue').glob('*.ticket'))), 1)
        with self.hold():
            pass
        self.assertEqual(list((self.root / 'nas-heavy.queue').glob('*.ticket')), [])


if __name__ == '__main__':
    unittest.main()
