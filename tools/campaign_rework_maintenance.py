"""Reviewed disposition of a completed owner's read-only maintenance failure.

This never changes infrastructure_failed to clean or permits a failed write.
The coordinator reviews every failed command and its frozen local helpers;
fresh service preflight is still required immediately before native startup.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex

from tools import extract_campaign as ec, campaign_supervision as supervision
from tools.campaign_rework_completion import _commands, _shell
from tools.campaign_ownership_recovery import _contract, _read, _ref, _time
from utils import process_receipts as receipts
from utils.extraction_quality import digest

VERSION = 'rework-maintenance-review-v1'
TRANSPORT = ('RemoteDisconnected', 'ConnectionRefusedError')
DENIED = re.compile(r'permission denied|approval.{0,20}(reject|denied)|HTTP Error (401|403|429)|'
                    r'batch belongs to a different worker slot|quality rework cannot', re.I)
WRITE = re.compile(r'\b(?:repair|nodes)\s+apply\b|\bcampaign\s+(?:claim|submit|release|cost|overhead)\b|'
                   r'\b(?:curl|wget|ssh|redis-cli|eval|exec)\b|\b(?:POST|PUT|PATCH|DELETE)\b')


def _local_script(text, audit, argv=()):
    """Reject network/process execution in reviewed failed local audit helpers.

    This is an execution boundary, not an automatic semantic approval. Exact
    helper bytes and the complete failed shell remain visible in the sheet.
    """
    audit=Path(audit).resolve()
    tree = ast.parse(text)
    imports = {'json', 'pathlib', 'sys', 'hashlib', 'os', 'copy'}
    aliases={}
    paths,segments,objects={},{},{}
    numeric={n.targets[0].id for n in ast.walk(tree) if isinstance(n,ast.Assign) and len(n.targets)==1
             and isinstance(n.targets[0],ast.Name) and isinstance(n.value,ast.Call) and ast.unparse(n.value.func)=='int'}
    for n in ast.walk(tree):
        if isinstance(n,ast.Assert) and isinstance(n.test,ast.Call) and ast.unparse(n.test.func)=='isinstance' and len(n.test.args)==2:
            if isinstance(n.test.args[0],ast.Name) and ast.unparse(n.test.args[1])=='int':numeric.add(n.test.args[0].id)
    def component(n):
        if isinstance(n,ast.Constant) and isinstance(n.value,str):return n.value
        if isinstance(n,ast.Name):return segments.get(n.id)
        if isinstance(n,ast.Subscript) and isinstance(n.value,ast.Name) and isinstance(n.slice,ast.Constant):
            value=objects.get(n.value.id,{}).get(n.slice.value)
            if isinstance(value,str):return value
        if isinstance(n,ast.Call) and ast.unparse(n.func)=='str' and len(n.args)==1 and isinstance(n.args[0],ast.Name) and n.args[0].id in numeric:return '0'
        if isinstance(n,ast.JoinedStr):
            values=[]
            for x in n.values:
                if isinstance(x,ast.Constant):values.append(str(x.value))
                elif isinstance(x,ast.FormattedValue) and isinstance(x.value,ast.Name) and x.value.id in numeric:values.append('0')
                else:return None
            return ''.join(values)
    for n in ast.walk(tree):
        if isinstance(n,ast.For) and isinstance(n.target,(ast.Tuple,ast.List)) and isinstance(n.iter,(ast.Tuple,ast.List)):
            names=n.target.elts
            if names and isinstance(names[0],ast.Name):
                values=[component(x.elts[0]) for x in n.iter.elts if isinstance(x,(ast.Tuple,ast.List)) and x.elts]
                if values and len(values)==len(n.iter.elts) and all(x is not None and not Path(x).is_absolute() and '..' not in Path(x).parts for x in values):
                    segments[names[0].id]=values[0]
    def path(n):
        if isinstance(n,ast.Name):return paths.get(n.id)
        if isinstance(n,ast.Call) and ast.unparse(n.func)=='pathlib.Path' and len(n.args)==1:
            value=component(n.args[0])
            if value is None and isinstance(n.args[0],ast.Subscript) and ast.unparse(n.args[0].value)=='sys.argv' and isinstance(n.args[0].slice,ast.Constant):
                index=n.args[0].slice.value
                if type(index) is int and 1<=index<=len(argv):value=argv[index-1]
            return Path(value).resolve() if value is not None else None
        if isinstance(n,ast.BinOp) and isinstance(n.op,ast.Div):
            left,right=path(n.left),component(n.right)
            if left is not None and right is not None:return (left/right).resolve()
    assignments=[n for n in ast.walk(tree) if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name)]
    for _ in range(len(assignments)+1):
        for n in assignments:
            p=path(n.value)
            if p is not None:paths[n.targets[0].id]=p
            if isinstance(n.value,ast.Call) and ast.unparse(n.value.func)=='json.loads' and n.value.args:
                read=n.value.args[0]
                if isinstance(read,ast.Call) and isinstance(read.func,ast.Attribute) and read.func.attr=='read_text':
                    p=path(read.func.value)
                    if p is not None and p.is_file() and (p.is_relative_to(audit) or p.is_relative_to(Path('/private/tmp')) and p.suffix=='.json'):
                        value=json.loads(p.read_text())
                        if isinstance(value,dict):objects[n.targets[0].id]=value
    for name in numeric | set(paths) | set(segments):
        stores=[n for n in assignments if n.targets[0].id==name]
        if len(stores)>1 or name in segments and stores:
            raise ValueError('failed helper cannot rebind its bounded path components')
    for n in ast.walk(tree):
        if isinstance(n,ast.ImportFrom):
            for item in n.names:
                if item.name=='extraction_quality':aliases[item.asname or item.name]='quality'
                if item.name=='agent_extract':aliases[item.asname or item.name]='ax'
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name not in imports or a.asname for a in n.names):
            raise ValueError('failed audit helper has an unsupported import')
        if isinstance(n, ast.ImportFrom) and (n.module, tuple(a.name for a in n.names)) not in (
                ('utils', ('extraction_quality',)), ('tools', ('agent_extract',))):
            raise ValueError('failed audit helper imports executable external code')
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                          ast.With, ast.AsyncWith, ast.Await, ast.Global, ast.Nonlocal)):
            raise ValueError('failed audit helper must be bounded local code')
        if isinstance(n, ast.Attribute) and n.attr.startswith('_'):
            raise ValueError('failed audit helper cannot access dynamic attributes')
        if isinstance(n,ast.Constant) and isinstance(n.value,str) and (n.value.startswith('../') or '/../' in n.value):
            raise ValueError('failed audit helper cannot traverse out of its private scope')
        if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store) and n.id in imports | set(aliases):
            raise ValueError('failed audit helper cannot replace its imported utilities')
        if not isinstance(n, ast.Call):continue
        name = ast.unparse(n.func)
        first,*tail=name.split('.')
        if first in aliases:name='.'.join([aliases[first],*tail])
        pure = {'json.loads','json.dumps','copy.deepcopy','hashlib.sha256','pathlib.Path',
                'str','int','len','set','isinstance','any','all','next','print','range','sorted','enumerate',
                'quality.context_digest','quality.validate_review','quality.validate_payload_quality','ax.build_payload',
                'sys.path.insert'}
        methods = {'read_text','write_text','exists','mkdir','split','splitlines','get','items','append','remove','update',
                   'resolve','hexdigest','encode'}
        if name not in pure and not (isinstance(n.func, ast.Attribute) and n.func.attr in methods):
            raise ValueError('failed helper contains an unreviewable executable call: '+name)
        if isinstance(n.func,ast.Attribute) and n.func.attr in ('read_text','write_text','exists','mkdir'):
            p=path(n.func.value)
            if p is None or any(x.startswith('.env') for x in p.parts) or not (p.is_relative_to(audit) or
                    p.is_relative_to(Path('/private/tmp')) and p.suffix in ('.json','.py')):
                raise ValueError('failed helper file operation is not bounded: '+ast.unparse(n.func.value))
        if name == 'pathlib.Path' and n.args and isinstance(n.args[0], ast.Constant):
            p = Path(n.args[0].value).resolve()
            if p.is_absolute() and not p.is_relative_to(audit) and not (
                    p.is_relative_to(Path('/private/tmp')) and p.suffix in ('.json','.py')):
                raise ValueError('failed helper accesses outside private audit scope')


def _failed(row, audit, batch):
    shell = _shell(row['command'])
    if row['exit_code'] in (75,76) or DENIED.search(row['output']) or WRITE.search(shell):
        raise ValueError('approval/NAS/permission or failed write cannot use maintenance continuation')
    refs = {}
    # Heredocs are data until the exact reviewed helper is invoked below.
    bodies = re.findall(r"(?:python3 -|cat > [^\n]+) <<'PY'\n(.*?)\nPY(?:\n|$)", shell, re.S)
    remainder = re.sub(r"(?:python3 -|cat > [^\n]+) <<'PY'\n.*?\nPY(?:\n|$)", '', shell, flags=re.S)
    for body in bodies:_local_script(body, audit)
    remainder = remainder.replace('"${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract"',
                                  shlex.quote(str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser())))
    remainder = remainder.replace('"${KG_HUB_PYTHON:-$HOME/.local/share/kg-hub/venv/bin/python}"','python3')
    if any(x in remainder for x in ('$','`',';')):
        raise ValueError('failed maintenance command has executable shell expansion/chain')
    transport_text = row['output']
    preview = False
    for line in remainder.splitlines():
        if not line.strip():continue
        lexer=shlex.shlex(line,posix=True,punctuation_chars=';&|');lexer.whitespace_split=True;lexer.commenters=''
        args=list(lexer)
        if any(x and all(c in ';&|' for c in x) for x in args):raise ValueError('failed shell cannot execute pipelines')
        if args[:2]==['rg','-n'] and len(args)>=4:
            if any(not Path(p).is_relative_to(Path('~/.local/share/kg-hub/current/tools').expanduser()) for p in args[3:]):
                raise ValueError('failed diagnostic must read published tool files')
        elif args and args[0]=='python3' and len(args)>=2:
            script=Path(args[1]).resolve(strict=True)
            if not script.is_relative_to(Path('/private/tmp')):raise ValueError('private audit helper required')
            _local_script(script.read_text(),audit,args[2:]);refs[str(script)]=_ref(script)
            for value in args[2:]:
                if value.startswith('/') and not Path(value).resolve().is_relative_to(audit) and not (
                        Path(value).resolve().is_relative_to(Path('/private/tmp')) and Path(value).suffix=='.json'):
                    raise ValueError('failed helper arguments must remain in private audit scope')
                if value.startswith('/') and Path(value).is_file():refs[str(Path(value).resolve())]=_ref(value)
        elif len(args)>=3 and args[0]==str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser()) and args[1:3] in (['repair','preview'],['agent','quality-check']):
            if args[1:3]==['repair','preview']:
                flags={};i=3
                while i<len(args):
                    key=args[i]
                    if key in ('>','2>'):
                        p=Path(args[i+1]).resolve()
                        if not p.is_relative_to(audit):raise ValueError('private preview output required')
                        if key=='2>' and p.exists():
                            refs[str(p)]=_ref(p);transport_text+='\n'+p.read_text()
                    elif key in ('--file','--out','--request-out') and key not in flags:
                        flags[key]=args[i+1]
                    else:raise ValueError('only original public preview flags qualify')
                    i+=2
                selector=Path(flags['--file']).resolve(strict=True)
                if not selector.is_relative_to(audit):raise ValueError('private original source selector required')
                source=_read(selector)
                if not any(all(source.get(k)==x.get(k) for k in ('source_description','source_obs_id')) for x in batch['items']):
                    raise ValueError('maintenance preview must concern this original submitted batch')
                refs[str(selector)]=_ref(selector);preview=True
                for key in ('--out','--request-out'):
                    if key in flags and not Path(flags[key]).resolve().is_relative_to(audit):
                        raise ValueError('preview artifact must stay in the private audit')
        else:raise ValueError('failed command contains an unsupported operation')
    if DENIED.search(transport_text):raise ValueError('maintenance never waives an actual refusal')
    transport=preview and any(x in transport_text for x in TRANSPORT)
    return {'event':row['id'],'exit_code':row['exit_code'],'command':row['command'],
            'command_sha256':digest(row['command']),'output_sha256':digest(row['output']),
            'transport_read_only':transport,'helpers':sorted(refs.values(),key=lambda x:x['path'])}


def inventory(c,d,model,slot,spec):
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None:raise ValueError('coordinator only')
    root=ec.supervisor_dir(d,model,slot);worker=receipts.read(root/'worker.json');native=receipts.read(root/'native/latest.json')
    sid=c['slot_sessions'][model][str(slot)];batches=worker.get('batches')
    if ((c.get('dispatch') or {}).get('state')!='draining' or worker.get('mode')!='quality_rework'
            or worker.get('session')!=sid or len(batches or [])!=1 or native.get('state')!='completed'
            or native.get('exit_code')!=0 or not native.get('attempt')
            or sum(sid==x for slots in c['slot_sessions'].values() for x in slots.values())!=1):
        raise ValueError('one idle native0 original owner rework required')
    bid=batches[0];b=ec.find_batch(c,bid)
    if b['status']!='submitted' or b['model']!=model or ec.batch_slot(c,b)!=slot:raise ValueError('original submitted batch required')
    event=Path(worker['events']).resolve(strict=True);job=event.parent
    if event.name!='events.jsonl' or not job.is_relative_to((root/'quality-reworks').resolve()):raise ValueError('original managed rework event required')
    scope=_read(job/'scope.json');terminal=_read(job/'exit-status.json')
    if (any(scope.get(k)!=v for k,v in {'campaign':c['id'],'model':model,'slot':slot,'session':sid,'batches':batches,'new_source_quota':0}.items())
            or terminal.get('exit_code')!=0 or terminal.get('session')!=sid
            or abs(_time(native['ended_at'])-_time(terminal['ended_at']))>2
            or Path(scope['assignment']['path']).resolve()!=job/'assignment.md'
            or _ref(scope['assignment']['path'])!=scope['assignment']):raise ValueError('original scope/native terminal differs')
    outcome=supervision.events(event)
    if (outcome.get('recovery_kind')!='infrastructure_failed' or outcome.get('session')!=sid
            or outcome.get('terminal')!='turn.completed' or outcome.get('hard_stop') or outcome.get('approval_refused')):
        raise ValueError('only explicit completed infrastructure stop qualifies')
    reports=[]
    for line in event.read_text().splitlines():
        row=json.loads(line)
        if row.get('type')=='error':raise ValueError('provider/native error requires separate recovery')
        item=row.get('item',{})
        if item.get('type')=='agent_message':
            try:msg=json.loads(item.get('text',''))
            except ValueError:continue
            if isinstance(msg,dict) and msg.get('recovery_kind'):reports.append(msg)
    if len(reports)!=1 or reports[0].get('batch')!=bid or reports[0].get('model')!=model or 'in_flight' not in reports[0] or reports[0]['in_flight'] is not None:
        raise ValueError('one original same-batch no-in-flight report required')
    audit=Path(spec['audit_dir']).resolve(strict=True)
    if not audit.is_relative_to((d/'audit').resolve()):raise ValueError('private campaign audit required')
    checkpoint=_read(audit/'checkpoint.json');original=_read(d/'batches'/f'{bid}.json')
    reviewed,pending=checkpoint.get('reviewed',[]),checkpoint.get('pending',[])
    ids={x['id'] for x in original['items']}
    if (checkpoint.get('batch')!=bid or len(reviewed)+len(pending)!=len(ids)
            or set(reviewed)&set(pending) or set(reviewed)|set(pending)!=ids
            or 'in_flight' not in checkpoint or checkpoint['in_flight'] is not None or reports[0].get('reviewed')!=len(reviewed)
            or set(reports[0].get('unfinished_sources',[]))!=set(pending)):
        raise ValueError('partial semantic checkpoint is incomplete or has an unresolved operation')
    commands=_commands(event,sid);failed=[_failed(x,audit,original) for x in commands if x['exit_code']]
    if not failed or not any(x['transport_read_only'] for x in failed):raise ValueError('read-only transport failure required')
    release={k:_ref(spec['release'][k]) for k in ('batch','completed','capabilities')}
    start,done,caps=(_read(release[k]['path']) for k in ('batch','completed','capabilities'))
    if (not start.get('authorization') or not start.get('operator') or done.get('commit')!=start.get('frozen_commit')
            or not _time(native['started_at'])<=_time(start['at'])<=_time(native['ended_at'])<=_time(done['at'])
            or caps.get('http')!=200 or not caps.get('body',{}).get('revision')):
        raise ValueError('the original stop must overlap the explicitly authorized completed maintenance')
    refs=[_ref(p) for p in sorted(audit.rglob('*')) if p.is_file()]
    if len(refs)>3000:raise ValueError('audit scope too large to review')
    return {'version':VERSION,'campaign':c['id'],'model':model,'slot':slot,'session':sid,'batches':batches,
            'contract':_contract(c,model),'worker':_ref(root/'worker.json'),'native':_ref(root/'native/latest.json'),
            'events':_ref(event),'scope':_ref(job/'scope.json'),'terminal':_ref(job/'exit-status.json'),
            'assignment':scope['assignment'],'batch':_ref(d/'batches'/f'{bid}.json'),
            'checkpoint':_ref(audit/'checkpoint.json'),'audit_files':refs,'failed':failed,'release':release,
            'server_revision':caps['body']['revision'],'reviewed':len(reviewed),'pending':len(pending),
            'semantic_acceptance':'not_granted','new_source_quota':0}


def sheet(cid,model,slot,*,evidence,out):
    ec.tracking_guard();d=ec.campaign_dir(cid)
    spec=_read(evidence);facts=inventory(ec.load(d),d,model,slot,spec)
    if Path(out).resolve().is_relative_to(Path(spec['audit_dir']).resolve()):
        raise ValueError('save the review outside the frozen audit directory')
    result={'version':VERSION,'spec':spec,'inventory':facts,'inventory_sha256':digest(facts),'reviewer':'',
            'reviews':[{'event':x['event'],'command_sha256':x['command_sha256'],'verdict':'pending','note':''} for x in facts['failed']]}
    receipts.write(out,result)
    return {'review':str(out),'failed_commands':len(facts['failed']),'reviewed':facts['reviewed'],'pending':facts['pending'],'approval':'pending'}


def validate(c,d,model,slot,batches,path):
    doc=_read(path)
    facts=inventory(c,d,model,slot,doc['spec'])
    if (doc.get('version')!=VERSION or doc.get('inventory')!=facts or doc.get('inventory_sha256')!=digest(facts)
            or facts['batches']!=batches or not str(doc.get('reviewer','')).strip()):raise ValueError('maintenance review is stale or incomplete')
    reviews=doc.get('reviews',[])
    if len(reviews)!=len(facts['failed']) or len({x['event'] for x in reviews})!=len(reviews):raise ValueError('review every failed command explicitly')
    for failure in facts['failed']:
        match=[x for x in reviews if x.get('event')==failure['event']]
        verdict='readonly_transport_failure' if failure['transport_read_only'] else 'local_failure_no_external_write'
        if (len(match)!=1 or match[0].get('command_sha256')!=failure['command_sha256']
                or match[0].get('verdict')!=verdict or len(str(match[0].get('note','')).strip())<20):
            raise ValueError('every failed operation needs its exact reviewed non-writing disposition')
    return {'review':_ref(path),'inventory':facts,'reviewer':doc['reviewer']}


def restored(proof,flight):
    if not flight.get('ok') or flight.get('server',{}).get('revision')!=proof['inventory']['server_revision']:
        raise ValueError('fresh preflight must confirm the completed maintenance provider revision')
