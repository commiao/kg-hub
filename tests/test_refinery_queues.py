"""Queue telemetry must measure real outstanding rows, including new arrivals."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import refinery_queues as Q
import flow_dashboard as F


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "source.db"
        self.history = Path(self.tmp.name) / "queue-remaining.sqlite3"
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY)")
            db.executemany("INSERT INTO observations VALUES (?)", [(x,) for x in (1, 4, 9, 11, 50, 99)])
        self.wm = dict(boundary_id=10, live_cursor=50, ingested={1, 11}, rejected=set(),
                       held={9}, failed=set())
        self.now = 1790946000

    def sample(self, at=None, process="a"):
        Q.sample(self.source, self.history, self.wm, process, at or self.now)

    def test_sparse_ids_cursor_inflight_and_held_are_not_confused(self):
        self.sample()
        row = Q.read(self.history, self.now)[0]
        self.assertEqual((row["backlog"], row["live"], row["backlog_held"]), (1, 2, 1))
        self.assertIsNone(row["live_rate"])

    def test_arrivals_minus_completions_gives_negative_net_rate(self):
        self.sample()
        self.wm["ingested"].add(50)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.executemany("INSERT INTO observations VALUES (?)", [(110,), (120,)])
        self.sample(self.now + 120)
        row = Q.read(self.history, self.now + 120)[-1]
        self.assertEqual(row["live"], 3)
        self.assertEqual(row["live_rate"], -30)
        self.assertEqual(row["backlog_rate"], 0)
        self.wm["rejected"].add(4)
        self.sample(self.now + 240)
        self.assertEqual(Q.read(self.history, self.now + 240)[-1]["backlog_rate"], 30)

    def test_delayed_sync_landing_at_once_is_a_burst_not_a_rate(self):
        self.sample()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.executemany("INSERT INTO observations VALUES (?)",
                           [(x,) for x in range(1000, 1000 + Q.BURST_ROWS)])
        self.sample(self.now + 120)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("INSERT INTO observations VALUES (5000)")
        self.sample(self.now + 240)
        burst, after = Q.read(self.history, self.now + 240)[1:]
        self.assertEqual(burst["live"], 2 + Q.BURST_ROWS)
        self.assertEqual(burst["live_burst"], Q.BURST_ROWS)
        self.assertIsNone(burst["live_rate"])
        self.assertEqual((burst["backlog_rate"], burst["backlog_burst"]), (0, None))
        self.assertEqual((after["live_rate"], after["live_burst"]), (-30, None))

    def test_history_written_before_source_totals_still_reads(self):
        with closing(sqlite3.connect(self.history)) as db, db:
            db.execute("CREATE TABLE samples (at REAL PRIMARY KEY, process TEXT, boundary INTEGER, "
                       "live INTEGER, backlog INTEGER, live_held INTEGER, backlog_held INTEGER)")
            db.execute("INSERT INTO samples VALUES (?, 'a', 10, 2581, 1, 0, 1)", (self.now - 120,))
        self.sample()
        old, new = Q.read(self.history, self.now)
        self.assertIsNone(old["live_total"])
        self.assertEqual(new["live_total"], 3)
        # Without a prior total the jump cannot be attributed, so the rate stays.
        self.assertEqual(new["live_rate"], (2581 - 2) * 30)
        self.assertIsNone(new["live_burst"])

    def test_restart_gap_and_boundary_do_not_create_rates(self):
        self.sample()
        self.wm["ingested"].add(50)
        self.sample(self.now + 120, "b")
        self.sample(self.now + 900, "b")
        self.wm["boundary_id"] = 60
        self.sample(self.now + 1020, "b")
        self.assertTrue(all(r["live_rate"] is None for r in Q.read(self.history, self.now + 1020)))

    def test_unreadable_source_is_not_a_zero_sample(self):
        self.sample()
        self.source.unlink()
        with self.assertRaises(sqlite3.Error):
            self.sample(self.now + 120)
        self.assertEqual(len(Q.read(self.history, self.now + 120)), 1)

    def test_retention_and_staleness(self):
        self.sample(self.now - Q.RETENTION - 1)
        self.sample()
        self.assertEqual(len(Q.read(self.history, self.now)), 1)
        flow = F.refinery_queue_trends(self.history, datetime.fromtimestamp(self.now + 601, timezone.utc))
        self.assertTrue(flow["stale"])
        self.assertEqual(flow["latest"]["live"], 2)
        self.assertEqual(Q.read(self.history.with_name("missing"), self.now), [])

    def test_sampler_runs_without_dashboard_or_dispatch(self):
        import kg_refinery as R
        async def stop_after_one(_):
            raise asyncio.CancelledError()
        with patch.object(R, "DB_PATH", self.source), patch.object(R, "STATE_DIR", Path(self.tmp.name)), \
             patch.object(R.time, "time", return_value=self.now), \
             patch.object(Q, "sample", wraps=Q.sample) as sample, \
             patch.object(R.asyncio, "sleep", side_effect=stop_after_one):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(R.queue_sample_loop(self.wm))
        sample.assert_called_once()
        self.assertEqual(sample.call_args.args[2]["boundary_id"], 10)
        self.assertIsNot(sample.call_args.args[2]["ingested"], self.wm["ingested"])
        self.assertEqual(Q.read(self.history, self.now)[0]["live"], 2)

    def test_worker_restart_inside_same_hour_clears_rate(self):
        points = [dict(at=self.now+i*120, worker="current", depth=20-i, source="live", pid=pid)
                  for i, pid in enumerate((1, 1, 2))]
        data = F.claude_mem_trends([dict(claude_mem_queue=dict(history=points, sampled_at=self.now+240))],
                                 datetime.fromtimestamp(self.now+240, timezone.utc))[0]
        self.assertIsNone(data["rows"][-1]["current_rate"])


if __name__ == "__main__":
    unittest.main()
