"""Explicit owner continuations for settled local protocol and read-only faults.

Does not clear approval, NAS refusal, a repair-write refusal or unknown writes.
The original failure and source review remain independent immutable evidence.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess

from tools import campaign_continuation as cc, extract_campaign as ec, agent_extract as ax
from tools import campaign_supervision as supervision
from tools.campaign_rework_completion import _shell
from utils import process_receipts as receipts
from utils.extraction_quality import digest


def commands(event):
    pending={};done=[]
    for line in event.read_text().splitlines():
        r=json.loads(line);i=r.get('item',{})
        if i.get('type')!='command_execution':continue
        if r['type']=='item.started':
            if not i.get('id') or i['id'] in pending:raise ValueError('unique command start required')
            pending[i['id']]=i.get('command')
        elif r['type']=='item.completed':
            if pending.pop(i.get('id'),None)!=i.get('command') or type(i.get('exit_code')) is not int:
                raise ValueError('complete original command evidence required')
            done.append(i)
    if pending:raise ValueError('unfinished tool command cannot use local continuation')
    return done


def reconcile(cid,model,slot,*,reason,url,token,bid=None,repair=None,reviewer=None,preflight=None,
              prior_backend=None,prior_bundle=None,probe=None):
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None or not reason.strip():
        raise ValueError('explicit coordinator disposition required')
    ec.tracking_guard();d=ec.campaign_dir(cid);c=ec.load(d)
    root,worker,native,event,session,terminal,outcome=cc._worker(c,d,model,slot)
    with receipts.idle(root),receipts.idle(native):
        if (terminal.get('exit_code')!=0 or outcome.get('terminal')!='turn.completed'
                or outcome.get('hard_stop') or outcome.get('approval_refused')):
            raise ValueError('completed original owner without approval/NAS refusal required')
        rows=commands(event);operations={};inputs={};registration=None
        own=[b for b in c['batches'] if b['model']==model and ec.batch_slot(c,b)==slot]
        for b in own:
            op=receipts.status(d/'operations'/b['id'])
            if op.get('lease_held') or op.get('requires_reconciliation') or op.get('state')=='uncertain':
                raise ValueError('local continuation never settles an unknown operation')
            if op.get('exit_code') in (75,76):raise ValueError('NAS refusal requires separate reconciliation')
        if prior_backend is not None:
            b=ec.find_batch(c,bid)
            core_failure=prior_bundle is not None
            expected_kind='infrastructure_failed' if core_failure else 'business_permission_denied'
            if b not in own or b['status'] in ('submitted','released') or outcome.get('recovery_kind')!=expected_kind:
                raise ValueError('exact pending owner stopped at stale delegation boundary required')
            failed=[r for r in rows if r['exit_code']]
            expected='llm-eval: bundle core differs' if core_failure else 'delegated campaign backend identity differs'
            if not failed or failed[-1].get('aggregated_output','').strip()!=expected:
                raise ValueError('exact original delegated backend rejection required')
            args=shlex.split(_shell(failed[-1]['command']).splitlines()[-1])
            entry=str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser())
            if args and args[0]=='${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract':args[0]=entry
            operation='operation-status' if core_failure else 'prepare'
            if args[:7]!=[entry,'campaign',operation,'--id',cid,'--batch',bid] or not (
                    len(args)==7 or len(args)==9 and args[7]=='>' and Path(args[8]).is_absolute()):
                raise ValueError('literal original public prepare boundary required')
            if any(json.loads(line).get('batch')==bid for line in (d/'log.jsonl').read_text().splitlines()):
                raise ValueError('delegation recovery never settles formal writes')
            env={**os.environ,'LLM_EVAL_DELEGATED_BACKEND':prior_backend,
                 'LLM_EVAL_ACTIVE_BUNDLE':str(Path('~/.local/share/llm-eval/active-bundle.json').expanduser())}
            env.pop('LLM_EVAL_BACKEND_RESELECTED',None)
            if core_failure:
                old=Path(prior_bundle).resolve(strict=True)
                if not old.is_relative_to(Path('~/.local/share/llm-eval/compositions').expanduser().resolve()):
                    raise ValueError('known installed prior composition required')
                metadata=json.loads(old.read_text())
                if metadata.get('delegates',{}).get('kg_hub.entity_extract',{}).get('commit')!=prior_backend:
                    raise ValueError('prior composition/backend binding differs')
                env['PYTHONPATH']=str(next((old.parent/'venv/lib').glob('python*/site-packages')))
                inputs[str(old)]=cc.file_sha(old)
            result=(probe or subprocess.run)([entry,'campaign','operation-status','--id',cid,'--batch',bid],
                 env=env,capture_output=True,text=True,timeout=30)
            if result.returncode!=0 or json.loads(result.stdout).get('batch')!=bid:
                raise ValueError('published stale-epoch re-selection readback required')
            inputs[str(Path(entry).resolve().parent/'extract_cli.py')]=cc.file_sha(Path(entry).resolve().parent/'extract_cli.py')
            inputs[str(Path(entry).resolve())]=cc.file_sha(Path(entry).resolve())
            kind='delegation_epoch_recovered'
        elif repair is not None:
            b=ec.find_batch(c,bid)
            if b not in own or b['status']!='submitted' or outcome.get('recovery_kind')!='business_permission_denied':
                raise ValueError('exact submitted owner stopped at production decision entry required')
            failed=[r for r in rows if r['exit_code']]
            if not failed:raise ValueError('original decision rejection missing')
            rejected=failed[-1];args=shlex.split(_shell(rejected['command']))
            entry=str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser())
            if args and args[0]=='${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract':
                args[0]=entry
            if (args[:3]!=[entry,'campaign','decision'] or len(args)!=11
                    or args[3:7]!=['--id',cid,'--batch',bid] or args[7]!='--file' or args[9]!='--reviewer'
                    or rejected['exit_code']!=1
                    or not rejected.get('aggregated_output','').rstrip().endswith(
                        'ValueError: one-source decisions require an opted-in open batch')):
                raise ValueError('only the exact closed production-decision rejection qualifies')
            from tools.campaign_decisions import record_repair
            registration=record_repair(cid,bid,repair,reviewer=reviewer)
            source_id=receipts.read(Path(registration['path']))['source_id']
            original=receipts.read(Path(args[8]))
            if original.get('id')!=source_id or original.get('verdict')!='ok':
                raise ValueError('rejected source differs from reviewed repair')
            inputs[str(Path(args[8]).resolve())]=cc.file_sha(Path(args[8]))
            inputs[registration['path']]=cc.file_sha(Path(registration['path']))
            kind='repair_decision_recovered'
        else:
            pending=[b for b in own if b['status'] not in ('submitted','released')]
            if len(pending)!=1 or outcome.get('recovery_kind')!='infrastructure_failed':
                raise ValueError('one pending read-only infrastructure preview required')
            b=pending[0];op=receipts.status(d/'operations'/b['id'])
            statuses=op.get('by_status',{})
            if (op.get('kind')!='dryrun' or op.get('state')!='completed' or op.get('exit_code')!=0
                    or op.get('may_have_written') is not False or not statuses.get('http_502')
                    or set(statuses)-{'dry_run','empty','http_502'}
                    or sum(statuses.values())!=len(b['ids'])):
                raise ValueError('exact incomplete read-only HTTP502 preview required')
            if any(r.get('exit_code') in (75,76) or any(s in r.get('aggregated_output','') for s in (
                    'This action was rejected due to unacceptable risk','PermissionError:',
                    'batch belongs to a different worker slot')) for r in rows):
                raise ValueError('real permission/NAS refusal cannot use preview continuation')
            _,_,_,_,sha=cc._inputs(c,d,b)
            if op.get('input_sha256')!=sha:raise ValueError('original preview input changed')
            kind='readonly_preview_recovered'
        operations[b['id']]=digest(receipts.status(d/'operations'/b['id']))
        # Service-only readiness, no paid model admission/probe.
        flight=(preflight or ax.runtime_preflight)(url,token)
        if flight.get('ok') is not True:raise ValueError('service readiness required')
        value={'version':'local-owner-continuation-v1','campaign':cid,'model':model,'slot':slot,
            'session':session,'kind':kind,'batch':b['id'],'reason':reason,'service_readiness':flight,
            'worker_sha256':cc.file_sha(root/'worker.json'),'native_sha256':cc.file_sha(native/'latest.json'),
            'events':str(event),'events_sha256':cc.file_sha(event),'operations':operations,'inputs':inputs,
            'frozen_source_sha256':cc.file_sha(d/'batches'/(b['id']+'.json')),
            'rules_sha256':digest(ec.batch_rules_text(d,c,b))}
        path=root/('local-continuation-'+digest(value)+'.json')
        if not path.exists():cc._write(path,value)
        receipts.write(root/'local-continuation-active.json',{'path':str(path),'sha256':cc.file_sha(path)})
    return {'kind':kind,'model':model,'slot':slot,'session':session,'registration':registration,
            'original_failure_retained':True,'graph_writes':0}


def valid(c,d,model,slot,*,locked=False):
    try:
        root,worker,native,event,session,terminal,outcome=cc._worker(c,d,model,slot,locked=locked)
        ref=receipts.read(root/'local-continuation-active.json');path=Path(ref['path'])
        if not path.resolve().is_relative_to(root.resolve()) or cc.file_sha(path)!=ref['sha256']:return False
        proof=cc._read(path);b=ec.find_batch(c,proof['batch'])
        if (proof.get('version')!='local-owner-continuation-v1' or proof.get('campaign')!=c['id']
                or proof.get('model')!=model or proof.get('slot')!=slot or proof.get('session')!=session
                or proof.get('kind') not in ('repair_decision_recovered','readonly_preview_recovered','delegation_epoch_recovered')
                or cc.file_sha(root/'worker.json')!=proof['worker_sha256']
                or cc.file_sha(native/'latest.json')!=proof['native_sha256']
                or str(event)!=proof['events'] or cc.file_sha(event)!=proof['events_sha256']
                or cc.file_sha(d/'batches'/(b['id']+'.json'))!=proof['frozen_source_sha256']
                or digest(ec.batch_rules_text(d,c,b))!=proof['rules_sha256']):return False
        for bid,expected in proof['operations'].items():
            raw=receipts.read(d/'operations'/bid/'latest.json') or {'state':'not_started'}
            if digest({**raw,'lease_held':False})!=expected:return False
        return all(cc.file_sha(Path(p))==sha for p,sha in proof['inputs'].items())
    except (OSError,KeyError,TypeError,ValueError):return False
