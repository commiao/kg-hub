"""Derive original evidence identity from campaign-owned files, never a manifest.

This is a read-only capture, not a lock capability or historical attestation.
An official consumer must supply configured roots (not request-file values),
hold the supervisor/native locks, and validate the complete disposition again.
"""
import json
from pathlib import Path
import re
import shlex

from tools.campaign_rework_evidence import capture_regular, read_regular, digest, unique_json
from tools.campaign_rework_host_receipt import one, require
from tools.campaign_rework_original_evidence import load


def capture(campaign_root, campaign_id, model, *, sessions_root, releases_root, workspace):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', campaign_id), 'invalid campaign')
    require(re.fullmatch(r'[A-Za-z0-9_-]+', model), 'invalid model')
    d, sessions, releases = map(Path, (campaign_root, sessions_root, releases_root))
    authority = {}; data = {}; refs = {}
    def take(anchor, relative):
        raw = capture_regular(anchor, relative)
        key=str(Path(anchor)/relative)
        require(key not in authority or authority[key]['sha256']==digest(raw), 'authority changed between reads')
        authority[key] = {'anchor':str(anchor), 'path':relative, 'sha256':digest(raw)}
        return raw
    c = unique_json(take(d, 'campaign.json'))
    require(c.get('id') == campaign_id and c.get('tool') == 'extract_campaign'
            and c.get('executor') == 'codex' and type(c.get('parallel')) is int and c['parallel'] == 1
            and not c.get('slot_sessions') and c.get('dispatch', {}).get('state') == 'draining'
            and model in c.get('models', {}), 'original single-lane drained campaign required')
    root = 'supervisors/' + model
    worker = unique_json(take(d, root+'/worker.json'))
    session = worker.get('session')
    require(type(session) is str and re.fullmatch(r'[A-Za-z0-9_-]+', session), 'invalid original session')
    bindings=[]
    for name in c['models']:
        require(re.fullmatch(r'[A-Za-z0-9_-]+', name), 'invalid peer model')
        bindings.append(unique_json(take(d, 'supervisors/'+name+'/worker.json')).get('session'))
    require(bindings.count(session) == 1, 'ambiguous original session ownership')
    batches=worker.get('batches')
    require(worker.get('mode') == 'quality_rework' and type(batches) is list and len(batches) == 1,
            'one original rework batch required')
    bid=batches[0]
    require(type(bid) is str and re.fullmatch(r'[A-Za-z0-9_-]+', bid), 'invalid original batch')
    b=one([b for b in c.get('batches',[]) if b.get('id')==bid], 'unique campaign batch required')
    require(b.get('model')==model and b.get('slot') is None and b.get('status')=='submitted',
            'original submitted own batch required')
    path=Path(worker.get('events','/missing'))
    try: relative=str(path.relative_to(d))
    except ValueError as exc: raise ValueError('foreign worker events') from exc
    require(re.fullmatch(re.escape(root)+r'/quality-reworks/[0-9a-f]{32}/events.jsonl',relative),
            'events outside original rework job')
    job=relative.removesuffix('/events.jsonl')
    native=unique_json(take(d,root+'/native/latest.json'))
    require(type(native.get('attempt')) is str and re.fullmatch('[0-9a-f]{32}',native['attempt'])
            and type(native.get('child_pid')) is int and native['child_pid']>0, 'missing native invocation identity')
    scope=unique_json(take(d,job+'/scope.json'))
    revision=scope.get('preflight',{}).get('revision')
    require(type(revision) is str and re.fullmatch('[0-9a-f]{40}',revision), 'invalid original release')
    release=releases/revision
    require(scope.get('preflight',{}).get('loaded_code_root')==str(release), 'release outside configured releases')
    assignment=take(d,job+'/assignment.md').decode()
    audit=one(re.findall(r"Save fresh audit material under this campaign's (audit/[A-Za-z0-9_-]+)/ directory\.",assignment),
              'one expressly assigned audit directory required')
    batch=unique_json(take(d,'batches/'+bid+'.json'))
    require(type(b.get('ids')) is list and sorted(b['ids'])==sorted(x['id'] for x in batch['items']),
            'campaign batch IDs differ from frozen source file')
    op=unique_json(take(d,'operations/'+bid+'/latest.json'))
    require(op.get('kind')=='submit' and op.get('state')=='completed'
            and type(op.get('exit_code')) is int and op['exit_code']==0
            and op.get('identity',{}).get('campaign')==campaign_id
            and op.get('identity',{}).get('batch')==bid
            and op.get('identity',{}).get('extractor_model')==model
            and op.get('identity',{}).get('extractor_tool')==c['executor']
            and op.get('identity',{}).get('run_id')==c['models'][model].get('run_id')
            and type(c['models'][model].get('run_id')) is str, 'original successful submit required')
    require(sorted((x['source_description'],x['source_obs_id']) for x in op.get('source_keys',[]))
            ==sorted((x['source_description'],x['source_obs_id']) for x in batch['items']),
            'submit source list differs from full original batch')
    paths={'events':relative,'scope':job+'/scope.json','assignment':job+'/assignment.md',
           'exit':job+'/exit-status.json','batch':'batches/'+bid+'.json',
           'worker':root+'/worker.json','native':root+'/native/latest.json'}
    for role,rel in paths.items():
        data[role]=take(d,rel);refs[role]={'anchor':'campaign','path':rel,'sha256':digest(data[role])}
    # Discovery is bounded to original date directories; capture_regular refuses
    # symlinks in every component, even if glob found one.
    candidates=list(sessions.glob('*/*/*/rollout-*-'+session+'.jsonl'))
    rollout=one(candidates,'unique original session rollout required')
    rel=str(rollout.relative_to(sessions));data['rollout']=take(sessions,rel)
    refs['rollout']={'anchor':'sessions','path':rel,'sha256':digest(data['rollout'])}
    native_rows=[unique_json(x) for x in data['events'].splitlines()]
    host_rows=[unique_json(x) for x in data['rollout'].splitlines()]
    started={r['item']['id']:r['item'] for r in native_rows if r.get('type')=='item.started'
             and r.get('item',{}).get('type')=='command_execution'}
    completed={r['item']['id'] for r in native_rows if r.get('type')=='item.completed'
               and r.get('item',{}).get('type')=='command_execution'}
    pending=one([v for k,v in started.items() if k not in completed], 'one pending original command required')
    argv=shlex.split(pending['command'])
    terminal=one([r['payload'] for r in host_rows if r.get('type')=='event_msg'
                  and r.get('payload',{}).get('type')=='item_completed'
                  and r['payload'].get('thread_id')==session
                  and r['payload'].get('item',{}).get('command')==argv], 'unique same-session host receipt required')
    turn=terminal.get('turn_id');require(type(turn) is str and turn, 'missing original turn')
    patches=[]
    for row in host_rows:
        p=row.get('payload',{})
        if p.get('type')!='custom_tool_call' or p.get('internal_chat_message_metadata_passthrough',{}).get('turn_id')!=turn: continue
        m=re.fullmatch(r'const patch=("(?:[^"\\]|\\.)*");\ntext\(await tools.apply_patch\(patch\)\)\n',p.get('input',''))
        if m and json.loads(m[1]).startswith('*** Begin Patch\n*** Add File: '+str(d/audit/'build-title-repairs.py')+'\n'):
            patches.append(p)
    patch=one(patches,'unique same-turn original builder creation required')
    for role,rel in {'quality_module':'utils/extraction_quality.py','utils_init':'utils/__init__.py','shell_entry':'tools/extract'}.items():
        data[role]=take(release,rel);refs[role]={'anchor':'release','path':rel,'sha256':digest(data[role])}
    identity={'campaign':campaign_id,'model':model,'batch':bid,'session':session,'turn':turn,'cwd':str(workspace),
              'release':revision,'job_relative':job,'audit_relative':audit,'native_attempt':native.get('attempt'),
              'native_child_pid':native.get('child_pid'),'published_entry':str(releases.parent/'current/tools/extract'),
              'builder_patch_call':patch.get('call_id')}
    manifest={'schema':'offline-rework-original-evidence-v0','identity':identity,'files':refs}
    raw=json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    anchors={'campaign':d,'release':release,'sessions':sessions}
    evidence=load(raw,digest(raw),anchors,identity)
    # Recheck all authority reads, including campaign and original operation,
    # after the slower rollout read/classification. This is still not a lease.
    for ref in authority.values():read_regular(ref['anchor'],ref['path'],ref['sha256'])
    return {'schema':'original-rework-context-capture-v1','identity':identity,'manifest':manifest,
            'manifest_sha256':digest(raw),'authority':authority,'evidence':evidence,
            'actual_process_locks_validated':False,'historical_deployment_attested':False,
            'restart_allowed':False}
