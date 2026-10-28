"""Explicitly quarantine an unsubmitted released batch before future own-slot work.

No old quality is accepted, no old operation is retried and no source is forced
back into a claim. Future claims still perform global ingestion checks and must
use the separately enabled one-source decision ledger.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex
import uuid

from tools import extract_campaign as ec
from tools.campaign_continuation import _worker
from tools.campaign_rework_completion import _commands, _shell
from tools.campaign_ownership_recovery import _ref, _read, _contract
from tools.campaign_rework_maintenance import DENIED
from utils import process_receipts as receipts
from utils.extraction_quality import digest

VERSION='released-quality-quarantine-v1'


def _preparation(command,cid,bid):
    """A failed local preparation may be discarded, never a failed graph write."""
    shell=_shell(command)
    bodies=re.findall(r"(?:python3 -|cat > [^\n]+) <<'PY'\n(.*?)\nPY(?:\n|$)",shell,re.S)
    remaining=re.sub(r"(?:python3 -|cat > [^\n]+) <<'PY'\n.*?\nPY(?:\n|$)",'',shell,flags=re.S)
    remaining=remaining.replace('"${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract"',
                                shlex.quote(str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser())))
    helpers=[]
    for line in remaining.splitlines():
        if not line.strip():continue
        args=shlex.split(line)
        if args and args[0]=='python3' and len(args)==2:
            p=Path(args[1]).resolve(strict=True)
            if not p.is_relative_to(Path('/private/tmp')) or p.suffix!='.py':raise ValueError('private preparation helper required')
            bodies.append(p.read_text());helpers.append(_ref(p))
        else:
            entry=str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser())
            if args[:3]==[entry,'campaign','prepare'] and args[3:]==['--id',cid,'--batch',bid]:continue
            if args[:3]==[entry,'campaign','status'] and args[3:]==['--id',cid,'--json']:continue
            if args[:6]==[entry,'campaign','status','--id',cid,'--json'] and len(args)==8 and args[6]=='>':
                p=Path(args[7]).resolve()
                if p.is_relative_to(Path('/private/tmp')) and p.suffix=='.json':continue
            raise ValueError('only discarded own-batch preparation or failed read diagnostic qualifies')
    for body in bodies:
        tree=ast.parse(body)
        paths={};assignments=[n for n in ast.walk(tree) if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name)]
        def path(n):
            if isinstance(n,ast.Name):return paths.get(n.id)
            if isinstance(n,ast.Constant) and isinstance(n.value,str):return Path(n.value)
            if isinstance(n,ast.Call) and ast.unparse(n.func)=='pathlib.Path' and len(n.args)==1:
                value=path(n.args[0]);return value.resolve() if value is not None else None
            if isinstance(n,ast.Call) and ast.unparse(n.func)=='str' and len(n.args)==1:return path(n.args[0])
            if isinstance(n,ast.BinOp) and isinstance(n.op,(ast.Div,ast.Add)):
                left=path(n.left)
                if left is not None and isinstance(n.right,ast.Constant) and isinstance(n.right.value,str):
                    return (left/n.right.value).resolve() if isinstance(n.op,ast.Div) else Path(str(left)+n.right.value).resolve()
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='with_suffix' and len(n.args)==1 and isinstance(n.args[0],ast.Constant):
                left=path(n.func.value)
                if left is not None:return left.with_suffix(n.args[0].value).resolve()
        for _ in range(len(assignments)+1):
            for n in assignments:
                value=path(n.value)
                if value is not None:paths[n.targets[0].id]=value
        def bounded(p,writing=False):
            if p is None:return False
            p=p.resolve();d=ec.campaign_dir(cid).resolve()
            if any(x.startswith('.env') for x in p.parts):return False
            if p.is_relative_to(Path('/private/tmp')) and p.suffix in ('.json','.py'):return True
            if not writing and p in (d/'campaign.json',d/'log.jsonl'):return True
            dirs=(d/'results',d/'reviews') if writing else (d/'results',d/'reviews',d/'batches')
            return any(p.parent==directory for directory in dirs) and (p.name==bid+'.json' or p.name.startswith(bid+'.'))
        for n in ast.walk(tree):
            if isinstance(n,ast.Import) and any(a.name not in {'json','pathlib','shutil'} or a.asname for a in n.names):raise ValueError('failed preparation cannot call external processes/network')
            if isinstance(n,(ast.ImportFrom,ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Lambda)):
                raise ValueError('failed preparation must be bounded local data manipulation')
            if isinstance(n,ast.Attribute) and n.attr.startswith('_'):raise ValueError('dynamic failed preparation cannot qualify')
            if isinstance(n,ast.Call):
                name=ast.unparse(n.func)
                pure={'json.loads','json.dumps','pathlib.Path','shutil.copy2','next','print','len','sum','zip','RuntimeError'}
                local={'read_text','write_text','with_suffix','append','pop','update','index'}
                if name not in pure and not (isinstance(n.func,ast.Attribute) and n.func.attr in local):raise ValueError('failed preparation contains an executable operation')
                if isinstance(n.func,ast.Attribute) and n.func.attr in ('read_text','write_text'):
                    if not bounded(path(n.func.value),n.func.attr=='write_text'):raise ValueError('failed preparation accesses outside its released batch')
                if name=='shutil.copy2':
                    if len(n.args)!=2 or not bounded(path(n.args[0])) or not bounded(path(n.args[1]),True):raise ValueError('only released local artifacts may be copied')
    return helpers


def facts(c,d,model,slot,bid,quarantine,*,locked=False,operation_locked=False):
    root,worker,native,event,sid,terminal,outcome=_worker(c,d,model,slot,locked=locked)
    b=ec.find_batch(c,bid);ids=set(b['ids'])
    if (terminal.get('state')!='completed' or terminal.get('exit_code')!=0 or outcome.get('terminal')!='turn.completed'
            or worker.get('mode')=='quality_rework'
            or outcome.get('recovery_kind')!='quality_process_violation' or outcome.get('hard_stop') or outcome.get('approval_refused')
            or b['model']!=model or ec.batch_slot(c,b)!=slot or b['status']!='released' or set(b.get('released_ids',[]))!=ids):
        raise ValueError('only exact native0 own released unsubmitted quality stop qualifies')
    if c.get('source_decisions',{}).get('version')!='source-decisions-v1':raise ValueError('future per-source decision enforcement must already be enabled')
    if any(x['model']==model and ec.batch_slot(c,x)==slot and x['status'] not in ('submitted','released') for x in c['batches']):
        raise ValueError('another unresolved original batch must not be bypassed')
    if any(v in ('ok','empty') for v in ec.final_statuses(d,bid,ids).values()):raise ValueError('submitted source quality debt requires rework, not quarantine')
    op=({'lease_held':False} if operation_locked else receipts.status(d/'operations'/bid))
    if op.get('lease_held') is not False:raise ValueError('original operation still live')
    operations=[]
    for p in sorted((d/'operations'/bid).glob('*.json')):
        obj=receipts.read(p)
        if obj.get('attempt') and obj.get('kind')!='dryrun':raise ValueError('unknown or writing operation receipt cannot qualify')
        if obj.get('kind') in ('submit','dryrun'):
            if (obj['kind']!='dryrun' or obj.get('state')!='completed' or obj.get('exit_code')!=0
                    or obj.get('may_have_written') is not False):raise ValueError('all operations must be completed non-writing dryruns')
            operations.append(_ref(p))
    if not operations:raise ValueError('original non-writing operation receipts required')
    rows=_commands(event,sid);reports=[]
    for line in event.read_text().splitlines():
        row=json.loads(line)
        if row.get('type')=='error':raise ValueError('native/provider error needs its own recovery')
        item=row.get('item',{})
        if item.get('type')=='agent_message':
            try:report=json.loads(item.get('text',''))
            except ValueError:continue
            if isinstance(report,dict) and report.get('recovery_kind'):reports.append(report)
    if len(reports)!=1 or reports[0].get('batch')!=bid or reports[0].get('model')!=model or reports[0].get('submitted')!={'ok':0,'empty':0,'error':0}:
        raise ValueError('one original zero-submission quality stop report required')
    failed=[]
    for row in rows:
        if not row['exit_code']:continue
        if row['exit_code'] in (75,76) or DENIED.search(row['output']):raise ValueError('real refusal is not a discarded preparation')
        helpers=_preparation(row['command'],c['id'],bid)
        failed.append({'event':row['id'],'command':row['command'],'command_sha256':digest(row['command']),
                       'exit_code':row['exit_code'],'output_sha256':digest(row['output']),'helpers':helpers})
    q=_read(quarantine)
    if (q.get('batch')!=bid or q.get('model')!=model or q.get('slot')!=slot or q.get('session')!=sid
            or set(q.get('source_ids',[]))!=ids
            or q.get('formal_ok')!=0 or q.get('official_empty')!=0
            or q.get('public_release',{}).get('exit_code')!=0 or q.get('public_release',{}).get('released')!=len(ids)
            or q.get('public_release',{}).get('reply',{}).get('batch')!=bid
            or q.get('invalid_quality_evidence',{}).get('entire_batch_quarantined') is not True
            or q.get('invalid_quality_evidence',{}).get('invalid_sheet_accepted_as_pass') is not False):
        raise ValueError('explicit full-batch invalid-quality quarantine and actual public release required')
    static=[]
    for ref in q.get('refs',[]):
        p=Path(ref['file']).resolve(strict=True)
        if any(x.startswith('.env') for x in p.parts) or not (p.is_relative_to(d.resolve()) or p.is_relative_to(Path('/private/tmp'))):
            raise ValueError('quarantine evidence must stay in private task scope')
        # These are historical snapshots; live protected fields are checked above.
        # Formal logs append and dispatch legitimately changes after approval.
        if p in ((d/'campaign.json').resolve(),(d/'log.jsonl').resolve()):continue
        value=_ref(p)
        if value['sha256']!=ref['sha256']:raise ValueError('original quarantine evidence changed')
        static.append(value)
    paths={Path(x['path']) for x in static}
    required={root.resolve()/'worker.json',(native/'latest.json').resolve(),event,(d/'batches'/f'{bid}.json').resolve(),(d/'results'/f'{bid}.json').resolve()}
    if not required<=paths or not any(p.is_relative_to((d/'reviews').resolve()) and p.name.startswith(bid+'.') for p in paths):
        raise ValueError('bind original worker/native/events/batch/results and invalid quality sheet')
    contract=_contract(c,model);contract.pop('dispatch',None)
    return {'version':VERSION,'campaign':c['id'],'model':model,'slot':slot,'session':sid,'batch':bid,'contract':contract,
            'source_decisions':c['source_decisions'],'released_batch':b,'worker':_ref(root/'worker.json'),
            'native':_ref(native/'latest.json'),'events':_ref(event),'batch_file':_ref(d/'batches'/f'{bid}.json'),
            'quarantine':_ref(quarantine),'quarantine_static':static,'operations':operations,'failed':failed,
            'old_quality_accepted':False,'old_batch_retried':False,'new_source_allowance':0}


def preview(cid,model,slot,bid,*,quarantine,out):
    ec.tracking_guard()
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None:raise ValueError('coordinator only')
    d=ec.campaign_dir(cid);c=ec.load(d)
    if (c.get('dispatch') or {}).get('state')!='draining':raise ValueError('preview quarantine while claims remain drained')
    value=facts(c,d,model,slot,bid,quarantine)
    output=Path(out).resolve()
    protected={Path(x['path']) for x in [value['quarantine'],*value['quarantine_static'],*value['operations']]}
    if output in protected or output.is_relative_to(d.resolve()) and not output.is_relative_to((d/'audit').resolve()):
        raise ValueError('review output must not overwrite original campaign evidence')
    sheet={'version':VERSION,'evidence':value,'evidence_sha256':digest(value),'reviewer':'','reason':'',
           'failed_reviews':[{'event':x['event'],'command_sha256':x['command_sha256'],'verdict':'pending','note':''} for x in value['failed']],
           'quarantine_verdict':'pending','quarantine_note':''}
    receipts.write(out,sheet)
    return {'review':str(out),'failed':len(value['failed']),'old_quality_accepted':False,'approval':'pending'}


def apply(cid,*,review):
    ec.tracking_guard()
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None:raise ValueError('coordinator only')
    doc=_read(review);old=doc['evidence'];d=ec.campaign_dir(cid);root=ec.supervisor_dir(d,old['model'],old['slot'])
    with receipts.idle(root),receipts.idle(root/'native'),receipts.idle(d/'operations'/old['batch']):
        c=ec.load(d)
        if (c.get('dispatch') or {}).get('state')!='draining':raise ValueError('apply quarantine while claims remain drained')
        now=facts(c,d,old['model'],old['slot'],old['batch'],old['quarantine']['path'],locked=True,operation_locked=True)
        if doc.get('version')!=VERSION or old!=now or doc.get('evidence_sha256')!=digest(now):raise ValueError('released quality review is stale')
        if not str(doc.get('reviewer','')).strip() or len(str(doc.get('reason','')).strip())<10 or doc.get('quarantine_verdict')!='discarded_invalid_no_formal_write' or len(str(doc.get('quarantine_note','')).strip())<20:
            raise ValueError('explicit quarantine judgement and coordinator required')
        reviews=doc.get('failed_reviews',[])
        if len(reviews)!=len(now['failed']) or len({x['event'] for x in reviews})!=len(reviews):raise ValueError('every failed preparation must be reviewed')
        for failure in now['failed']:
            rows=[x for x in reviews if x.get('event')==failure['event']]
            if len(rows)!=1 or rows[0].get('command_sha256')!=failure['command_sha256'] or rows[0].get('verdict')!='discarded_with_released_batch' or len(str(rows[0].get('note','')).strip())<20:
                raise ValueError('each original failure needs an exact non-retry disposition')
        proof={'version':VERSION,'review':_ref(review),'evidence':now,'reviewer':doc['reviewer'],'reason':doc['reason'],'at':receipts.now()}
        proof['sha256']=digest(proof)
        folder=root/'released-quality-quarantines'/uuid.uuid4().hex;folder.mkdir(parents=True)
        receipts.write(folder/'proof.json',proof);receipts.write(root/'released-quality-active.json',_ref(folder/'proof.json'))
    return {'state':'quarantined','batch':old['batch'],'same_session_only':True,'old_quality_accepted':False,'dispatch_resumed':False}


def valid(c,d,model,slot,*,locked=False,operation_locked=False):
    try:
        root=ec.supervisor_dir(d,model,slot);ref=receipts.read(root/'released-quality-active.json');p=Path(ref['path']).resolve(strict=True)
        if not p.is_relative_to((root/'released-quality-quarantines').resolve()) or _ref(p)!=ref:return False
        proof=_read(p)
        if proof['version']!=VERSION or proof['sha256']!=digest({k:v for k,v in proof.items() if k!='sha256'}) or _ref(proof['review']['path'])!=proof['review']:return False
        old=proof['evidence']
        return (old['campaign'],old['model'],old['slot'])==(c['id'],model,slot) and facts(c,d,model,slot,old['batch'],old['quarantine']['path'],locked=locked,operation_locked=operation_locked)==old
    except (KeyError,ValueError,OSError,TypeError,SyntaxError,receipts.Busy):return False
