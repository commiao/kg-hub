"""Evidence-bound disposition of a completed rework's rejected cost note.

The rejected write remains rejected. This registers an explicitly reviewed
terminal, not quality acceptance, cost permission, or a new source allowance.
"""
from __future__ import annotations

import ast
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import uuid

from tools import extract_campaign as ec
from tools.campaign_ownership_recovery import _contract, _read, _ref, _time, _unchanged
from utils import extraction_quality as quality, process_receipts as receipts

KIND = 'completed_rework_cost_note_disposition'
MESSAGE = 'quality rework cannot claim, resubmit or overwrite original batch artifacts; use separate agent files'


def _entry():
    return str(Path(os.environ.get('KG_HUB_CODE', '~/.local/share/kg-hub/current')).expanduser() / 'tools/extract')


def _shell(command):
    args = shlex.split(command)
    if len(args) == 3 and Path(args[0]).name in ('sh', 'bash', 'zsh') and args[1] in ('-c', '-lc'):
        return args[2]
    return command


def cost_note_script(d, audit, bid, suffix, newline='\\n'):
    """The one supported legacy wrapper, also useful for synthetic fixtures.

    Nothing executes this text here. Equality of its complete AST excludes
    extra child processes, shell/HTTP calls, writes, or altered argument flags.
    """
    return f'''import json,pathlib,subprocess
p=pathlib.Path({str(d)!r});a=p/{str(Path(audit).resolve().relative_to(Path(d).resolve()))!r};c=json.loads((p/'campaign.json').read_text());b=next(x for x in c['batches'] if x['id']=={bid!r});old=b.get('cost',{{}});(a/'final/cost-before-final-rework-note.json').write_text(json.dumps(old,ensure_ascii=False,indent=2)+{newline!r});assert not any(k in old for k in ['credits','tokens','seconds'])
note=old['note']+{suffix!r}
r=subprocess.run([{_entry()!r},'campaign','cost','--id',c['id'] if 'id'in c else{ec.load(d)['id']!r},'--batch',{bid!r},'--note',note],capture_output=True,text=True);(a/'final/cost-command.stdout.log').write_text(r.stdout);(a/'final/cost-command.stderr.log').write_text(r.stderr);print('cost exit',r.returncode,r.stdout[:1200],r.stderr);raise SystemExit(r.returncode)
'''


def _cost_command(command, d, audit, bid):
    shell = _shell(command)
    match = re.fullmatch(r"python3 - <<'PY'\n(.*)\nPY", shell, flags=re.S)
    if not match:
        raise ValueError('exact bounded Python cost-note wrapper required')
    tree = ast.parse(match[1])
    notes = [n.value.right.value for n in tree.body if isinstance(n, ast.Assign)
             and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'note'
             and isinstance(n.value, ast.BinOp) and isinstance(n.value.op, ast.Add)
             and isinstance(n.value.right, ast.Constant) and isinstance(n.value.right.value, str)]
    endings = [n.value.args[0].right.value for n in tree.body if isinstance(n, ast.Expr)
               and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
               and n.value.func.attr == 'write_text' and n.value.args
               and isinstance(n.value.args[0], ast.BinOp) and isinstance(n.value.args[0].right, ast.Constant)]
    if len(notes) != 1 or not notes[0].strip() or endings not in (['\\n'], ['\n']):
        raise ValueError('only an appended note and private audit outputs qualify')
    expected = ast.parse(cost_note_script(d, audit, bid, notes[0], endings[0]))
    if ast.dump(tree) != ast.dump(expected):
        raise ValueError('cost wrapper contains other operations or altered identity/flags')
    return {'note_suffix': notes[0], 'script_ast_sha256': quality.digest(ast.dump(tree))}


def _commands(path, session):
    starts, pending, done, completed_turns, notices = [], {}, [], 0, []
    for number, line in enumerate(Path(path).read_text().splitlines()):
        row = json.loads(line)
        if completed_turns:
            raise ValueError('no events may follow the original turn terminal')
        if row.get('type') == 'thread.started':
            starts.append(row.get('thread_id'))
        if row.get('type') == 'turn.failed':
            raise ValueError('failed native turn requires separate recovery')
        if row.get('type') == 'error':
            message = row.get('message', '')
            if not (message.startswith('Reconnecting... ') and any(x in message for x in (
                    'tls handshake eof', 'error decoding response body', 'TLS close_notify',
                    'waiting for network (Connection failed: error sending request)',
                    'peer closed connection without sending TLS close_notify'))):
                raise ValueError('other native/provider failures cannot use cost disposition')
            notices.append(number)
        if row.get('type') == 'turn.completed':
            completed_turns += 1
        item = row.get('item', {})
        if item.get('type') != 'command_execution':
            continue
        key = item.get('id')
        if row.get('type') == 'item.started':
            if not key or key in pending or any(x['id'] == key for x in done):
                raise ValueError('unique command starts required')
            pending[key] = item.get('command')
        elif row.get('type') == 'item.completed':
            if key not in pending or pending.pop(key) != item.get('command') or type(item.get('exit_code')) is not int:
                raise ValueError('every original command requires its exact terminal')
            done.append({'id': key, 'index': number, 'command': item['command'], 'exit_code': item['exit_code'],
                         'output': str(item.get('aggregated_output', ''))})
    if starts != [session] or pending or completed_turns != 1:
        raise ValueError('one fully completed original native invocation required')
    if notices and not any(x['exit_code'] == 0 and x['index'] > max(notices) for x in done):
        raise ValueError('network retries require subsequent real successful progress')
    return done


