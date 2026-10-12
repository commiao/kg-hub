"""Hold one read-only connection per WAL database for the serving process (T-0234).

model-attempts.sqlite3 and reconciliation-mailbox.sqlite3 are WAL databases
that every caller opens and closes on its own. With no connection left open,
each close is the last one: SQLite checkpoints and deletes -wal/-shm, and the
next open creates them again. On the NAS the reconciliation loop alone did that
about 50 times a second in bursts every 10 seconds -- 32KB of -shm written and
thrown away per cycle (~1.5MB/s of cancelled writes) plus a file create and
delete each time on btrfs. While one connection stays open, no close is the
last one and the sidecars stay put.

Only the server's lifespan calls ``hold_open``. A connection must not cross
fork(); kg-hub's production code never forks, and tools that touch these files
(journal_prune, vacuum) run as separate processes. ``release_all`` closes only
the connections this process opened.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger("kg_hub.sqlite_anchor")

_anchors: dict[Path, tuple[int, sqlite3.Connection]] = {}
_lock = threading.Lock()


def hold_open(path: str | Path) -> bool:
    """Hold ``path`` open if it is an existing WAL database. Never switches modes.

    Returns False -- and the database keeps working exactly as before -- when
    the file is missing, is not in WAL mode, or SQLite reports any error.
    """
    path = Path(path)
    with _lock:
        held = _anchors.get(path)
        if held is not None and held[0] == os.getpid():
            return True
        if not path.is_file():
            return False
        anchor = None
        try:
            writer = sqlite3.connect(path, timeout=15)
            try:
                if str(writer.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                    return False
                # -wal/-shm exist while this connection holds them, so the
                # read-only anchor can open on any SQLite build.
                writer.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
                anchor = sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                         uri=True, check_same_thread=False)
                # connect() is lazy; only a statement really opens the file.
                anchor.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            finally:
                writer.close()
        except sqlite3.Error as exc:
            if anchor is not None:
                anchor.close()
            logger.warning("[sqlite-anchor] %s not held: %s", path.name, type(exc).__name__)
            return False
        _anchors[path] = (os.getpid(), anchor)
        return True


HOT_DATABASES = ("model-attempts.sqlite3", "reconciliation-mailbox.sqlite3")


def hold_backup_databases(backup_path: str | Path, ensure_journal=None) -> dict[str, bool]:
    """The server's startup step. Fail-open: logs, never raises."""
    if ensure_journal is not None:
        try:
            ensure_journal()          # creates model-attempts.sqlite3 on a fresh volume
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sqlite-anchor] journal not ready: %s", type(exc).__name__)
    held = {}
    for name in HOT_DATABASES:
        try:
            held[name] = hold_open(Path(backup_path).with_name(name))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sqlite-anchor] %s: %s", name, type(exc).__name__)
            held[name] = False
        logger.info("[sqlite-anchor] %s %s", name, "held" if held[name] else "not held")
    return held


def release_all() -> None:
    with _lock:
        for pid, connection in _anchors.values():
            if pid == os.getpid():
                connection.close()
        _anchors.clear()
