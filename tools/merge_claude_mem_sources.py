#!/usr/bin/env python3
"""Append two read-only capture databases into one durable refinery identity space.

The output is state, not a disposable cache. --initialize is explicit; a missing
output on subsequent runs is an error. Commit source cursors with imported rows.
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import uuid
from pathlib import Path

TABLES = ('sdk_sessions', 'observations')

def ro(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    return db

def cols(db, table):
    return [r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')]

def insert(db, table, row):
    names = list(row)
    sql = 'INSERT INTO ' + table + ' (' + ','.join('"' + n + '"' for n in names) + ') VALUES (' + ','.join('?' for _ in names) + ')'
    return db.execute(sql, [row[n] for n in names]).lastrowid

def merge(config, output, initialize=False):
    output = Path(output)
    sources = config['sources']
    if len(sources) != 2 or len({s['name'] for s in sources}) != 2:
        raise ValueError('two distinct source names required')
    if not all(isinstance(s.get('after_id', 0), int) and s.get('after_id', 0) >= 0 for s in sources):
        raise ValueError('invalid baseline')
    if sources[0].get('after_id', 0) != 0:
        raise ValueError('legacy baseline must preserve all existing ids')
    if output.resolve() in [Path(s['path']).resolve() for s in sources]:
        raise ValueError('output cannot be a source')
    if initialize:
        with output.open('xb'):
            pass
    elif not output.is_file():
        raise ValueError('durable aggregate missing: restore it, never silently rebuild')
    readers = []
    db = None
    try:
        readers = [ro(s['path']) for s in sources]
        db = sqlite3.connect(str(output))
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        if initialize:
            for table in TABLES:
                ddl = readers[0].execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()
                if not ddl:
                    raise ValueError('missing source table ' + table)
                db.execute(ddl[0])
            db.execute('CREATE TABLE source_cursor (name TEXT PRIMARY KEY, ordinal INTEGER UNIQUE NOT NULL, path TEXT NOT NULL, baseline INTEGER NOT NULL, last_id INTEGER NOT NULL)')
            db.execute('CREATE TABLE source_observation_map (source TEXT, local_id INTEGER, aggregate_id INTEGER UNIQUE NOT NULL, PRIMARY KEY(source, local_id))')
            db.execute('CREATE TABLE source_session_map (source TEXT, local_id INTEGER, aggregate_id INTEGER UNIQUE NOT NULL, PRIMARY KEY(source, local_id))')
            for ordinal, source in enumerate(sources):
                db.execute('INSERT INTO source_cursor VALUES (?,?,?,?,?)', (source['name'], ordinal, str(Path(source['path']).resolve()), source.get('after_id', 0), source.get('after_id', 0)))
        if {r[0] for r in db.execute('SELECT name FROM source_cursor')} != {s['name'] for s in sources}:
            raise ValueError('source set changed')
        counts = {}
        for index, (source, reader) in enumerate(zip(sources, readers)):
            for table in TABLES:
                if cols(db, table) != cols(reader, table):
                    raise ValueError('schema mismatch: ' + table)
            name = source['name']
            cursor = db.execute('SELECT * FROM source_cursor WHERE name=?', (name,)).fetchone()
            if (cursor['ordinal'], cursor['path'], cursor['baseline']) != (index, str(Path(source['path']).resolve()), source.get('after_id', 0)):
                raise ValueError('source identity changed')
            high = reader.execute('SELECT coalesce(max(id),0) FROM observations').fetchone()[0]
            if high < cursor['last_id']:
                raise ValueError('source rolled back: ' + name)
            counts[name] = 0
            for original in reader.execute('SELECT * FROM observations WHERE id>? AND id<=? ORDER BY id', (cursor['last_id'], high)):
                row = dict(original)
                session = reader.execute('SELECT * FROM sdk_sessions WHERE memory_session_id=?', (row['memory_session_id'],)).fetchone()
                if session is None and index != 0:
                    raise ValueError('observation without session')
                memory = row['memory_session_id']
                if session is not None:
                    mapped = db.execute('SELECT aggregate_id FROM source_session_map WHERE source=? AND local_id=?', (name, session['id'])).fetchone()
                    memory = session['memory_session_id'] if index == 0 else str(uuid.uuid5(uuid.NAMESPACE_URL, name + ':' + session['memory_session_id']))
                    if mapped is None:
                        sr = dict(session)
                        old_sid = sr.pop('id')
                        if initialize and index == 0:
                            sr['id'] = old_sid
                        if index != 0:
                            sr['content_session_id'] = name + ':' + sr['content_session_id']
                        sr['memory_session_id'] = memory
                        new_sid = insert(db, 'sdk_sessions', sr)
                        db.execute('INSERT INTO source_session_map VALUES (?,?,?)', (name, old_sid, new_sid))
                local_id = row.pop('id')
                if initialize and index == 0:
                    row['id'] = local_id
                row['memory_session_id'] = memory
                aggregate_id = insert(db, 'observations', row)
                db.execute('INSERT INTO source_observation_map VALUES (?,?,?)', (name, local_id, aggregate_id))
                counts[name] += 1
            db.execute('UPDATE source_cursor SET last_id=? WHERE name=?', (high, name))
        db.commit()
        return counts
    except BaseException:
        if db is not None:
            db.rollback()
        raise
    finally:
        if db is not None:
            db.close()
        for reader in readers:
            reader.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--initialize', action='store_true')
    args = parser.parse_args()
    print(json.dumps(merge(json.loads(args.config.read_text()), args.output, args.initialize)))