def _cost_anchor(d, job, spec, batch, current_started):
    previous = _read(job / 'previous-worker.json')
    native = _read(job / 'previous-native.json')
    event = Path(previous['events']).resolve(strict=True)
    if (not event.is_relative_to(job.parent.parent.resolve()) or previous.get('session') != spec['session']
            or native.get('state') != 'completed' or native.get('exit_code') != 0
            or not native.get('attempt') or _time(native['ended_at']) >= _time(current_started)):
        raise ValueError('trusted preceding successful original-session cost anchor required')
    rows = _commands(event, spec['session'])
    matches = [x for x in rows if x['id'] == spec['cost_anchor_event']]
    if len(matches) != 1 or matches[0]['exit_code'] != 0:
        raise ValueError('original successful cost event required')
    command = matches[0]
    # Match the complete executed shell, not an apparent command printed inside
    # a heredoc, a printf payload, or an unexecuted branch.
    lines = _shell(command['command']).splitlines()
    if len(lines) != 8 or lines[0] != 'set -e' or lines[4] != "python3 - <<'PY'" or lines[-1] != 'PY':
        raise ValueError('complete original set-e cost/report/count/readback shape required')
    if any(x in '\n'.join(lines[:4]) for x in ('$', '`', '\r')):
        raise ValueError('original cost anchor shell expansion/compound actions forbidden')
    for line in lines[1:4]:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=';&|<>')
        lexer.whitespace_split = True
        lexer.commenters = ''
        if any(x in (';', '&', '&&', '|', '||', '<', '<<', '>>') for x in lexer):
            raise ValueError('compound original cost anchor command forbidden')
    args = shlex.split(lines[1])
    if (len(args) != 12 or args[:8] != [_entry(), 'campaign', 'cost', '--id', ec.load(d)['id'],
                                       '--batch', batch['id'], '--note']):
        raise ValueError('literal prior note-only cost command identity required')
    note, rest = args[8], args[9:]
    receipt = Path(spec['cost_anchor_receipt'])
    if not receipt.resolve(strict=True).is_relative_to((d / 'audit').resolve()) or rest != ['>', str(receipt), '2>&1']:
        raise ValueError('exact original cost output receipt required')
    report = d / 'audit' / (batch['id'] + '-report.md')
    if (receipt != d / 'audit' / (batch['id'] + '-cost.log')
            or shlex.split(lines[2]) != [_entry(), 'campaign', 'report', '--id', ec.load(d)['id'], '>', str(report), '2>&1']):
        raise ValueError('exact original cost receipt/report path required')
    count = shlex.split(lines[3])
    count_ref = spec.get('cost_count_helper') or {}
    if (len(count) != 3 or count[0] != 'python3' or str(Path(count[1]).resolve()) != count_ref.get('path')
            or Path(count[2]).name != count[2] or not count[2].endswith('.json')
            or _ref(count[1]) != count_ref):
        raise ValueError('reviewed original local count helper required')
    expected_count = f'''import json,datetime,sys
from pathlib import Path
P=Path({str(d)!r});c=json.loads((P/'campaign.json').read_text());batches={{b['id']:b for b in c['batches'] if b['model']=={batch['model']!r} and b.get('status')!='released'}};last={{}}
for l in (P/'log.jsonl').read_text().splitlines():
 d=json.loads(l)
 if d.get('batch') in batches and d.get('id') is not None and d.get('status') in ['ok','empty','error']:
  last[(d['batch'],d['id'])]=d
ok=sum(d.get('status')=='ok' and d.get('http')==200 for d in last.values());empty=sum(d.get('status')=='empty' and d.get('http')==200 and d.get('empty_recorded') is True for d in last.values());out=dict(model={batch['model']!r},ok=ok,empty=empty,success=ok+empty,method='formal log latest(batch,id), exclude released; ok HTTP200; empty HTTP200 and empty_recorded=true',at=datetime.datetime.now(datetime.timezone.utc).isoformat())
if len(sys.argv)>1:(P/'audit'/sys.argv[1]).write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\\n')
print(json.dumps(out,ensure_ascii=False))
'''
    if ast.dump(ast.parse(Path(count[1]).read_text())) != ast.dump(ast.parse(expected_count)):
        raise ValueError('count helper may not forge the cost receipt or execute other actions')
    expected_readback = f'''from pathlib import Path
p=Path({str(d)!r});print('COST', (p/{str(receipt.relative_to(d))!r}).read_text());print('REPORT\\n'+(p/{str(report.relative_to(d))!r}).read_text())'''
    readback = '\n'.join(lines[5:-1])
    if not any(ast.dump(ast.parse(readback)) == ast.dump(ast.parse(candidate)) for candidate in (
            expected_readback, expected_readback.replace("'REPORT\\n'", "'REPORT\\\\n'"))):
        raise ValueError('actual original COST output must read only its executed command receipt')
    value = _read(receipt)
    expected = {'batch': batch['id'], 'cost': {'note': note}}
    output = command['output']
    try:
        embedded, _ = json.JSONDecoder().raw_decode(output.split('COST ', 1)[1].lstrip())
    except (IndexError, ValueError) as exc:
        raise ValueError('original native COST output must attest the prior cost receipt') from exc
    if value != expected or embedded != expected or batch.get('cost') != expected['cost']:
        raise ValueError('original native cost output, receipt and current cost differ')
    if any('campaign cost' in x['command'] and batch['id'] in x['command'] for x in rows if x['index'] > command['index']):
        raise ValueError('a later prior cost operation requires its own anchor')
    return {'previous_worker': _ref(job / 'previous-worker.json'), 'previous_native': _ref(job / 'previous-native.json'),
            'events': _ref(event), 'event': command['id'], 'receipt': _ref(receipt), 'cost': expected['cost'],
            'count_helper': count_ref}


