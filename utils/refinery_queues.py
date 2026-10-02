"""Real queue snapshots, independent of dispatch limits and dashboard visits."""
from contextlib import closing
from pathlib import Path
import sqlite3

INTERVAL = 120
RETENTION = 7 * 86400
MAX_GAP = 600


def sample(source: Path, history: Path, wm: dict, process: str, at: float) -> None:
    boundary = wm.get("boundary_id")
    if not isinstance(boundary, int) or isinstance(boundary, bool):
        return
    terminal = set().union(*(wm.get(k, ()) for k in ("ingested", "rejected", "failed", "held")))
    held = set(wm.get("held", ()))
    counts = dict(live=0, backlog=0, live_held=0, backlog_held=0)
    # Match the refinery's immutable, atomically replaced source DB contract.
    with closing(sqlite3.connect(f"file:{source}?mode=ro&immutable=1", uri=True)) as db:
        for (oid,) in db.execute("SELECT id FROM observations"):
            kind = "backlog" if oid <= boundary else "live"
            if oid in held:
                counts[kind + "_held"] += 1
            elif oid not in terminal:
                counts[kind] += 1
    history.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(history, timeout=2)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS samples (at REAL PRIMARY KEY, "
                   "process TEXT, boundary INTEGER, live INTEGER, backlog INTEGER, "
                   "live_held INTEGER, backlog_held INTEGER)")
        db.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?)",
                   (at, process, boundary, counts["live"], counts["backlog"],
                    counts["live_held"], counts["backlog_held"]))
        db.execute("DELETE FROM samples WHERE at < ?", (at - RETENTION,))


def read(history: Path, now: float) -> list[dict]:
    if not history.exists():
        return []
    with closing(sqlite3.connect(f"file:{history}?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM samples WHERE at >= ? AND at <= ? ORDER BY at",
            (now - RETENTION, now + 60))]
    previous = None
    for row in rows:
        for kind in ("live", "backlog"):
            row[kind + "_rate"] = None
            if (previous and row["process"] == previous["process"]
                    and row["boundary"] == previous["boundary"]
                    and 0 < row["at"] - previous["at"] <= MAX_GAP):
                row[kind + "_rate"] = round(
                    (previous[kind] - row[kind]) * 3600 / (row["at"] - previous["at"]), 1)
        previous = row
    return rows
