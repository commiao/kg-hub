"""Bind independently pinned originals to one failed rework attempt.

This is evidence loading only. The runtime caller must derive anchors and
expected identity from authoritative campaign/native state, supply the reviewed
manifest digest independently, and separately reconcile live locks and writes.
No submitted flag can turn its result into restart authorization.
"""
import json
from pathlib import Path
import re
from tools.campaign_rework_evidence import digest, unique_json, read_regular
from tools.campaign_rework_host_receipt import inspect, milliseconds, one, require
from tools.campaign_rework_local_disposition import review


def load(manifest_bytes, pinned_manifest_sha, anchors, expected_identity):
    require(digest(manifest_bytes) == pinned_manifest_sha, 'manifest changed')
    m = unique_json(manifest_bytes)
    require(m.get('schema') == 'offline-rework-original-evidence-v0', 'unknown manifest')
    require(set(anchors) == {'campaign', 'release', 'sessions'}, 'unknown trust anchors')
    refs = m['files']; wanted = {'events', 'scope', 'assignment', 'exit', 'batch', 'worker', 'native',
                               'rollout', 'quality_module', 'utils_init', 'shell_entry'}
    require(set(refs) == wanted, 'incomplete or extra evidence roles')
    data = {}
    for name, ref in refs.items():
        require(set(ref) == {'anchor', 'path', 'sha256'} and ref['anchor'] in anchors, 'bad file reference')
        data[name] = read_regular(anchors[ref['anchor']], ref['path'], ref['sha256'])
    identity = m['identity']
    require(identity == expected_identity, 'manifest identity differs from authoritative context')
    job = identity['job_relative']; bid = identity['batch']
    require(re.fullmatch(r'supervisors/[A-Za-z0-9_-]+/quality-reworks/[0-9a-f]{32}', job), 'invalid original job')
    require(re.fullmatch(r'[A-Za-z0-9_-]+', bid), 'invalid batch')
    require(job.split('/')[1] == identity['model'], 'job model mismatch')
    expected_paths = {'events': job + '/events.jsonl', 'scope': job + '/scope.json',
                      'assignment': job + '/assignment.md', 'exit': job + '/exit-status.json',
                      'batch': 'batches/' + bid + '.json',
                      'worker': 'supervisors/' + identity['model'] + '/worker.json',
                      'native': 'supervisors/' + identity['model'] + '/native/latest.json'}
    for role, path in expected_paths.items():
        require(refs[role]['anchor'] == 'campaign' and refs[role]['path'] == path, 'evidence role path mismatch')
    for role, path in {'quality_module':'utils/extraction_quality.py', 'utils_init':'utils/__init__.py', 'shell_entry':'tools/extract'}.items():
        require(refs[role]['anchor'] == 'release' and refs[role]['path'] == path, 'release evidence role mismatch')
    require(refs['rollout']['anchor'] == 'sessions', 'rollout outside session anchor')
    require(re.fullmatch(r'\d{4}/\d{2}/\d{2}/rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-'
                         + re.escape(identity['session']) + r'\.jsonl', refs['rollout']['path']),
            'not an original session rollout path')
    s, end, batch, worker, native = [unique_json(data[k]) for k in ('scope','exit','batch','worker','native')]
    require(s['campaign'] == identity['campaign'] and s['model'] == identity['model']
            and s['slot'] is None and s['session'] == identity['session']
            and s['batches'] == [bid] and s['new_source_quota'] == 0, 'original scope mismatch')
    require(s['assignment'] == {'path': str(Path(anchors['campaign']) / expected_paths['assignment']),
                              'sha256': refs['assignment']['sha256']}, 'original assignment binding differs')
    release = s['preflight']
    require(release['ok'] is True and release['revision'] == identity['release']
            and release['loaded_code_root'] == str(anchors['release'])
            and Path(anchors['release']).name == identity['release'], 'original deployment binding differs')
    require(worker['session'] == identity['session'] and worker['mode'] == 'quality_rework'
            and worker['batches'] == [bid] and worker['events'] == str(Path(anchors['campaign']) / expected_paths['events']),
            'worker is not the original selected rework')
    require(native['attempt'] == identity['native_attempt'] and native['state'] == 'completed'
            and native['kind'] == 'native_cli' and native['exit_code'] == 1
            and native['child_pid'] == identity['native_child_pid']
            and end['session'] == identity['session'] and end['exit_code'] == 1, 'native/exit differs')
    require(abs(milliseconds(end['ended_at']) - milliseconds(native['ended_at'])) <= 2000,
            'original native and wrapper completion differ')
    ids = [i['id'] for i in batch['items']]
    require(len(ids) == 50 and len(set(ids)) == 50 and all(type(n) is int for n in ids), 'original full50 required')
    keys = [(i['source_description'], i['source_obs_id']) for i in batch['items']]
    require(len(set(keys)) == 50, 'source identities not unique')
    audit = identity['audit_relative']
    require(re.fullmatch(r'audit/[A-Za-z0-9_-]+', audit), 'unbounded assignment audit')
    assignment = data['assignment'].decode()
    require("Save fresh audit material under this campaign's " + audit + '/ directory.' in assignment,
            'audit root not expressly in original assignment')
    rows = [unique_json(line) for line in data['rollout'].splitlines()]
    meta = one([r for r in rows if r.get('type') == 'session_meta'], 'unique original session metadata required')
    require(meta['payload']['id'] == identity['session'] and meta['payload']['cwd'] == identity['cwd'], 'foreign original session')
    scope = {'campaign_root': str(anchors['campaign']), 'audit_relative': audit, 'batch': bid,
             'session': identity['session'], 'turn': identity['turn'], 'cwd': identity['cwd'],
             'source_ids': ids, 'native_started_at': native['started_at'], 'native_ended_at': native['ended_at'],
             'campaign_id': identity['campaign'], 'published_entry': identity['published_entry']}
    native_rows = [unique_json(line) for line in data['events'].splitlines()]
    h = inspect(native_rows, rows, scope)
    patch = one([r['payload'] for r in rows if r.get('payload', {}).get('type') == 'custom_tool_call'
                 and r['payload'].get('call_id') == identity['builder_patch_call']], 'unique original patch required')
    require(patch.get('internal_chat_message_metadata_passthrough', {}).get('turn_id') == identity['turn'], 'foreign helper patch turn')
    match = re.fullmatch(r'const patch=("(?:[^"\\]|\\.)*");\ntext\(await tools.apply_patch\(patch\)\)\n', patch['input'])
    require(match, 'unsupported original patch format')
    lines = json.loads(match[1]).splitlines()
    builder = '\n'.join(x[1:] for x in lines[2:-1]) + '\n'
    historical = {k:data[k].decode() for k in ('quality_module','utils_init','shell_entry')}
    historical.update(builder=builder, original_patch_input=patch['input'])
    failed = [r['item'] for r in native_rows if r.get('type') == 'item.completed'
              and r.get('item', {}).get('type') == 'command_execution' and r['item'].get('exit_code')]
    local = [review(item, scope, historical) for item in failed]
    return {'schema':'original-rework-evidence-v1', 'manifest_sha256':pinned_manifest_sha,
            'raw_evidence_files_verified':len(data), 'host_evidence':h, 'local_evidence':local,
            'live_leases_policy_and_writes_validated':False, 'restart_allowed':False}