REFUSALS = re.compile(r'permission\s*(?:denied|error)|PermissionError|approval.{0,20}(?:reject|denied)|'
                      r'unacceptable risk|different worker slot|may.have.written|uncertain.submit|'
                      r'"http"\s*:\s*(?:401|403)|business_permission_denied|nas.{0,10}(?:refus|temperature)', re.I)


def _pure_local_python(source, spec, helper_refs, seen=None):
    """Conservative static local-file-only diagnostic subset, never eval/exec.

    Only explicitly hash-pinned helper source can be inspected recursively.
    Unknown calls/imports (including hidden subprocess/HTTP writes) fail closed.
    """
    seen = set() if seen is None else seen
    tree = ast.parse(source)
    names = {'len', 'set', 'dict', 'list', 'str', 'int', 'next', 'any', 'all', 'sorted',
             'print', 'range', 'enumerate', 'zip', 'isinstance', 'Path', 'type', 'sum', 'tuple', 'bool',
             'min', 'max', 'round'}
    names.update(n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    protected = {'len', 'set', 'dict', 'list', 'str', 'int', 'next', 'any', 'all', 'sorted', 'print', 'range',
                 'enumerate', 'zip', 'isinstance', 'Path', 'type', 'sum', 'tuple', 'bool', 'min', 'max', 'round',
                 'json', 'hashlib', 'copy', 'pathlib', 'open', 'exec'}
    forbidden = {'eval', 'compile', '__import__', 'getattr', 'setattr', 'delattr', 'globals', 'locals', 'vars',
                 'builtins', 'object', 'input', 'breakpoint'}
    attrs = {'read_text', 'read_bytes', 'write_text', 'write_bytes', 'Path', 'exists', 'glob', 'mkdir', 'get', 'update', 'append',
             'index', 'strip', 'items', 'values', 'keys', 'loads', 'dumps', 'deepcopy', 'sha256', 'hexdigest',
             'encode', 'splitlines', 'resolve', 'replace', 'iterdir', 'is_file'}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and (node.id in forbidden or node.id.startswith('__')
                or isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in protected)
                or isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in protected
                or isinstance(node, ast.Attribute) and node.attr.startswith('_') and not (
                    node.attr == '__name__' and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name) and node.value.func.id == 'type'
                    and len(node.value.args) == 1 and not node.value.keywords)):
            raise ValueError('diagnostic rebinding/dynamic or private object access forbidden')
        if isinstance(node, ast.Import):
            if any(x.name not in ('json', 'hashlib', 'copy', 'pathlib') or x.asname for x in node.names):
                raise ValueError('diagnostic imports must be pure local helpers')
        elif isinstance(node, ast.ImportFrom):
            if node.module != 'pathlib' or [(x.name, x.asname) for x in node.names] != [('Path', None)]:
                raise ValueError('unknown diagnostic import')
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == 'exec':
                if (len(node.args) != 1 or not isinstance(node.args[0], ast.Call)
                        or not isinstance(node.args[0].func, ast.Attribute) or node.args[0].func.attr != 'read'
                        or not isinstance(node.args[0].func.value, ast.Call)
                        or not isinstance(node.args[0].func.value.func, ast.Name)
                        or node.args[0].func.value.func.id != 'open'
                        or len(node.args[0].func.value.args) != 1
                        or not isinstance(node.args[0].func.value.args[0], ast.Constant)):
                    raise ValueError('dynamic diagnostic execution is forbidden')
                path = Path(node.args[0].func.value.args[0].value).resolve(strict=True)
                declared = spec.get('local_helpers', {}).get(str(path))
                if declared is None or _ref(path)['sha256'] != declared:
                    raise ValueError('diagnostic helper needs its explicitly reviewed source hash')
                helper_refs[str(path)] = _ref(path)
                if str(path) not in seen:
                    seen.add(str(path))
                    _pure_local_python(path.read_text(), spec, helper_refs, seen)
            elif isinstance(node.func, ast.Name):
                if node.func.id == 'open':
                    # open is admitted only as the exact read nested inside exec above.
                    parents = [p for p in ast.walk(tree) if isinstance(p, ast.Attribute) and p.value is node and p.attr == 'read']
                    if not parents or len(node.args) != 1 or node.keywords:
                        raise ValueError('arbitrary diagnostic open is forbidden')
                elif node.func.id not in names:
                    # A defined function may come from a reviewed local helper.
                    declared_names = set()
                    for path in helper_refs:
                        declared_names.update(n.name for n in ast.walk(ast.parse(Path(path).read_text())) if isinstance(n, ast.FunctionDef))
                    if node.func.id not in declared_names:
                        raise ValueError('unknown diagnostic call may have side effects: ' + node.func.id)
            elif not isinstance(node.func, ast.Attribute) or node.func.attr not in attrs | {'read'}:
                raise ValueError('unknown diagnostic operation may write externally: ' + ast.dump(node.func))
    return tree


