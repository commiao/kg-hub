"""Bounded continuation of an exact capacity failure during drained rework.

This is a process disposition, not semantic acceptance. No production claim is
enabled. Failed tools are rejected unless they provably stopped before executing
their Python body, are an argument-parser-only help request, or are a bound
read-only stale-preview/not-found pair.
"""
import ast
import json
import os
import re
import shlex
from pathlib import Path

from tools import campaign_supervision as supervision, extract_campaign as ec, agent_extract as ax
from tools.campaign_rework_completion import _shell
from tools.campaign_ownership_recovery import _ref, _time
from utils import process_receipts as receipts


PRECHECK_WRAPPER = r"""import json, pathlib, subprocess, sys
p=pathlib.Path(AUDIT_PATH)
cli=REPAIR_CLI
for n in SOURCE_IDS:
 req=json.loads((p/f'{n}-reviewed-request.json').read_text()); plan=json.loads((p/f'{n}-plan-preview.json').read_text())
 assert plan['http']==200 and plan['status']=='repair_preview' and plan['request_sha256']
 assert len(plan['plan']['remove'])==len(req['remove_edge_uuids'])
 assert all(e['props'].get('episodes')==[plan['snapshot']['episode']['props']['uuid']] for e in plan['snapshot']['edges'] if e['props']['uuid'] in req['remove_edge_uuids'])
 for label,cmd in [
  ('apply',['apply','--file',str(p/f'{n}-reviewed-request.json'),'--out',str(p/f'{n}-apply.json')]),
  ('status',['status','--repair-id',req['repair_id'],'--out',str(p/f'{n}-status.json')]),
  ('readback',['preview','--file',str(p/f'{n}-selector.json'),'--out',str(p/f'{n}-readback.json')])]:
  r=subprocess.run(['python3',cli,*cmd],capture_output=True,text=True)
  print(n,label,r.returncode,flush=True)
  if r.returncode: print(r.stderr[-500:],flush=True);sys.exit(r.returncode)
  if label=='apply':
   a=json.loads((p/f'{n}-apply.json').read_text());
   if a.get('http')!=200 or a.get('verified') is not True: print('unverified apply',n,flush=True);sys.exit(1)
  if label=='status':
   s=json.loads((p/f'{n}-status.json').read_text());
   if s.get('repair',{}).get('state')!='applied': print('unapplied status',n,flush=True);sys.exit(1)
  if label=='readback':
   x=json.loads((p/f'{n}-readback.json').read_text()); fs=[e['props']['fact'] for e in x['snapshot']['edges']]; uuids={e['props']['uuid'] for e in x['snapshot']['edges']}
   if not all(e['fact'] in fs for e in req['replacement']['edges']) or uuids.intersection(req['remove_edge_uuids']): print('bad readback',n,flush=True);sys.exit(1)
 print(n,'verified',len(fs),flush=True)"""


def local_failure(item, scopes):
    shell = _shell(item['command'])
    output = item.get('aggregated_output', '')
    # No command body was run by argparse. Do not accept a shell command chain.
    if (item['exit_code'] == 2 and re.fullmatch(
            r'"\$\{KG_HUB_CODE:-\$HOME/\.local/share/kg-hub/current\}/tools/extract" campaign repair --help', shell)
            and "invalid choice: 'repair'" in output and 'Traceback' not in output):
        return 'argument_parser_help_only'
    match = re.fullmatch(r"python3 - <<'PY'\n(.*)\nPY", shell, re.S)
    if match and item['exit_code'] == 1:
        try:
            ast.parse(match[1])
        except SyntaxError:
            if re.fullmatch(r'  File "<stdin>", line \d+\n.*SyntaxError: [^\n]+\n', output, re.S):
                return 'python_body_did_not_compile'
        else:
            if prewrite_failure(match[1], output, scopes):
                return 'repair_wrapper_failed_before_first_write'
    raise ValueError('capacity continuation cannot waive a failed or uncertain tool')



def prewrite_failure(script, output, scopes):
    # This exact sequential wrapper prints after every subprocess return. Its
    # first failing generator assertion is before any subprocess. Requiring the
    # complete AST and an output consisting solely of that traceback excludes
    # prior iterations, partial remote writes, altered shell calls and catches.
    tree = ast.parse(script)
    try:
        audit = ast.literal_eval(tree.body[1].value.args[0])
        cli = ast.literal_eval(tree.body[2].value)
        ids = ast.literal_eval(tree.body[3].iter)
    except (AttributeError, ValueError, TypeError, IndexError):
        return False
    allowed_cli = {Path(__file__).resolve().with_name('extraction_repair.py'),
                   Path('~/.local/share/kg-hub/current/tools/extraction_repair.py').expanduser().resolve()}
    if (not isinstance(audit, str) or str(Path(audit).resolve()) not in scopes
            or not isinstance(cli, str) or Path(cli).resolve() not in allowed_cli
            or not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids)
            or len(ids) != len(set(ids)) or not set(ids) <= scopes[str(Path(audit).resolve())]):
        return False
    expected = PRECHECK_WRAPPER.replace('AUDIT_PATH', repr(audit)).replace('REPAIR_CLI', repr(cli)).replace('SOURCE_IDS', repr(ids))
    if ast.dump(tree) != ast.dump(ast.parse(expected)):
        return False
    line = tree.body[3].body[4].lineno
    return output == ('Traceback (most recent call last):\n'
        f'  File "<stdin>", line {line}, in <module>\n'
        f'  File "<stdin>", line {line}, in <genexpr>\nKeyError: \'props\'\n')

