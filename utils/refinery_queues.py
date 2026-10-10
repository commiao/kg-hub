"""Real queue snapshots, independent of dispatch limits and dashboard visits."""
from contextlib import closing
from pathlib import Path
import sqlite3

INTERVAL = 120
RETENTION = 30 * 86400
MAX_GAP = 600
# Normal Mac→NAS syncs add a few dozen rows every 15 minutes. A jump this large
# in one 2-minute interval is a delayed sync landing at once (2026-10-08: +4405
# rows after 4.7 days without sync), not a digestion rate worth plotting.
BURST_ROWS = 200
_COLUMNS = ("at", "process", "boundary", "live", "backlog", "live_held",
            "backlog_held", "live_total", "backlog_total")


def sample(source: Path, history: Path, wm: dict, process: str, at: float) -> None:
    boundary = wm.get("boundary_id")
    if not isinstance(boundary, int) or isinstance(boundary, bool):
        return
    terminal = set().union(*(wm.get(k, ()) for k in ("ingested", "rejected", "failed", "held")))
    held = set(wm.get("held", ()))
    counts = dict(live=0, backlog=0, live_held=0, backlog_held=0,
                  live_total=0, backlog_total=0)
    # Match the refinery's immutable, atomically replaced source DB contract.
    with closing(sqlite3.connect(f"file:{source}?mode=ro&immutable=1", uri=True)) as db:
        for (oid,) in db.execute("SELECT id FROM observations"):
            kind = "backlog" if oid <= boundary else "live"
            counts[kind + "_total"] += 1
            if oid in held:
                counts[kind + "_held"] += 1
            elif oid not in terminal:
                counts[kind] += 1
        # Creation-time hourly counts over the whole retention window, recounted
        # every sample so a delayed Mac→NAS sync back-fills the hours it belongs to.
        created = db.execute(
            "SELECT created_at_epoch / 3600000, count(*) FROM observations "
            "WHERE created_at_epoch >= ? AND created_at_epoch < ? GROUP BY 1",
            (int(at - RETENTION) // 3600 * 3600000, int(at + 3600) * 1000)).fetchall()
    history.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(history, timeout=2)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS samples (at REAL PRIMARY KEY, "
                   "process TEXT, boundary INTEGER, live INTEGER, backlog INTEGER, "
                   "live_held INTEGER, backlog_held INTEGER)")
        # Source totals arrived later; older rows keep NULL and cannot flag bursts.
        existing = {r[1] for r in db.execute("PRAGMA table_info(samples)")}
        for column in ("live_total", "backlog_total"):
            if column not in existing:
                db.execute(f"ALTER TABLE samples ADD COLUMN {column} INTEGER")
        db.execute(f"INSERT OR REPLACE INTO samples ({','.join(_COLUMNS)}) "
                   f"VALUES ({','.join('?' * len(_COLUMNS))})",
                   (at, process, boundary, *(counts[c] for c in _COLUMNS[3:])))
        db.execute("DELETE FROM samples WHERE at < ?", (at - RETENTION,))
        db.execute("CREATE TABLE IF NOT EXISTS created_hourly ("
                   "hour INTEGER PRIMARY KEY, count INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS created_meta ("
                   "id INTEGER PRIMARY KEY CHECK (id = 1), sampled_at REAL NOT NULL, "
                   "first_hour INTEGER NOT NULL)")
        first_hour = int(at - RETENTION) // 3600
        # The recount covers the whole window; anything older ages out.
        db.execute("DELETE FROM created_hourly")
        db.executemany("INSERT INTO created_hourly VALUES (?, ?)", created)
        db.execute("INSERT OR REPLACE INTO created_meta VALUES (1, ?, ?)", (at, first_hour))


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
            row[kind + "_rate"] = row[kind + "_burst"] = None
            if (previous and row["process"] == previous["process"]
                    and row["boundary"] == previous["boundary"]
                    and 0 < row["at"] - previous["at"] <= MAX_GAP):
                total, before = row.get(kind + "_total"), previous.get(kind + "_total")
                if total is not None and before is not None and abs(total - before) >= BURST_ROWS:
                    row[kind + "_burst"] = total - before
                    continue
                row[kind + "_rate"] = round(
                    (previous[kind] - row[kind]) * 3600 / (row["at"] - previous["at"]), 1)
        previous = row
    return rows


def read_created(history: Path) -> dict | None:
    """Hourly new-observation counts by claude-mem creation time (UTC epoch hours).

    Hours inside [first_hour, sampled_at] without a row are a real zero; None
    means the sampler has never written this table."""
    if not history.exists():
        return None
    with closing(sqlite3.connect(f"file:{history}?mode=ro", uri=True, timeout=2)) as db:
        try:
            meta = db.execute("SELECT sampled_at, first_hour FROM created_meta").fetchone()
            rows = db.execute("SELECT hour, count FROM created_hourly").fetchall()
        except sqlite3.OperationalError:
            return None
    if not meta:
        return None
    return {"sampled_at": meta[0], "first_hour": meta[1], "hours": dict(rows)}