def _quality_helper(path, args, spec, d, helper_refs):
    """Admit the frozen legacy agent quality-check wrapper, no other verb."""
    declared = spec.get('quality_helper') or {}
    if (str(path) != declared.get('path') or _ref(path)['sha256'] != declared.get('sha256')
            or len(args) != 2 or not args[0].isdigit() or args[1] != 'quality-check'):
        raise ValueError('only the reviewed local quality-check helper may run')
    # The complete helper AST must equal this finite orchestration shape.
    text = path.read_text()
    tree = ast.parse(text)
    first = tree.body[0]
    try:
        dependency = first.value.args[0].func.value.args[0].value
    except AttributeError as exc:
        raise ValueError('quality helper dependency shape differs') from exc
    c = ec.load(d)
    model = spec['model']
    expected = f'''exec(open({dependency!r}).read())
import subprocess,sys
source_id=int(sys.argv[1]);verb=sys.argv[2];assert verb in ['quality-sheet','quality-check'];d=A/f'repairs/{{source_id}}';b=d/'batch.json';r=d/'candidate.json';prompt=A/'baseline/prompt_c13.txt';assert isinstance(json.loads(b.read_text())['items'],list);assert isinstance(json.loads(r.read_text())['items'],list);assert prompt.read_text().strip()
args=[{_entry()!r},'agent',verb,'--batch',str(b),'--file',str(r),'--prompt-file',str(prompt)]
if verb=='quality-sheet':args += ['--out',str(d/'quality.json'),'--model',{model!r},'--executor',{c['executor']!r},'--run-id',{c['models'][model]['run_id']!r}]
else:args += ['--review',str(d/'quality.json')]
p=subprocess.run(args,text=True,capture_output=True,cwd={str(ec.workspace_dir())!r});(d/(verb+'.stdout.log')).write_text(p.stdout);(d/(verb+'.stderr.log')).write_text(p.stderr)
if verb=='quality-check':(d/'quality-check.json').write_text(p.stdout)
print(p.stdout[:2500]);print(p.stderr[:1500]);sys.exit(p.returncode)
'''
    if ast.dump(tree) != ast.dump(ast.parse(expected)):
        raise ValueError('quality helper contains other operations')
    _pure_local_python(f'exec(open({dependency!r}).read())', spec, helper_refs)
    helper_refs[str(path)] = _ref(path)


def _diagnostic_command(command, spec, d):
    shell = _shell(command)
    helper_refs, public = {}, []
    lines = shell.splitlines()
    pos = 0
    while pos < len(lines):
        line = lines[pos]
        if line == "python3 - <<'PY'":
            end = next((i for i in range(pos + 1, len(lines)) if lines[i] == 'PY'), None)
            if end is None:
                raise ValueError('unterminated diagnostic heredoc')
            _pure_local_python('\n'.join(lines[pos + 1:end]), spec, helper_refs)
            pos = end + 1
            continue
        args = shlex.split(line)
        if not args:
            pos += 1
            continue
        if args[0] == 'python3' and len(args) >= 2:
            _quality_helper(Path(args[1]).resolve(strict=True), args[2:], spec, d, helper_refs)
        elif args[0] == _entry():
            if any(ch in line for ch in ('$', '`', ';', '\r', '|', '<')):
                raise ValueError('compound/expanded diagnostic public command forbidden')
            plain = line[:-5].rstrip() if line.endswith('2>&1') else line
            if any(ch in plain for ch in ('&',)):
                raise ValueError('background or additional diagnostic public command forbidden')
            if '>' in args:
                cut = args.index('>')
                if cut + 1 >= len(args) or args[cut:] not in ([args[cut], args[cut + 1]], [args[cut], args[cut + 1], '2>&1']):
                    raise ValueError('only one literal diagnostic output redirection permitted')
                target = Path(args[cut + 1])
                if not target.is_absolute() or not target.resolve().is_relative_to((d / 'audit').resolve()):
                    raise ValueError('diagnostic redirected output must be private campaign audit')
                args = args[:cut]
            elif '>' in plain:
                raise ValueError('unparsed diagnostic shell redirection forbidden')
            if args[1:] == ['--help']:
                pass
            elif args[1:3] not in (['agent', 'quality-sheet'], ['agent', 'quality-check'],
                                   ['repair', 'preview'], ['repair', 'status'], ['nodes', 'preview']):
                raise ValueError('failed public write or other command needs separate reconciliation')
            else:
                required = {('agent', 'quality-sheet'): {'--batch', '--file', '--out', '--model', '--executor', '--run-id', '--prompt-file'},
                            ('agent', 'quality-check'): {'--batch', '--file', '--review', '--prompt-file'},
                            ('repair', 'preview'): {'--file', '--out'}, ('repair', 'status'): {'--repair-id', '--out'},
                            ('nodes', 'preview'): {'--file', '--out'}}[tuple(args[1:3])]
                flags = args[3::2]
                accepted = [required]
                if args[1] == 'agent':
                    accepted.append(required - {'--prompt-file'})
                if len(args[3:]) % 2 or set(flags) not in accepted or len(flags) != len(set(flags)):
                    raise ValueError('exact diagnostic public flags required')
            public.append(args[1:3])
        elif args[0] != 'rg' or any(x in args for x in (';', '|', '&&', '||', '>', '<')) or any(x in line for x in ('$', '`', ';')):
            raise ValueError('unknown failed diagnostic shell command')
        elif any(x.startswith('--') or x.startswith('-') and x not in ('-n', '-m', '-i', '-F') for x in args[1:]):
            raise ValueError('ripgrep preprocessors/unknown options cannot authorize diagnostic recovery')
        pos += 1
    return {'helpers': sorted(helper_refs.values(), key=lambda x: x['path']), 'public': public}


