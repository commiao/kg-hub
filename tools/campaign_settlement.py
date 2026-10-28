"""Reconcile an exact capacity exit during sequential formal submission.

Service settlement is recorded explicitly, never as an invented HTTP response.
Only a terminal contiguous prefix with byte-identical accepted inputs may be
adopted. An unaccepted suffix is the sole part the original owner may submit.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import subprocess
from datetime import datetime

from tools import campaign_continuation as cc, campaign_supervision as supervision
from tools import extract_campaign as ec, agent_extract as ax
from utils import process_receipts as receipts
from utils.extraction_quality import digest

VERSION = 'source-settlement-v1'


def remote_snapshot(keys, payload_fields):
    from tools.nas_gate import load_settings
    script = Path(__file__).with_name('campaign_settlement_remote.py').read_text()
    docker = os.environ.get('KG_HUB_NAS_DOCKER', 'sudo -n /var/packages/ContainerManager/target/usr/bin/docker')
    command = shlex.join(shlex.split(docker) + ['exec','-i','kg-hub-server','python','-c',script])
    result = subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',
        load_settings()['ssh_target'],command], input=json.dumps({'keys':keys,'payload_fields':payload_fields}),
        capture_output=True,text=True,timeout=180)
    if result.returncode:
        raise ValueError('read-only settlement evidence unavailable: ' + result.stderr[-1000:])
    return json.loads(result.stdout)


def capacity_event(path, cid, bid, session):
    starts, pending, errors, terminals = [], {}, [], []
    for line in path.read_text().splitlines():
        row=json.loads(line); item=row.get('item',{})
        if terminals: raise ValueError('events after capacity terminal')
        if row.get('type')=='thread.started': starts.append(row.get('thread_id'))
        if row.get('type')=='error': errors.append(row.get('message'))
        if row.get('type')=='turn.failed':
            errors.append((row.get('error') or {}).get('message'));terminals.append('turn.failed')
        if row.get('type')=='turn.completed': terminals.append('turn.completed')
        if item.get('type')!='command_execution': continue
        key=item.get('id')
        if row['type']=='item.started':
            if not key or key in pending:raise ValueError('ambiguous command start')
            pending[key]=item.get('command')
        elif row['type']=='item.completed':
            if pending.pop(key,None)!=item.get('command') or item.get('exit_code')!=0:
                raise ValueError('other failed command requires its own disposition')
    if (starts!=[session] or errors!=[supervision.CAPACITY_MESSAGE]*2
            or terminals!=['turn.failed'] or len(pending)!=1):
        raise ValueError('exact capacity failure with one interrupted command required')
    command=next(iter(pending.values()))
    scope=cc.command_scope(command,(cid,bid))
    if not scope or (scope['campaign'],scope['batch'],scope['kind'])!=(cid,bid,'submit'):
        raise ValueError('only the original public formal submit may be interrupted')


def payloads(c,d,b):
    batch,results,prompt,identity,sha=cc._inputs(c,d,b)
    source={i['id']:i for i in batch['items']}
    if len(source)!=len(batch['items']) or set(source)!=set(b['ids']):
        raise ValueError('frozen source coverage differs')
    version=ec.episode_prompt_version(c,b['chain']);out=[]
    for r in results['items']:
        if not r.get('entities'):raise ValueError('empty results need a separate settlement contract')
        p=ax.build_payload(source[r['id']],r,model=b['model'],session=c['id'],executor=c['executor'],
            run_id=c['models'][b['model']]['run_id'],prompt_version=version)
        if ax.quality.requires_quality(version):
            p.update(quality_review=receipts.read(ax.quality_file(d,b['id'])),quality_context={
                'batch':batch,'results':results,'prompt_text':prompt,'prompt_version':version})
        out.append(p)
    if len(out)!=len(source) or {r['id'] for r in results['items']}!=set(source):
        raise ValueError('frozen result coverage differs')
    return batch,results,prompt,identity,sha,out


def verify_remote(remote, keys, bodies, service, original, operation):
    if (remote.get('version')!='source-settlement-remote-v1' or remote.get('keys')!=keys
            or remote.get('payload_fields')!=sorted(bodies[0]) or remote.get('read_only') is not True
            or remote.get('observation_bodies_returned') is not False
            or remote.get('health')!={'status':'ok','active_extractions':0,'draining':False}):
        raise ValueError('complete idle read-only service evidence required')
    def pair(v):return (v.get('source_description'),v.get('source_obs_id'))
    executions,accepted=remote.get('executions',[]),remote.get('accepted',[])
    ex={pair(v):v for v in executions}; ac={pair(v):v for v in accepted}
    present=[k for k in keys if service[pair(k)].get('status') is not None]
    if (len(ex)!=len(executions) or len(ac)!=len(accepted)
            or set(ex)!=set(ac) or set(ex)!={pair(k) for k in present}
            or not len(original)<len(present)<len(keys) or present!=keys[:len(present)]):
        raise ValueError('unique contiguous settled prefix and unaccepted suffix required')
    if remote.get('anchor_count')!=1 or remote.get('anchor_request_id')!=ex[pair(keys[0])].get('execution_id'):
        raise ValueError('accepted-input evidence must cover the original first acknowledged request')
    for n,(key,body) in enumerate(zip(keys,bodies)):
        p=pair(key);found=service[p]
        if n>=len(present):
            if found.get('status') is not None or found.get('episode_uuid') is not None:
                raise ValueError('suffix is not absent')
            continue
        record,request=ex[p],ac[p]
        started=datetime.fromisoformat(record['started_at'].replace('Z','+00:00'))
        original_started=datetime.fromisoformat(operation['started_at'].replace('Z','+00:00'))
        if started.tzinfo is None or original_started.tzinfo is None:
            raise ValueError('aware original execution timestamps required')
        if (found.get('status')!='ok' or not found.get('episode_uuid')
                or record.get('state')!='succeeded' or not record.get('finished_at')
                or record.get('execution_id')!=request.get('request_id')
                or request.get('payload_sha256')!=digest(body)
                or started<original_started):
            raise ValueError('settled source terminal, accepted payload or attempt identity differs')
        if n<len(original) and found['episode_uuid']!=original[n]['episode_uuid']:
            raise ValueError('original acknowledged episode changed')
    return len(present)


def reconcile(cid,bid,*,url,token,post=None,snapshot=None):
    cc.ec.tracking_guard()
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None:
        raise ValueError('settlement requires the coordinator')
    d=ec.campaign_dir(cid);c=ec.load(d);b=ec.find_batch(c,bid)
    root,worker,native,event,session,terminal,outcome=cc._worker(c,d,b['model'],ec.batch_slot(c,b))
    directory=d/'operations'/bid
    observed=receipts.status(directory)
    with receipts.idle(root),receipts.idle(native),cc._operation_lock(directory):
        if (directory/'settlement-active.json').exists():
            records=records_for(c,d,b);_finish(d,b,records)
            return {'state':'reconciled_partial','settled':len(records),'idempotent':True}
        op={**receipts.read(directory/'latest.json'),'lease_held':False}
        if op.get('attempt')!=observed.get('attempt'):
            raise ValueError('operation changed while obtaining settlement lease')
        op['state']=observed.get('state')
        batch,results,prompt,identity,sha,bodies=payloads(c,d,b)
        keys=[{k:i[k] for k in ('source_description','source_obs_id')} for i in batch['items']]
        if (b['status'] in ('submitted','released') or op.get('lease_held') is not False
                or op.get('identity')!={'campaign':cid,'batch':bid,**identity}
                or op.get('kind')!='submit' or op.get('state')!='uncertain'
                or op.get('may_have_written') is not True or op.get('input_sha256')!=sha
                or op.get('source_keys')!=keys or terminal.get('exit_code')!=1
                or not supervision.capacity_terminal(outcome,terminal)):
            raise ValueError('exact abandoned formal operation and original capacity terminal required')
        capacity_event(event,cid,bid,session)
        original=cc._prefix(c,d,b,batch,results,prompt)
        ordered=[{k:i[k] for k in ('source_description','source_obs_id')} for i in bodies]
        service=cc._states(keys,url,token,post or ax._http)
        remote=(snapshot or remote_snapshot)(ordered,sorted(bodies[0]))
        count=verify_remote(remote,ordered,bodies,service,original,op)
        if cc._inputs(ec.load(d),d,b)[-1]!=sha:
            raise ValueError('reviewed inputs changed during service reconciliation')
        recovered=[];sources={i['id']:i for i in batch['items']}
        for r in results['items'][len(original):count]:
            body=bodies[len(original)+len(recovered)]
            found=service[(body['source_description'],body['source_obs_id'])]
            recovered.append({'id':r['id'],'batch':bid,'model':b['model'],'executor':c['executor'],
                'run_id':identity['run_id'],'status':'ok','http':None,'episode_uuid':found['episode_uuid'],
                'receipt_kind':VERSION,'ts':receipts.now(),
                'payload_digest':ax.extraction_payload_digest(sources[r['id']],r,
                    ec.episode_prompt_version(c,b['chain']),prompt,batch=batch,results=results,identity=identity)})
        proof={'version':VERSION,'identity':{'campaign':cid,'batch':bid,**identity},'session':session,
            'original_attempt':op['attempt'],'original_receipt':cc.file_sha(directory/(op['attempt']+'.json')),
            'native_receipt':str(native/(terminal['attempt']+'.json')),
            'native_sha256':cc.file_sha(native/(terminal['attempt']+'.json')),
            'events':str(event),'events_sha256':cc.file_sha(event),'input_sha256':sha,
            'original':original,'recovered':recovered,'remote':remote,'service':list(service.values())}
        path=directory/('settlement-'+digest(proof)+'.json');cc._write(path,proof)
        receipts.write(directory/'settlement-active.json',{'path':str(path),'sha256':cc.file_sha(path)})
        _finish(d,b,original+recovered)
    return {'state':'reconciled_partial','acknowledged':len(original),'adopted_settled':len(recovered),
            'remaining':len(b['ids'])-count,'graph_writes':0,'original_failure_retained':True}


def records_for(c,d,b):
    directory=d/'operations'/b['id'];ref=receipts.read(directory/'settlement-active.json')
    path=Path(ref['path'])
    if not path.resolve().is_relative_to(directory.resolve()) or cc.file_sha(path)!=ref['sha256']:
        raise ValueError('settlement reference changed')
    proof=cc._read(path)
    root=ec.supervisor_dir(d,b['model'],ec.batch_slot(c,b))
    _,_,_,identity,sha,bodies=payloads(c,d,b)
    if (proof.get('version')!=VERSION or proof.get('identity')!={'campaign':c['id'],'batch':b['id'],**identity}
            or proof.get('session')!=c.get('slot_sessions',{}).get(b['model'],{}).get(str(ec.batch_slot(c,b)))
            or proof.get('input_sha256')!=sha
            or cc.file_sha(directory/(proof['original_attempt']+'.json'))!=proof['original_receipt']
            or cc.file_sha(Path(proof['native_receipt']))!=proof['native_sha256']
            or cc.file_sha(Path(proof['events']))!=proof['events_sha256']):
        raise ValueError('original settlement inputs changed')
    capacity_event(Path(proof['events']),c['id'],b['id'],proof['session'])
    ordered=[{k:i[k] for k in ('source_description','source_obs_id')} for i in bodies]
    service={(v['source_description'],v['source_obs_id']):v for v in proof['service']}
    count=verify_remote(proof['remote'],ordered,bodies,service,proof['original'],
                        receipts.read(directory/(proof['original_attempt']+'.json')))
    records=proof['original']+proof['recovered']
    if len(records)!=count or [r['id'] for r in records]!=[r['id'] for r in cc._inputs(c,d,b)[1]['items'][:count]]:
        raise ValueError('settlement result coverage differs')
    lines=[json.loads(s) for s in (d/'log.jsonl').read_text().splitlines()]
    for r in records:
        existing=[x for x in lines if x.get('batch')==b['id'] and x.get('id')==r['id']]
        if existing!=[r] and not (not existing and r in proof['recovered']):
            raise ValueError('original formal acknowledgement or adopted record changed')
    return records


def _finish(d,b,records):
    directory=d/'operations'/b['id'];ref=receipts.read(directory/'settlement-active.json');proof=cc._read(Path(ref['path']))
    # The exact source set is guarded by the operation lease; append each new
    # settled record once. Crash after append resumes without inventing HTTP200.
    with ec.locked(d):
        if records_for(ec.load(d),d,b)!=records:
            raise ValueError('settlement evidence changed before adoption')
        lines=[json.loads(s) for s in (d/'log.jsonl').read_text().splitlines()]
        for r in proof['recovered']:
            existing=[x for x in lines if x.get('batch')==b['id'] and x.get('id')==r['id']]
            if existing and existing!=[r]:raise ValueError('formal ledger changed before settlement adoption')
            if not existing:
                with (d/'log.jsonl').open('a') as stream:
                    stream.write(json.dumps(r,ensure_ascii=False)+'\n');stream.flush();os.fsync(stream.fileno())
        op=receipts.read(directory/'latest.json')
        if op.get('attempt')!=proof['original_attempt']:raise ValueError('operation advanced before adoption')
        op.update(state='reconciled_partial',requires_reconciliation=False,settlement_continuation=ref)
        receipts.write(directory/'latest.json',op)


def valid(c,d,b,operation):
    try:
        ref=receipts.read(d/'operations'/b['id']/'settlement-active.json')
        proof=cc._read(Path(ref['path']))
        return (operation.get('state')=='reconciled_partial' and not operation.get('requires_reconciliation')
            and operation.get('attempt')==proof['original_attempt']
            and operation.get('settlement_continuation')==ref and bool(records_for(c,d,b)))
    except (OSError,KeyError,TypeError,ValueError):return False
