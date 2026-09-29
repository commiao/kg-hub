"""Journal pruning may only remove evidence that no longer guards anything."""
import pathlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from utils.graphiti_stage_adapter import StageArtifactStore
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

    def seed(self, sid, *, execution="succeeded", grant=False):
        sd = "source"
        key = f"key-{sid}"
        self.journal.prepare(key=key, business_key="kg-hub", source_description=sd,
                             source_obs_id=sid, step_id="step", request_digest="digest")
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
        return sd, sid

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
        self.assertEqual(params, {"cutoff": "2026-09-22"})

    def test_release_prunes_after_drain_and_before_switch_only_when_asked(self):
        src = (ROOT / "deploy/nas/release.sh").read_text(encoding="utf-8")
        drained = src.index('没排空干净，已中止')
        hook = src.index('if [ "${KG_HUB_JOURNAL_PRUNE:-0}" = 1 ]; then')
        switch = src.index("# ---- 5. 切标签 + 起容器")
        self.assertLess(drained, hook)
        self.assertLess(hook, switch)
        self.assertIn("python -m utils.journal_prune --apply", src[hook:switch])


if __name__ == "__main__":
    unittest.main()
