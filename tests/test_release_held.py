"""Only observations whose failed calls never reached the provider are resubmitted."""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import release_held as H

SD = "claude-mem obs id={} project=p type=discovery platform=claude"


class ClassifyTests(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(H.classify(["absent"]), H.RESET)
        self.assertEqual(H.classify(["absent", "failed"]), H.RESET)
        self.assertEqual(H.classify(["completed", "absent"]), H.RESUME)
        self.assertEqual(H.classify(["completed", "completed"]), H.ALL_COMPLETED)
        for maybe_paid in ("http_started", "prepared", "unknown", "admitted"):
            self.assertEqual(H.classify(["absent", maybe_paid]), H.UNKNOWN, maybe_paid)
        self.assertEqual(H.classify([]), H.UNKNOWN)


class FakeDriver:
    def __init__(self, keys):
        self.keys, self.deletes = keys, []

    async def execute_query(self, query, **p):
        key = (p["sd"], p["sid"])
        if "DELETE" in query:
            self.assertions(query)
            row = self.keys.get(key)
            if row and row["status"] == "needs_reconciliation" and row["worker_state"] is None:
                del self.keys[key]
                self.deletes.append(key)
                return [{"c": 1}], None, None
            return [{"c": 0}], None, None
        row = self.keys.get(key)
        return ([dict(row)] if row else []), None, None

    @staticmethod
    def assertions(query):
        assert "k.status = 'needs_reconciliation'" in query and "k.worker_state IS NULL" in query


class ResetKeysTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "model-attempts.sqlite3"
        db = sqlite3.connect(self.db_path)
        db.execute("CREATE TABLE model_attempts (phase TEXT, source_description TEXT, source_obs_id TEXT)")
        db.executemany("INSERT INTO model_attempts VALUES (?,?,?)", [
            ("absent", SD.format(1), "s1"),
            ("completed", SD.format(2), "s2"), ("absent", SD.format(2), "s2"),
            ("absent", SD.format(3), "s3"),
            ("absent", SD.format(4), "s4"),
            ("absent", SD.format(10), "s10"),          # id=1 前缀不能误匹配 id=10
        ])
        db.commit(); db.close()
        self.env = patch.dict(os.environ, {"KG_HUB_INGEST_BACKUP_PATH": str(root / "ingest-backup.jsonl")})
        self.env.start()
        self.watermark = root / "watermark.json"
        self.watermark.write_text(json.dumps({"held": [1, 2, 3, 4], "ingested": [9], "boundary_id": 5}))
        self.driver = FakeDriver({
            (SD.format(1), "s1"): {"status": "needs_reconciliation", "worker_state": None},
            (SD.format(2), "s2"): {"status": "needs_reconciliation", "worker_state": None},
            (SD.format(3), "s3"): {"status": "needs_reconciliation", "worker_state": "running"},
            (SD.format(4), "s4"): {"status": "ok", "worker_state": None},
        })

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def plan(self):
        return asyncio.run(H.plan(self.watermark, self.driver))

    def test_plan_classifies_without_writing(self):
        items = {i["oid"]: i for i in self.plan()}
        self.assertEqual(items[1]["category"], H.RESET)
        self.assertEqual(items[2]["category"], H.RESUME)
        self.assertEqual(items[4]["server"], "ok")
        self.assertEqual(self.driver.deletes, [])

    def test_dry_run_deletes_nothing(self):
        got = asyncio.run(H.reset_keys(self.plan(), self.driver, limit=10, apply=False))
        self.assertEqual(got, [1, 3])
        self.assertEqual(self.driver.deletes, [])

    def test_apply_only_resets_unpaid_idle_claims(self):
        got = asyncio.run(H.reset_keys(self.plan(), self.driver, limit=10, apply=True))
        self.assertEqual(got, [1])                    # 2 有成功调用；3 正在执行
        self.assertEqual(self.driver.deletes, [(SD.format(1), "s1")])

    def test_journal_change_after_plan_is_skipped(self):
        items = self.plan()
        db = sqlite3.connect(self.db_path)
        db.execute("INSERT INTO model_attempts VALUES ('completed', ?, 's1')", (SD.format(1),))
        db.commit(); db.close()
        self.assertEqual(asyncio.run(H.reset_keys(items, self.driver, limit=10, apply=True)), [])

    def test_limit_bounds_one_batch(self):
        self.driver.keys[(SD.format(3), "s3")]["worker_state"] = None
        got = asyncio.run(H.reset_keys(self.plan(), self.driver, limit=1, apply=True))
        self.assertEqual(got, [1])

    def test_unhold_moves_only_held_ids(self):
        result = H.unhold(self.watermark, release=[1, 99], ingested=[4])
        wm = json.loads(self.watermark.read_text())
        self.assertEqual(wm["held"], [2, 3])
        self.assertEqual(wm["ingested"], [4, 9])
        self.assertEqual(wm["boundary_id"], 5)
        self.assertEqual(result, {"released": 1, "marked_ingested": 1, "held_left": 2})


if __name__ == "__main__":
    unittest.main()