def _successful_output(row, audit):
    """A shell's zero exit never clears a real nested refusal/unknown write.

    Inspect operation-shaped top-level output, not quoted historical facts in
    a source snapshot or a request's nested replacement text.
    """
    if MESSAGE in row['output']:
        raise ValueError('a successful shell masked another actual cost refusal')
    for line in row['output'].splitlines():
        text = line.strip()
        if text.startswith(('PermissionError:', 'permission denied', 'This action was rejected due to unacceptable risk')):
            raise ValueError('a successful shell masked an actual permission refusal')
    rest = row['output'].lstrip()
    decoder = json.JSONDecoder()
    while rest.startswith('{'):
        try:
            value, end = decoder.raw_decode(rest)
        except ValueError:
            break
        rest = rest[end:].lstrip()
        if not isinstance(value, dict):
            continue
        if (value.get('http') in (401, 403) or value.get('exit_code') in (75, 76)
                or value.get('may_have_written') is True or value.get('state') in ('uncertain', 'uncertain_submit')
                or value.get('code') in ('permission_denied', 'forbidden', 'approval_rejected', 'nas_refused')):
            raise ValueError('a successful shell masked an actual refusal or unknown write')
        kind = value.get('recovery_kind')
        if kind and kind not in ('none', 'diagnostic_unavailable'):
            # Exact local final audit summary reports the already preserved
            # cost incident; it is not a second attempted operation.
            if not (kind == 'business_permission_denied' and value.get('audit_run') == str(audit / 'audit-run.json')
                    and value.get('quality_check') is True and value.get('coordinator_acceptance') == 'pending'):
                raise ValueError('a successful shell reported an unresolved recovery incident')


def _diagnostic(row, spec, d):
    command = _diagnostic_command(row['command'], spec, d)
    text = row['output']
    refs = []
    for name in spec.get('failure_receipts', {}).get(row['id'], []):
        path = Path(name).resolve(strict=True)
        if not path.is_relative_to((d / 'audit').resolve()):
            raise ValueError('failure evidence must remain in the campaign audit')
        refs.append(_ref(path))
        text += '\n' + path.read_text()
    if row['exit_code'] != 1 or REFUSALS.search(text) or MESSAGE in text:
        raise ValueError('other real refusals/unknown writes remain blocked')
    if not text.strip():
        args = shlex.split(_shell(row['command']))
        if not args or args[0] != 'rg':
            raise ValueError('empty failed-command output requires original diagnostic evidence')
        kind = 'search_no_match'
    elif text.lstrip().startswith('usage: extract_cli.py campaign|') and _shell(row['command']) == shlex.join([_entry(), '--help']):
        kind = 'top_level_help'
    elif 'Traceback (most recent call last):' in text and re.search(
            r'(?:KeyError|NameError|TypeError|StopIteration|FileNotFoundError|AssertionError|JSONDecodeError|GovernanceError):?', text):
        kind = 'local_schema_path_diagnostic'
    elif 'quality_review_failed' in text or '"ok": false' in text and '"errors": [' in text:
        kind = 'quality_validation'
    elif 'URLError:' in text and 'Operation timed out' in text and 'repair status ' in row['command']:
        kind = 'readonly_status_transport'
    elif '"http": 404' in text and 'repair status ' in row['command'] and ('not_found' in text or 'not found' in text):
        kind = 'readonly_unknown_repair_id'
    else:
        raise ValueError('unclassified failed command cannot be dispositioned as cost-only')
    return {'event': row['id'], 'kind': kind, 'receipts': refs, 'command_evidence': command}


def _bound_refs(value, d, refs):
    if isinstance(value, dict):
        if isinstance(value.get('path'), str) and isinstance(value.get('sha256'), str):
            path = Path(value['path']).resolve(strict=True)
            if not path.is_relative_to(d.resolve()) or not _unchanged(value):
                raise ValueError('quality evidence reference outside campaign or changed')
            already = str(path) in refs
            refs[str(path)] = _ref(path)
            if path.suffix == '.json' and not already:
                _bound_refs(_read(path), d, refs)
        for key, child in value.items():
            if key not in ('path', 'sha256'):
                _bound_refs(child, d, refs)
    elif isinstance(value, list):
        for child in value:
            _bound_refs(child, d, refs)


