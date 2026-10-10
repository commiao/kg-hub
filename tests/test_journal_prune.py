"""Journal pruning may only remove evidence that no longer guards anything."""
import pathlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from utils.graphiti_stage_adapter import StageArtifactStore
from utils import journal_prune
from utils.journal_prune import prune, settled_tasks
from utils.model_attempt_journal import ModelAttemptJournal
from utils.reconciliation_mailbox import task_uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
TABLES = ("model_attempts", "graphiti_stage_artifacts", "task_executions",
          "gateway_step_mappings", "episode_contexts", "model_retry_grants")


class JournalPruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "model-attempts.sqlite3"
        self.journal = ModelAttemptJournal(self.path)
        self.store = StageArtifactStore(self.path)

    def seed(self, sid, *, execution="succeeded", grant=False, queue=None):
        sd = "source"
        key = f"key-{sid}"
        self.journal.prepare(key=key, business_key="kg-hub", source_description=sd,
                             source_obs_id=sid, step_id="step", request_digest="digest",
                             queue_owned=queue is not None)
        self.journal.complete(key, '{"answer": 1}')
        self.journal.record_gateway_step(sd, sid, key, "wire", "body")
        self.journal.save_episode_context(sd, sid, "op", "input", [])
        self.store.save_or_load(sd, sid, "op", "input", "extraction", {"nodes": []})
        self.journal.begin_task_execution(sd, sid, f"exec-{sid}")
        if execution != "running":
            self.journal.finish_task_execution(sd, sid, f"exec-{sid}", state=execution)
        if grant:
            with sqlite3.connect(self.path) as db:
                db.execute("INSERT INTO model_retry_grants VALUES (?,?,?,?,?,?,?,?,?)",
                           ("grant", sd, sid, "step", "digest", None, "granted", "now", None))
        if queue in ("receipt", "acknowledged"):
            self.journal.queue_business_receipts(sd, sid, f"neo4j:episode:{sid}")
        if queue == "acknowledged":
            self.journal.acknowledge_queue_receipt(key)
        return sd, sid

    def receipts(self, sid):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT count(*) FROM queue_business_receipts "
                              "WHERE idempotency_key=?", (f"key-{sid}",)).fetchone()[0]

    def rows(self, sd, sid):
        tid = task_uuid(sd, sid)
        with sqlite3.connect(self.path) as db:
            return {
                "model_attempts": db.execute(
                    "SELECT count(*) FROM model_attempts WHERE source_obs_id=?", (sid,)).fetchone()[0],
                "graphiti_stage_artifacts": db.execute(
                    "SELECT count(*) FROM graphiti_stage_artifacts WHERE task_sid=?", (sid,)).fetchone()[0],
                "task_executions": db.execute(
                    "SELECT count(*) FROM task_executions WHERE task_id=?", (tid,)).fetchone()[0],
                "gateway_step_mappings": db.execute(
                    "SELECT count(*) FROM gateway_step_mappings WHERE task_id=?", (tid,)).fetchone()[0],
                "episode_contexts": db.execute(
                    "SELECT count(*) FROM episode_contexts WHERE source_obs_id=?", (sid,)).fetchone()[0],
            }

    def test_dry_run_counts_without_deleting(self):
        task = self.seed("done")
        report = prune(self.path, [task], apply=False, pause=0)
        self.assertEqual(report["pruned"], 1)
        self.assertEqual(report["rows"]["model_attempts"], 1)
        self.assertTrue(all(self.rows(*task).values()))

    def test_apply_removes_every_table_of_only_the_listed_task(self):
        done, kept = self.seed("done"), self.seed("kept")
        report = prune(self.path, [done], apply=True, pause=0)
        self.assertEqual(report["pruned"], 1)
        self.assertEqual(set(self.rows(*done).values()), {0})
        self.assertTrue(all(self.rows(*kept).values()))

    def test_unsettled_execution_or_open_grant_is_kept(self):
        running = self.seed("running", execution="running")
        uncertain = self.seed("uncertain", execution="uncertain")
        granted = self.seed("granted", grant=True)
        report = prune(self.path, [running, uncertain, granted], apply=True, pause=0)
        self.assertEqual((report["pruned"], report["skipped_guarding"]), (0, 3))
        for task in (running, uncertain, granted):
            self.assertTrue(all(self.rows(*task).values()), task)

    def test_queue_answer_without_acknowledged_receipt_is_kept(self):
        # No receipt yet: recovery rebuilds it from model_attempts, which must survive.
        missing = self.seed("missing", queue="answer")
        pending = self.seed("pending", queue="receipt")
        report = prune(self.path, [missing, pending], apply=True, pause=0)
        self.assertEqual((report["pruned"], report["skipped_guarding"]), (0, 2))
        self.assertTrue(all(self.rows(*missing).values()))
        self.assertEqual(self.receipts("pending"), 1)

    def test_acknowledged_receipt_is_pruned_with_its_task(self):
        done, kept = self.seed("done", queue="acknowledged"), self.seed("kept", queue="acknowledged")
        report = prune(self.path, [done], apply=True, pause=0)
        self.assertEqual(report["rows"]["queue_business_receipts"], 1)
        self.assertEqual(set(self.rows(*done).values()), {0})
        self.assertEqual((self.receipts("done"), self.receipts("kept")), (0, 1))

    def test_time_budget_stops_between_batches_and_reports_the_rest(self):
        tasks = [self.seed(f"t{i}") for i in range(3)]
        report = prune(self.path, tasks, apply=True, batch=1, pause=0, max_seconds=0)
        self.assertEqual((report["pruned"], report["remaining"]), (0, 3))
        self.assertTrue(all(self.rows(*tasks[0]).values()))

    def test_only_ok_rows_older_than_cutoff_are_selected(self):
        graph = SimpleNamespace(calls=[])

        def ro_query(query, params):
            graph.calls.append((query, params))
            return SimpleNamespace(result_set=[["source", "a"], [None, "b"]])

        graph.ro_query = ro_query
        self.assertEqual(settled_tasks(graph, older_than="2026-09-22"), [("source", "a")])
        query, params = graph.calls[0]
        self.assertIn("k.status = 'ok'", query)
        self.assertIn("< $cutoff", query)
        self.assertIn("ORDER BY coalesce(k.updated_at, k.created_at)", query)
        self.assertEqual(params, {"cutoff": "2026-09-22"})

    def test_tasks_without_journal_rows_are_not_visited(self):
        # Over 90% of settled tasks on the NAS have no rows; probing them cost 33 minutes.
        done = self.seed("done")
        ghosts = [("source", f"ghost{i}") for i in range(3)]
        with mock.patch.object(journal_prune, "_task_rows", wraps=journal_prune._task_rows) as rows:
            report = prune(self.path, [*ghosts, done], apply=True, pause=0)
        self.assertEqual((report["eligible"], report["with_journal"], report["pruned"]), (4, 1, 1))
        self.assertEqual([call.args[2:4] for call in rows.call_args_list], [done])

    def test_settled_order_is_kept_so_a_cut_short_run_resumes_at_the_oldest(self):
        tasks = [self.seed(sid) for sid in ("old", "mid", "new")]
        with mock.patch.object(journal_prune, "_task_rows", wraps=journal_prune._task_rows) as rows:
            prune(self.path, tasks, apply=True, batch=1, pause=0)
        self.assertEqual([call.args[2:4] for call in rows.call_args_list], tasks)

    def test_release_no_longer_prunes_while_producers_are_stopped(self):
        # A full pass takes over half an hour; inside a release that is half an hour of no intake.
        src = (ROOT / "deploy/nas/release.sh").read_text(encoding="utf-8")
        self.assertNotIn("utils.journal_prune", src)
        self.assertNotIn("KG_HUB_JOURNAL_PRUNE", src)

if __name__ == "__main__":
    unittest.main()
