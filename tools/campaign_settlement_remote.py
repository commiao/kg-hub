"""Read-only journal/accepted-input evidence; no graph connection or model calls."""
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import uuid
import urllib.request


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def main():
    request = json.load(sys.stdin)
    keys, fields = request['keys'], request['payload_fields']
    pairs = {(k['source_description'], k['source_obs_id']) for k in keys}
    if len(pairs) != len(keys) or not 1 <= len(keys) <= 200 or not fields:
        raise ValueError('unique bounded keys and payload field list required')
    ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, 'kg-hub:' + json.dumps(
        [k['source_description'], k['source_obs_id']], ensure_ascii=False, separators=(',', ':')))) for k in keys]
    with sqlite3.connect('file:/backup/model-attempts.sqlite3?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute(
            'SELECT execution_id,source_description,source_obs_id,state,started_at,finished_at '
            'FROM task_executions WHERE task_id IN (' + ','.join('?' for _ in ids) + ')', ids)]
    accepted = []
    needles=[json.dumps(k['source_obs_id']).encode() for k in keys]
    first=[r for r in rows if (r['source_description'],r['source_obs_id'])==
           (keys[0]['source_description'],keys[0]['source_obs_id'])]
    if len(first)!=1:
        raise ValueError('unique original first acknowledged execution required')
    anchor=first[0]['execution_id'];anchor_count=0
    with open('/backup/ingest-backup.jsonl', 'rb') as stream:
        before = os.fstat(stream.fileno())
        # The sequential original submit cannot send its suffix before its
        # first acknowledged request. Require that exact journal anchor in the
        # bounded suffix and examine every subsequent accepted header. Missing
        # anchors fail closed; merely searching a recent tail proves nothing.
        start=max(0,before.st_size-512*1024*1024)
        stream.seek(start)
        if start:stream.readline()
        remaining = before.st_size-stream.tell()
        while remaining:
            raw = stream.readline(remaining)
            remaining -= len(raw)
            if not raw or not raw.endswith(b'\n'):
                raise ValueError('accepted-input snapshot has an incomplete record')
            # Stable backup headers put the source keys before the body. Skip
            # unrelated large quality contexts without parsing their bodies.
            if not any(key in raw[:4096] for key in needles):
                continue
            row = json.loads(raw)
            if (row.get('source_description'), row.get('source_obs_id')) not in pairs:
                continue
            if row.get('request_id')==anchor:anchor_count+=1
            body = row.get('input_snapshot')
            if row.get('schema_version') != 2 or not isinstance(body, dict):
                raise ValueError('exact accepted payload unavailable')
            accepted.append({k: row.get(k) for k in ('request_id','source_description','source_obs_id','ts')}
                | {'payload_sha256': digest({k: body.get(k) for k in fields})})
        after = os.fstat(stream.fileno())
        if before.st_ino != after.st_ino or after.st_size < before.st_size:
            raise ValueError('accepted-input prefix replaced during read')
    if anchor_count!=1:
        raise ValueError('bounded accepted-input evidence does not cover original sequential submission')
    with urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('KG_HUB_PORT', '8080')
                               + '/health', timeout=10) as response:
        health = json.load(response)
    print(json.dumps({'version':'source-settlement-remote-v1','keys':keys,'payload_fields':fields,
        'executions':rows,'accepted':accepted,'backup_size':before.st_size,
        'anchor_request_id':anchor,'anchor_count':anchor_count,
        'health':{k:health.get(k) for k in ('status','active_extractions','draining')},
        'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'read_only':True,'observation_bodies_returned':False}))


if __name__ == '__main__':
    main()