def _quality_evidence(c, d, audit, batch):
    final, baseline = audit / 'final', audit / 'baseline'
    refs = {}
    run = _read(audit / 'audit-run.json')
    if (run.get('campaign') != c['id'] or run.get('batch') != batch['id'] or run.get('model') != batch['model']
            or run.get('source_count') != len(batch['ids']) or run.get('new_campaign_sources') != 0
            or run.get('final_reviewed') != len(batch['ids']) or run.get('remaining_ids', []) != []
            or run.get('inflight_nas_or_repair') is not False
            or run.get('native_session') != c['slot_sessions'][batch['model']][str(ec.batch_slot(c, batch))]):
        raise ValueError('complete same-owner source audit with no inflight writes required')
    for name in ('batch.json', 'effective-results.json', 'quality.json', 'quality-check.json', 'prompt-c13.txt',
                 'source-decision-index.json', 'repair-status-index.json', 'original-integrity.json',
                 'review-history.json', 'global-metadata-limitations.json', 'business-permission-denial.json',
                 'rework-cost.json', 'freeze-manifest.json'):
        refs[str(final / name)] = _ref(final / name)
    source_batch, results, review = (_read(final / name) for name in ('batch.json', 'effective-results.json', 'quality.json'))
    if (source_batch != _read(d / 'batches' / (batch['id'] + '.json'))
            or {x['id'] for x in source_batch['items']} != set(batch['ids'])
            or review.get('identity') != quality.extraction_identity(batch['model'], c['executor'], c['models'][batch['model']]['run_id'])
            or _read(final / 'quality-check.json') != {'ok': True, 'errors': []}
            or quality.validate_review(source_batch, results, review, (final / 'prompt-c13.txt').read_text())):
        raise ValueError('complete frozen public quality evidence required')
    index = _read(baseline / 'input-index.json')
    for key in ('batch', 'results', 'quality', 'prompt_c13'):
        v = index[key]
        original, snapshot = Path(v['original']).resolve(strict=True), Path(v['snapshot']).resolve(strict=True)
        if (not original.is_relative_to(d.resolve()) or not snapshot.is_relative_to(baseline.resolve())
                or _ref(original)['sha256'] != v['sha256'] or _ref(snapshot)['sha256'] != v['sha256']):
            raise ValueError('original batch/results/quality/prompt bytes changed')
        refs[str(original)], refs[str(snapshot)] = _ref(original), _ref(snapshot)
    if (final / 'prompt-c13.txt').read_bytes() != Path(index['prompt_c13']['snapshot']).read_bytes():
        raise ValueError('final quality prompt differs from the original frozen rules')
    frozen = _read(final / 'freeze-manifest.json')
    for name, sha in frozen['files'].items():
        p = (final / name).resolve(strict=True)
        if not p.is_relative_to(final.resolve()) or _ref(p)['sha256'] != sha:
            raise ValueError('final quality freeze differs')
        refs[str(p)] = _ref(p)
    for path in [audit / 'audit-run.json', baseline / 'input-index.json', final / 'source-decision-index.json',
                 final / 'repair-status-index.json']:
        refs[str(path)] = _ref(path)
        _bound_refs(_read(path), d, refs)
    decisions = _read(final / 'source-decision-index.json')['sources']
    if {x['id'] for x in decisions} != set(batch['ids']) or len(decisions) != len(batch['ids']):
        raise ValueError('every original source needs an explicit final decision')
    for row in decisions:
        decision = _read(row['final_decision']['path'])
        if decision != next(x for x in review['items'] if x['id'] == row['id']):
            raise ValueError('saved decisions differ from approved quality evidence')
    repairs = _read(final / 'repair-status-index.json')
    seen = set()
    sources = {x['id']: x for x in source_batch['items']}
    for row in repairs['new_requests'] + repairs['prior_requests']:
        p = Path(row['status_receipt']['path'])
        response = _read(p)
        status = response.get('repair', {})
        request = status.get('request', {})
        source = sources.get(row['id'], {})
        replacement = request.get('replacement', {})
        if (response.get('http') != 200 or status.get('repair_id') != row['repair_id'] or row['repair_id'] in seen
                or status.get('state') != 'applied' or status.get('result', {}).get('verified') is not True
                or request.get('source_description') != source.get('source_description')
                or str(request.get('source_obs_id')) != str(source.get('source_obs_id'))
                or replacement.get('episode_body') != source.get('episode_body')
                or replacement.get('reference_time') != source.get('reference_time')
                or replacement.get('extractor_model') != batch['model']
                or replacement.get('extractor_tool') != c['executor']
                or replacement.get('run_id') != c['models'][batch['model']]['run_id']):
            raise ValueError('all original source repair terminals must be applied and verified')
        if row in repairs['new_requests']:
            result = next(x for x in results['items'] if x['id'] == row['id'])
            if (replacement.get('entities') != result.get('entities') or replacement.get('edges', []) != result.get('edges', [])):
                raise ValueError('new repair terminal differs from final reviewed effective result')
        seen.add(row['repair_id'])
    return {'references': sorted(refs.values(), key=lambda x: x['path']), 'repair_ids': sorted(seen),
            'quality_scope': 'evidence integrity only; source semantics/shared metadata/provenance require separate coordinator acceptance'}


@contextlib.contextmanager
def _idle(d, root, batches):
    with contextlib.ExitStack() as stack:
        stack.enter_context(receipts.idle(root))
        stack.enter_context(receipts.idle(root / 'native'))
        for bid in batches:
            stack.enter_context(receipts.idle(d / 'operations' / bid))
        yield


