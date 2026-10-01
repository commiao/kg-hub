"""Bounded, durable queue telemetry. No queue mutation or model requests."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
LINE = re.compile(r'^\[(\d{4}-\d\d-\d\d [\d:.]+)\].*\[WORKER\].*Broadcasting processing status.*queueDepth=(\d+)')


def collect(home: Path, http_json, now: float) -> dict:
    """Import hourly log samples incrementally, then sample both local workers.

    Missing workers stay unknown, never zero. Historical log points do not imply
    process continuity: net rates are only derived from live points with same PID.
    """
    state = home / '.kg-hub/state/claude-mem-queue.sqlite3'
    state.parent.mkdir(parents=True, exist_ok=True)
    cutoff = now - 7 * 86400
    current = []
    with closing(sqlite3.connect(state, timeout=5)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS samples (worker TEXT, bucket INTEGER, at REAL, depth INTEGER, pid TEXT, source TEXT, PRIMARY KEY(worker,bucket))')
        db.execute('CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER)')
        for worker, port, directory in [('legacy',37701,'.claude-mem'), ('current',37721,'.claude-mem-next')]:
            log_dir = home / directory / 'logs'
            for path in sorted(log_dir.glob('claude-mem-*.log')):
                # File dates use UTC, line timestamps use the Mac local timezone.
                if path.name < 'claude-mem-' + datetime.fromtimestamp(cutoff-86400,timezone.utc).strftime('%Y-%m-%d') + '.log':
                    continue
                stat = path.stat()
                prev = db.execute('SELECT inode,offset FROM files WHERE path=?',(str(path),)).fetchone()
                offset = prev[1] if prev and prev[0]==stat.st_ino and prev[1]<=stat.st_size else 0
                with path.open('rb') as f:
                    f.seek(offset)
                    while True:
                        start=f.tell(); raw=f.readline()
                        if not raw or not raw.endswith(b'\n'):
                            f.seek(start); break
                        m=LINE.search(raw.decode('utf-8',errors='replace'))
                        if not m: continue
                        at=datetime.fromisoformat(m[1]).astimezone().timestamp()
                        if cutoff <= at <= now:
                            db.execute('INSERT INTO samples VALUES (?,?,?,?,?,?) ON CONFLICT(worker,bucket) DO UPDATE SET at=excluded.at,depth=excluded.depth,pid=excluded.pid,source=excluded.source WHERE excluded.at>samples.at AND samples.source="log"',
                                       (worker,int(at)//3600,at,int(m[2]),None,'log'))
                    offset=f.tell()
                db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?)',(str(path),stat.st_ino,offset))
            health, health_error = http_json(f'http://127.0.0.1:{port}/api/health',timeout=4,retries=1)
            data, error = http_json(f'http://127.0.0.1:{port}/api/processing-status',timeout=4,retries=1)
            depth = (data or {}).get('queueDepth')
            valid = not error and isinstance(depth,int) and not isinstance(depth,bool) and depth>=0
            pid = str((health or {}).get('pid') or '') if not health_error else ''
            current.append({'worker':worker,'port':port,'depth':depth if valid else None,
                            'at':now,'pid':pid,'error':None if valid else '队列接口不可用或响应无效'})
            db.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?)',
                       (worker,int(now)//3600,now,depth if valid else None,pid,'live'))
        db.execute('DELETE FROM samples WHERE at<?',(cutoff,))
        db.execute('DELETE FROM files WHERE path NOT IN (SELECT path FROM files ORDER BY path DESC LIMIT 32)')
        history=[{'worker':w,'at':at,'depth':depth,'pid':pid,'source':source}
                 for w,at,depth,pid,source in db.execute('SELECT worker,at,depth,pid,source FROM samples ORDER BY at')]
    return {'sampled_at':now,'current':current,'history':history,'error':None}
