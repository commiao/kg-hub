"""T-0234: the server holds model-attempts and the reconciliation mailbox open so
their -wal/-shm are not deleted and recreated on every open/close."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import sqlite_anchor  # noqa: E402
from utils.model_attempt_journal import ModelAttemptJournal  # noqa: E402
from utils.reconciliation_mailbox import MailboxStore  # noqa: E402


def open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def sqlite_deletes_wal_on_last_close() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.sqlite3"
        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t(a)")
        db.commit()
        db.close()
        return not Path(str(path) + "-wal").exists()


def journal_mode(path: Path) -> str:
    db = sqlite3.connect(path)
    try:
        return db.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        db.close()


class SqliteAnchorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(sqlite_anchor.release_all)

    def journal(self):
        return ModelAttemptJournal(self.root / "model-attempts.sqlite3")

    def test_hold_open_really_holds_a_wal_database_and_is_idempotent(self):
        journal = self.journal()
        before = open_fds()
        self.assertTrue(sqlite_anchor.hold_open(journal.path))
        self.assertGreaterEqual(open_fds() - before, 2)
        self.assertTrue(sqlite_anchor.hold_open(journal.path))
        self.assertGreaterEqual(open_fds() - before, 2)
        journal.find_task("sd", "sid")                        # still serving
        sqlite_anchor.release_all()
        self.assertEqual(open_fds(), before)

    def test_a_non_wal_database_is_left_alone(self):
        path = self.root / "delete-mode.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE t(a)")
        db.commit()
        db.close()
        self.assertFalse(sqlite_anchor.hold_open(path))
        self.assertEqual(journal_mode(path), "delete")

    def test_a_missing_database_is_not_created(self):
        path = self.root / "missing.sqlite3"
        self.assertFalse(sqlite_anchor.hold_open(path))
        self.assertFalse(path.exists())

    def test_only_the_opening_process_closes_its_anchor(self):
        journal = self.journal()
        sqlite_anchor.hold_open(journal.path)
        pid, connection = sqlite_anchor._anchors[journal.path]
        sqlite_anchor._anchors[journal.path] = (pid + 1, connection)   # as a fork child sees it
        self.assertTrue(sqlite_anchor.hold_open(journal.path))          # opens its own
        child_view = sqlite_anchor._anchors[journal.path]
        self.assertEqual(child_view[0], os.getpid())
        sqlite_anchor._anchors[journal.path] = (pid + 1, connection)
        sqlite_anchor.release_all()
        connection.execute("SELECT 1").fetchall()                      # left open
        connection.close()
        child_view[1].close()

    def test_constructing_the_stores_holds_nothing_open(self):
        before = open_fds()
        journal = self.journal()
        MailboxStore(self.root / "reconciliation-mailbox.sqlite3")
        journal.find_task("sd", "sid")
        self.assertEqual(open_fds(), before)
        self.assertEqual(sqlite_anchor._anchors, {})

    @unittest.skipUnless(sqlite_deletes_wal_on_last_close(),
                         "this SQLite persists the WAL; run where it is deleted (kg-hub image)")
    def test_opens_and_closes_no_longer_recreate_wal_and_shm(self):
        journal = self.journal()
        mailbox = MailboxStore(self.root / "reconciliation-mailbox.sqlite3")
        for path in (journal.path, mailbox.path):
            self.assertTrue(sqlite_anchor.hold_open(path))
        inodes = lambda: tuple(Path(str(p) + s).stat().st_ino
                               for p in (journal.path, mailbox.path) for s in ("-wal", "-shm"))
        before = inodes()
        for n in range(5):
            journal.find_task("sd", f"sid-{n}")
            mailbox.all_reports()
            self.assertEqual(inodes(), before)

    def test_server_lifespan_holds_both_and_releases_them(self):
        source = (Path(__file__).resolve().parent.parent / "kg_hub_server.py").read_text("utf-8")
        body = source[source.index("async def _application_lifespan("):]
        body = body[:body.index("\napp = Starlette(")]
        self.assertIn("sqlite_anchor.hold_backup_databases", body)
        self.assertIn("sqlite_anchor.release_all()", body)
        self.assertLess(body.index("sqlite_anchor.hold_backup_databases"), body.index("yield"))

    def test_startup_holds_both_databases_and_never_raises(self):
        journal = self.journal()
        MailboxStore(self.root / "reconciliation-mailbox.sqlite3")
        held = sqlite_anchor.hold_backup_databases(self.root / "ingest-backup.jsonl")
        self.assertEqual(held, {"model-attempts.sqlite3": True,
                                "reconciliation-mailbox.sqlite3": True})
        self.assertEqual(set(sqlite_anchor._anchors),
                         {journal.path, self.root / "reconciliation-mailbox.sqlite3"})

    def test_startup_is_fail_open(self):
        def broken():
            raise sqlite3.OperationalError("database is locked")

        held = sqlite_anchor.hold_backup_databases(self.root / "ingest-backup.jsonl", broken)
        self.assertEqual(held, {"model-attempts.sqlite3": False,
                                "reconciliation-mailbox.sqlite3": False})


if __name__ == "__main__":
    unittest.main()