def _evidence(c, d, model, slot, spec, *, supervisor_snapshot=None, locked=True):
    from tools.campaign_supervision import events
    root = ec.supervisor_dir(d, model, slot)
    worker, native = (_read(root / name) for name in ('worker.json', 'native/latest.json'))
    supervisor = supervisor_snapshot if supervisor_snapshot is not None else _read(root / 'latest.json')
    sid = c.get('slot_sessions', {}).get(model, {}).get(str(slot))
    if (c['executor'] != 'codex' or c['dispatch']['state'] != 'draining' or not sid
            or worker.get('session') != sid or spec.get('session') != sid
            or sum(sid == x for slots in c['slot_sessions'].values() for x in slots.values()) != 1
            or worker.get('mode') != 'quality_rework' or len(worker.get('batches', [])) != 1):
        raise ValueError('drained original single-batch owner rework required')
    bid = worker['batches'][0]
    batch = ec.find_batch(c, bid)
    if batch['model'] != model or ec.batch_slot(c, batch) != slot or batch['status'] != 'submitted':
        raise ValueError('submitted original own-slot batch required')
    event = Path(worker['events']).resolve(strict=True)
    job = event.parent
    if not job.is_relative_to((root / 'quality-reworks').resolve()) or event.name != 'events.jsonl':
        raise ValueError('original public rework job required')
    scope, terminal = _read(job / 'scope.json'), _read(job / 'exit-status.json')
    if (any(scope.get(k) != v for k, v in {'campaign': c['id'], 'model': model, 'slot': slot, 'session': sid,
                                         'batches': [bid], 'new_source_quota': 0}.items())
            or not _unchanged(scope['assignment']) or Path(scope['assignment']['path']).resolve() != job / 'assignment.md'
            or native.get('state') != 'completed' or native.get('exit_code') != 0 or not native.get('attempt')
            or terminal.get('exit_code') != 0 or terminal.get('session') != sid
            or supervisor.get('state') != 'completed' or supervisor.get('kind') != 'quality_rework'
            or supervisor.get('phase') != 'coordinator_required' or supervisor.get('exit_code') != 2
            or supervisor.get('session') != sid or supervisor.get('batches') != [bid]
            or Path(supervisor.get('events', '/missing')).resolve() != event
            or abs(_time(native['ended_at']) - _time(terminal['ended_at'])) > 2
            or abs(_time(native['ended_at']) - _time(supervisor['ended_at'])) > 2
            or abs(_time(native['started_at']) - _time(supervisor['started_at'])) > 2):
        raise ValueError('exact completed native0/public2 cost refusal terminal required')
    outcome = events(event)
    if (outcome.get('session') != sid or outcome.get('terminal') != 'turn.completed'
            or outcome.get('hard_stop') or outcome.get('approval_refused')
            or outcome.get('recovery_kind') != 'business_permission_denied'):
        raise ValueError('only an explicitly reported completed cost business refusal qualifies')
    audit = Path(spec['audit_run']).resolve(strict=True).parent
    if not audit.is_relative_to((d / 'audit').resolve()) or Path(spec['audit_run']).name != 'audit-run.json':
        raise ValueError('original campaign audit-run required')
    commands, refusals, diagnostics = _commands(event, sid), [], []
    for row in commands:
        if row['exit_code']:
            if MESSAGE in row['output']:
                if row['exit_code'] != 1 or row['output'].strip() != 'cost exit 1  ' + MESSAGE:
                    raise ValueError('exact single cost-note denial output required')
                parsed = _cost_command(row['command'], d, audit, bid)
                refusals.append({'event': row['id'], **parsed})
            else:
                diagnostics.append(_diagnostic(row, spec, d))
        else:
            _successful_output(row, audit)
    if len(refusals) != 1:
        raise ValueError('exactly one rejected cost-note operation required')
    anchor = _cost_anchor(d, job, spec, batch, native['started_at'])
    denied = _read(audit / 'final/business-permission-denial.json')
    if (denied.get('exit_code') != 1 or denied.get('reason') != MESSAGE or denied.get('retry_count') != 0
            or denied.get('alternate_entry_attempted') is not False
            or Path(denied['stdout']['path']).read_text() != '' or Path(denied['stderr']['path']).read_text() != MESSAGE + '\n'):
        raise ValueError('exact preserved original denial stdout/stderr required')
    quality_evidence = _quality_evidence(c, d, audit, batch)
    formal = {}
    for line in (d / 'log.jsonl').read_text().splitlines():
        row = json.loads(line)
        if row.get('batch') == bid:
            formal[row['id']] = row
    if set(formal) != set(batch['ids']) or any(x.get('status') not in ('ok', 'empty')
            or x.get('model') != model or x.get('executor') != c['executor']
            or x.get('run_id') != c['models'][model]['run_id'] for x in formal.values()):
        raise ValueError('original source terminals must remain complete and known')
    operation = ({**receipts.read(d / 'operations' / bid / 'latest.json'), 'lease_held': False}
                 if locked else receipts.status(d / 'operations' / bid))
    if operation.get('lease_held') is not False or operation.get('state') != 'completed' or operation.get('exit_code') != 0:
        raise ValueError('no in-flight or uncertain original batch operation allowed')
    return {'model': model, 'slot': slot, 'session': sid, 'batch': bid, 'attempt': native['attempt'],
            'files': [_ref(p) for p in (root / 'worker.json', root / 'native/latest.json',
                       event, job / 'scope.json', job / 'exit-status.json', job / 'assignment.md')],
            'original_supervisor': supervisor,
            'worker_sha256': quality.digest(worker), 'cost_anchor': anchor, 'denial': refusals[0], 'diagnostics': diagnostics,
            'quality': quality_evidence, 'formal_sha256': quality.digest(formal), 'operation': operation,
            'commands': [{'id': x['id'], 'command': x['command'], 'exit_code': x['exit_code'],
                          'output_sha256': quality.digest(x['output'])} for x in commands]}


