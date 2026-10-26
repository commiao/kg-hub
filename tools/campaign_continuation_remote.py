"""Read-only, source-scoped NAS evidence. Executed over the existing SSH route.

Never imports a journal constructor (which would migrate SQLite), reads secrets,
returns observation bodies, or writes the graph/filesystem.
"""
import datetime
import json
import os
import re
import sqlite3
import sys
import urllib.request
import uuid


def main():
    request = json.load(sys.stdin)
    keys, anchor = request['keys'], tuple(request['anchor'])
    pairs = {(x['source_description'], x['source_obs_id']) for x in keys}
    if len(pairs) != len(keys) or not 1 <= len(keys) <= 200:
        raise ValueError('unique bounded source keys required')
    ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, 'kg-hub:' + json.dumps(
        [x['source_description'], x['source_obs_id']], ensure_ascii=False, separators=(',', ':')))) for x in keys]
    with sqlite3.connect('file:/backup/model-attempts.sqlite3?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute(
            'SELECT execution_id,source_description,source_obs_id,state,started_at,finished_at '
            'FROM task_executions WHERE task_id IN (' + ','.join('?' for _ in ids) + ')', ids)]
    anchor_rows = [r for r in rows if (r['source_description'], r['source_obs_id']) == anchor]
    if len(anchor_rows) != 1:
        raise ValueError('unique original anchor execution required')
    request_id = anchor_rows[0]['execution_id']
    fd = os.open('/backup/ingest-backup.jsonl', os.O_RDONLY)
    with os.fdopen(fd, 'rb') as f:
        before = os.fstat(f.fileno())
        start = max(0, before.st_size - 128 * 1024 * 1024)
        f.seek(start)
        if start:
            f.readline()
        data = f.read(before.st_size - f.tell())
        after = os.fstat(f.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('backup changed during source evidence read; retry read-only verification')

    def field(head, key):
        m = re.search(rb'"' + key.encode() + rb'"\s*:\s*("(?:\\.|[^"\\])*")', head)
        return json.loads(m.group(1)) if m else None

    anchors, following, invalid = [], [], 0
    for raw in data.splitlines():
        head = raw[:4096]
        sd, sid = field(head, 'source_description'), field(head, 'source_obs_id')
        rid = field(head, 'request_id')
        if rid == request_id:
            # Only the exact anchor's provenance is returned, never its body.
            snapshot = json.loads(raw).get('input_snapshot') or {}
            anchors.append({k: snapshot.get(k) for k in (
                'source_description', 'source_obs_id', 'extractor_model', 'extractor_tool',
                'extractor_session', 'run_id')})
        if anchors:
            if sd is None or sid is None:
                invalid += 1
            elif (sd, sid) in pairs:
                following.append({'source_description': sd, 'source_obs_id': sid,
                                  'request_id': rid})
    with urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('KG_HUB_PORT', '8080')
                                + '/health', timeout=10) as response:
        health = json.load(response)
    print(json.dumps({'version': 'partial-upload-remote-v1',
        'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'keys': keys,
        'executions': rows, 'anchor_request_id': request_id, 'anchors': anchors,
        'backup_size': before.st_size, 'backup_mtime_ns': before.st_mtime_ns,
        'post_anchor_sources': following, 'unparsed_post_anchor_headers': invalid,
        'health': {k: health.get(k) for k in ('status', 'active_extractions', 'draining')},
        'read_only': True, 'observation_bodies_returned': False}))


if __name__ == '__main__':
    main()