def readonly_failures(path, source_keys, audit, model, executor, run_id):
    """Bind an exact stale preview/not-found pair; never replay either call."""
    pairs, references, accepted = {}, {}, {}
    entry = str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser())
    for line in Path(path).read_text().splitlines():
        row = json.loads(line); item = row.get('item', {})
        if (row.get('type') != 'item.completed' or item.get('type') != 'command_execution'
                or item.get('exit_code') != 1):
            continue
        try:
            shell = _shell(item['command'])
            if any(mark in shell for mark in ('\n', ';', '|', '&', '<', '>', '`', '$(')):
                continue
            args = shlex.split(shell)
            body = json.loads(item.get('aggregated_output', ''))
        except (ValueError, KeyError):
            continue
        if args and args[0] == '${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract':
            args[0] = entry
        if len(args) != 7 or args[:2] != [entry, 'repair'] or args[5] != '--out':
            continue
        if not isinstance(body, dict) or body.get('status') != 'error':
            continue
        preview = args[2:4] == ['preview', '--file'] and (body.get('http'), body.get('code')) == (409, 'stale_snapshot')
        status = args[2:4] == ['status', '--repair-id'] and (body.get('http'), body.get('code')) == (404, 'repair_not_found')
        if not preview and not status:
            continue
        output = Path(args[6]).resolve(strict=True)
        if not output.is_relative_to(audit.resolve()) or receipts.read(output) != body:
            raise ValueError('retained original read-only response differs')
        references[str(output)] = _ref(output)
        if preview:
            request_path = Path(args[4]).resolve(strict=True)
            if not request_path.is_relative_to(audit.resolve()) or request_path.parent != output.parent:
                raise ValueError('stale repair request must stay in original private audit')
            request = receipts.read(request_path); repair_id = request.get('repair_id')
            replacement = request.get('replacement', {})
            if (not isinstance(repair_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', repair_id)
                    or repair_id in pairs
                    or (request.get('source_description'), request.get('source_obs_id')) not in source_keys.values()
                    or replacement.get('extractor_model') != model or replacement.get('extractor_tool') != executor
                    or replacement.get('run_id') != run_id
                    or not re.fullmatch(r'[0-9a-f]{64}', str(request.get('expected_snapshot_sha256', '')))):
                raise ValueError('exact original-source stale repair request required')
            references[str(request_path)] = _ref(request_path)
            pairs[repair_id] = {'preview': item['id'], 'folder': output.parent, 'status': None}
        else:
            pair = pairs.get(args[4])
            if not pair or pair['status'] is not None or pair['folder'] != output.parent:
                raise ValueError('not-found read must follow the exact same stale preview')
            pair['status'] = item['id']
    for pair in pairs.values():
        if pair['status'] is None:
            raise ValueError('stale preview requires its retained same-repair not-found response')
        for key in (pair['preview'], pair['status']):
            accepted[key] = 'readonly_stale_preview_and_not_found_status'
    return accepted, references


def transcript(path, session, scopes, readonly=None):
    pending, completed, sessions, failures = {}, set(), [], []
    terminal = False
    for line in Path(path).read_text().splitlines():
        row = json.loads(line)
        if terminal:
            raise ValueError('events follow the capacity terminal')
        if row.get('type') == 'thread.started':
            sessions.append(row.get('thread_id'))
        if row.get('type') == 'turn.completed':
            raise ValueError('exact failed capacity turn required')
        if row.get('type') == 'turn.failed':
            terminal = True
        item = row.get('item', {})
        if item.get('type') == 'file_change':
            key = item.get('id')
            changes = item.get('changes')
            if not changes or any(not any(Path(x['path']).resolve().is_relative_to(Path(a)) for a in scopes)
                                  for x in changes):
                raise ValueError('file changes must stay in the selected private audit')
            if row.get('type') == 'item.started':
                if not key or key in pending or key in completed:
                    raise ValueError('unique file change required')
                pending[key] = changes
            elif row.get('type') == 'item.completed':
                if pending.pop(key, None) != changes or item.get('status') != 'completed':
                    raise ValueError('complete file change required')
                completed.add(key)
            continue
        if item and item.get('type') not in ('command_execution', 'agent_message', 'reasoning'):
            raise ValueError('unrecognized tool requires separate reconciliation')
        if item.get('type') != 'command_execution':
            continue
        key = item.get('id')
        if row.get('type') == 'item.started':
            if not key or not item.get('command') or key in pending or key in completed:
                raise ValueError('unique tool starts required')
            pending[key] = item['command']
        elif row.get('type') == 'item.completed':
            if key not in pending or pending.pop(key) != item.get('command'):
                raise ValueError('matched tool terminal required')
            completed.add(key)
            if (type(item.get('exit_code')) is not int
                    or item.get('status') not in ('completed', 'failed')
                    or item['exit_code'] == 0 and item.get('status') != 'completed'):
                raise ValueError('complete tool exit required')
            if item['exit_code']:
                failures.append({'id': key, 'disposition': (readonly or {}).get(key) or local_failure(item, scopes)})
    if sessions != [session] or pending or not terminal:
        raise ValueError('one original session and no pending tools required')
    return failures



def reviewed_incident(c, root, model, slot, session, worker, native, batches, path):
    """Reconcile the one source-audited incident; never waive an unknown script.

    This evidence-only function cannot acquire a retry permission. Its only
    production caller is validate(), consumed by campaign_rework.run inside
    the existing supervisor attempt and native idle contexts, twice before
    startup. Budget, service, NAS, approval and operation gates remain outside
    and cannot be replaced by a caller-provided lock boolean.
    """
    from tools import campaign_rework_context as original
    from tools import campaign_rework_reviewed_effects as effects
    from tools import campaign_rework_repairs as explicit
    from tools.campaign_rework_evidence import read_regular, unique_json
    # Exact immutable transcript reviewed alongside the code-pinned private profile.
    # A changed or unrelated transcript follows the ordinary strict validator.
    if _ref(path)['sha256'] != '2404c3537d0c3e46791d6d5dcfc0ba6dc464b33687f040c0c17373d51001027a':
        return None
    if slot is not None:
        raise ValueError('reviewed incident cannot migrate the original single lane')
    d = ec.campaign_dir(c['id'])
    sessions = Path.home() / '.codex/sessions'
    releases = Path.home() / '.local/share/kg-hub/releases'
    context = original.capture(d, c['id'], model, sessions_root=sessions,
                               releases_root=releases, workspace=ec.workspace_dir())
    identity = context['identity']
    if (identity['session'] != session or [identity['batch']] != batches
            or identity['native_attempt'] != native['attempt']
            or identity['native_child_pid'] != native['child_pid']
            or d / identity['job_relative'] != path.parent
            or root != d / 'supervisors' / model):
        raise ValueError('reviewed original invocation identity changed')
    effect = effects.inspect(context, d, releases, sessions)
    refs = context['manifest']['files']
    rows = [unique_json(line) for line in read_regular(d,refs['events']['path'],refs['events']['sha256']).splitlines()]
    batch = unique_json(read_regular(d,refs['batch']['path'],refs['batch']['sha256']))
    source_keys = {x['id']:(x['source_description'],x['source_obs_id']) for x in batch['items']}
    audit = d / identity['audit_relative']
    reconciled = explicit.reconcile(rows,effect['repair_inventory'],
        expected_items={x['item'] for x in effect['repair_inventory']},audit=audit,
        source_keys=source_keys,model=model,executor=c['executor'],run_id=c['models'][model]['run_id'],
        status_get=lambda rid:explicit.read_status(rid,os.environ.get('KG_HUB_URL',ax.DEFAULT_URL)))
    # Fresh GETs cannot replace unchanged original authority. Retain every input
    # needed to reconstruct this disposition in the new job's frozen evidence.
    evidence = {}
    for absolute, ref in context['authority'].items():
        read_regular(ref['anchor'],ref['path'],ref['sha256'])
        evidence[absolute] = {'path':absolute,'sha256':ref['sha256']}
    if effects.inspect(context,d,releases,sessions) != effect:
        raise ValueError('reviewed retained source changed during public readback')
    for absolute, ref in effect['retained_source_references'].items():
        evidence[absolute] = {'path':absolute,'sha256':ref['sha256']}
    for entry in effect['repair_inventory']:
        for suffix,key in [('reviewed-request','request_sha256'),('apply','apply_sha256')]:
            name=f"{entry['source_id']}.{suffix}.json"
            read_regular(audit,name,entry[key])
            evidence[str(audit/name)]={'path':str(audit/name),'sha256':entry[key]}
    profile=d/effects.PROFILE_RELATIVE
    if _ref(profile)['sha256'] != effect['review_profile_sha256']:
        raise ValueError('private reviewed profile changed')
    evidence[str(profile)] = _ref(profile)
    proof = context['evidence']
    failures = [{'id':x['item'],'disposition':x['evidence_disposition']}
                for x in proof['local_evidence']]
    failures.append({'id':proof['host_evidence']['native_item'],
                     'disposition':proof['host_evidence']['disposition']})
    return {'local_failures':failures,'evidence':evidence,
            'reviewed_incident':{'original_identity':identity,'original_evidence':proof,
                'source_effect_review':effect,'public_repair_readbacks':reconciled,
                'normal_os_exit_proven':False,'semantic_acceptance':False,
                'new_source_quota':0}}


def validate(c, root, model, slot, session, worker, native, batches):
    path = Path(worker.get('events', '/missing')).resolve(strict=True)
    job = path.parent
    outcome = supervision.events(path)
    if (worker.get('mode') != 'quality_rework' or worker.get('batches') != batches
            or job.parent != (root / 'quality-reworks').resolve() or path.name != 'events.jsonl'
            or not native.get('attempt') or not native.get('child_pid')
            or not supervision.capacity_terminal(outcome, native)
            or outcome.get('session') != session):
        raise ValueError('exact original rework capacity terminal required')
    end, scope = receipts.read(job / 'exit-status.json'), receipts.read(job / 'scope.json')
    expected = {'campaign': c['id'], 'model': model, 'slot': slot, 'session': session,
                'batches': batches, 'new_source_quota': 0}
    if (any(scope.get(k) != v for k, v in expected.items())
            or scope.get('assignment') != _ref(job / 'assignment.md')
            or end.get('session') != session or end.get('exit_code') != 1
            or abs(_time(end['ended_at']) - _time(native['ended_at'])) > 2):
        raise ValueError('original rework scope and exit must match')
    count = worker.get('capacity_retries', 0)
    policy = supervision.capacity_policy(c)
    if type(count) is not int or not 0 <= count < policy['capacity_max_retries']:
        raise ValueError('rework capacity retry budget exhausted')
    wait = supervision.capacity_wait(policy, count + 1)
    if _time(receipts.now()) - _time(native['ended_at']) < wait:
        raise ValueError(f'capacity continuation requires the original {wait:g}-second backoff')
    scopes = {str((ec.campaign_dir(c['id']) / 'private-rework' / bid).resolve()):
              set(ec.find_batch(c, bid)['ids']) for bid in batches}
    source_keys = {}
    for bid in batches:
        batch = receipts.read(ec.campaign_dir(c['id']) / 'batches' / (bid + '.json'))
        source_keys.update({item['id']: (item['source_description'], item['source_obs_id']) for item in batch['items']})
        op = receipts.status(ec.campaign_dir(c['id']) / 'operations' / bid)
        if (op.get('lease_held') is not False or op.get('state') != 'completed'
                or op.get('kind') != 'submit' or op.get('exit_code') != 0
                or op.get('may_have_written') or op.get('requires_reconciliation')):
            raise ValueError('submitted batch operation must have a complete known terminal')
    reviewed = reviewed_incident(c, root, model, slot, session, worker, native, batches, path)
    if reviewed is not None:
        return {'retry': count + 1, **reviewed}
    repairs = {}
    for audit, ids in scopes.items():
        for applied in Path(audit).glob('*-apply.json'):
            source = applied.name.removesuffix('-apply.json')
            if not source.isdigit() or int(source) not in ids:
                raise ValueError('repair receipt source is outside original batch')
            status = applied.with_name(source + '-status.json')
            a, s = receipts.read(applied), receipts.read(status)
            repair = s.get('repair', {})
            request = repair.get('request', {})
            if (a.get('http') != 200 or a.get('verified') is not True or s.get('http') != 200
                    or repair.get('state') != 'applied' or repair.get('result', {}).get('verified') is not True
                    or not a.get('repair_id') or repair.get('repair_id') != a['repair_id']
                    or (request.get('source_description'), request.get('source_obs_id')) != source_keys[int(source)]):
                raise ValueError('known repair writes need matching applied and verified status receipts')
            repairs[str(applied)] = _ref(applied)
            repairs[str(status)] = _ref(status)
    readonly, retained_reads = readonly_failures(path, source_keys, ec.campaign_dir(c['id']) / 'audit',
                                                 model, c['executor'], c['models'][model]['run_id'])
    failures = transcript(path, session, scopes, readonly)
    return {'retry': count + 1, 'local_failures': failures, 'evidence': {**repairs, **retained_reads, **{
        name: _ref(job / name) for name in ('events.jsonl', 'scope.json', 'exit-status.json', 'assignment.md')
    }}}