def preview(cid, model, slot, *, evidence_file, reason, out):
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None or not reason.strip():
        raise ValueError('explicit coordinator reason required')
    ec.tracking_guard()
    d = ec.campaign_dir(cid)
    spec = _read(evidence_file)
    with ec.locked(d):
        c = ec.load(d)
        slot = ec.worker_slot(c, model, slot)
        root = ec.supervisor_dir(d, model, slot)
        batches = _read(root / 'worker.json').get('batches', [])
        with _idle(d, root, batches):
            evidence = _evidence(c, d, model, slot, spec)
            plan = {'version': 'rework-completion-preview-v1', 'kind': KIND, 'campaign': cid,
                    'at': receipts.now(), 'reason': reason, 'input': _ref(evidence_file), 'evidence': evidence,
                    'contract': _contract(c, model), 'operator_review_required':
                    'Read every original command/output, diagnostic closure and repair terminal. Confirm no unknown writes or other actual refusals. This is not semantic acceptance or permission to retry cost.',
                    'scope': 'same original slot/session only; cost protection, original rejection/native0/public2 remain; no new claims or semantic acceptance'}
            plan['sha256'] = quality.digest(plan)
            if Path(out).exists():
                raise ValueError('preview output already exists')
            receipts.write(Path(out), plan)
            return plan


def apply(cid, *, plan_file, confirm, reviewer, reason):
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None or not reviewer.strip() or not reason.strip():
        raise ValueError('explicit coordinator reviewer/reason required')
    ec.tracking_guard()
    plan = _read(plan_file)
    if (plan.get('kind') != KIND or plan.get('campaign') != cid or confirm != plan.get('sha256')
            or quality.digest({k: v for k, v in plan.items() if k != 'sha256'}) != confirm):
        raise ValueError('exact reviewed completion preview digest required')
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c, old = ec.load(d), plan['evidence']
        root = ec.supervisor_dir(d, old['model'], old['slot'])
        with _idle(d, root, [old['batch']]):
            active = receipts.read(root / 'rework-completion-active.json')
            if active:
                proof = _read(active['path'])
                if proof.get('approved_preview_sha256') == confirm and valid(c, d, old['model'], old['slot'], locked=True):
                    return _applied(active['path'], proof)
                raise ValueError('different or stale completion disposition already exists')
            if not _unchanged(plan['input']):
                raise ValueError('completion input evidence changed')
            fresh = _evidence(c, d, old['model'], old['slot'], _read(plan['input']['path']))
            if fresh != old or _contract(c, old['model']) != plan['contract']:
                raise ValueError('completion preview is stale')
            directory = root.resolve() / 'rework-completions' / uuid.uuid4().hex
            directory.mkdir(parents=True)
            # Preserve every original failure file; never rewrite worker/native.
            proof = {'version': 'rework-completion-v1', 'kind': KIND, 'campaign': cid,
                     'model': old['model'], 'slot': old['slot'], 'session': old['session'],
                     'at': receipts.now(), 'reviewer': reviewer, 'reason': reason,
                     'approved_preview': _ref(plan_file), 'approved_preview_sha256': confirm,
                     'evidence': old, 'input': plan['input'], 'contract': plan['contract']}
            proof['sha256'] = quality.digest(proof)
            receipts.write(directory / 'proof.json', proof)
            receipts.write(root / 'rework-completion-active.json', _ref(directory / 'proof.json'))
            return _applied(directory / 'proof.json', proof)


def _applied(path, proof):
    return {'kind': KIND, 'model': proof['model'], 'slot': proof['slot'], 'session': proof['session'],
            'original_native_exit_code': 0, 'original_public_exit_code': 2, 'rejected_cost_retried': False,
            'original_cost_unchanged': True, 'semantic_acceptance': 'not_granted', 'dispatch_resumed': False,
            'proof': str(path)}


def valid(c, d, model, slot, *, locked=False):
    try:
        root = ec.supervisor_dir(d, model, slot)
        ref = receipts.read(root / 'rework-completion-active.json')
        path = Path(ref['path']).resolve(strict=True)
        if not path.is_relative_to((root / 'rework-completions').resolve()) or not _unchanged(ref):
            return False
        proof = _read(path)
        if (proof['sha256'] != quality.digest({k: v for k, v in proof.items() if k != 'sha256'})
                or (proof['kind'], proof['campaign'], proof['model'], proof['slot'], proof['session']) != (
                    KIND, c['id'], model, slot, c['slot_sessions'][model][str(slot)])
                or not _unchanged(proof['approved_preview']) or not _unchanged(proof['input'])):
            return False
        plan = _read(proof['approved_preview']['path'])
        if (plan['sha256'] != proof['approved_preview_sha256']
                or quality.digest({k: v for k, v in plan.items() if k != 'sha256'}) != plan['sha256']
                or plan['evidence'] != proof['evidence'] or plan['contract'] != proof['contract']):
            return False
        # Dispatch legitimately changes only after separate quality acceptance.
        current, frozen = _contract(c, model), dict(proof['contract'])
        current.pop('dispatch', None)
        frozen.pop('dispatch', None)
        if current != frozen:
            return False
        if not locked and receipts.status(root / 'native').get('lease_held') is not False:
            return False
        checked = dict(c, dispatch=proof['contract']['dispatch'])
        return _evidence(checked, d, model, slot, _read(proof['input']['path']),
                         supervisor_snapshot=proof['evidence']['original_supervisor'], locked=locked) == proof['evidence']
    except (OSError, KeyError, ValueError, TypeError, SyntaxError, receipts.Busy):
        return False
