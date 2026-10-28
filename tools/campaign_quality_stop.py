"""Admit an audited quality-stop transcript to drained own-source rework only.

This never makes quality_process_violation a normal production recovery kind.
The supported original turn only reads local evidence / public state and saves
private audit files. It has no failed command or unresolved native operation.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import shlex

from tools.campaign_rework_completion import _commands, _shell
from tools.campaign_ownership_recovery import _ref


def _path(node, paths):
    if isinstance(node, ast.Name):
        return paths.get(node.id)
    if (isinstance(node, ast.Call) and ast.unparse(node.func) in ('Path', 'pathlib.Path')
            and len(node.args) == 1 and not node.keywords
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        p = Path(node.args[0].value)
        if p.is_absolute():
            return p.resolve()
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
        base = _path(node.left, paths)
        if base is not None:
            return (base / node.right.value).resolve()
    return None


def _file(path, refs):
    if path is None or path.name.startswith('.env'):
        raise ValueError('quality stop requires literal non-secret local evidence paths')
    if not path.exists():
        raise ValueError('quality stop local evidence is missing')
    if path.is_file():
        refs[str(path)] = _ref(path)


def _python(body, audit, refs):
    tree = ast.parse(body)
    paths, assigned = {}, set()
    imports = {'ast', 'hashlib', 'json', 'pathlib', 'datetime'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(x.name not in imports or x.asname for x in node.names):
                raise ValueError('quality stop Python imports must be local audit utilities')
        elif isinstance(node, ast.ImportFrom):
            if node.module != 'pathlib' or [(x.name, x.asname) for x in node.names] != [('Path', None)]:
                raise ValueError('quality stop Python imports must be local audit utilities')
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                               ast.With, ast.AsyncWith, ast.Await, ast.Global, ast.Nonlocal,
                               ast.AugAssign, ast.NamedExpr, ast.Delete)):
            raise ValueError('quality stop Python must be bounded local audit code')
        elif isinstance(node, ast.Attribute) and node.attr.startswith('_'):
            raise ValueError('quality stop Python cannot access dynamic attributes')
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise ValueError('quality stop Python cannot change object or subscript state')
            name = node.targets[0].id
            if name in assigned or name in imports | {'Path', 'print', 'str', 'len', 'range', 'min', 'isinstance'}:
                raise ValueError('quality stop Python cannot rebind audit variables')
            assigned.add(name)
            p = _path(node.value, paths)
            if p is not None:
                paths[name] = p
    safe = {'Path', 'pathlib.Path', 'print', 'str', 'len', 'range', 'min', 'isinstance',
            'ast.parse', 'ast.walk', 'hashlib.sha256', 'json.dumps', 'datetime.datetime.now'}
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.comprehension)):
            targets = {x.id for x in ast.walk(node.target) if isinstance(x, ast.Name)}
            if targets & (set(paths) | imports | {'Path'}):
                raise ValueError('quality stop loops cannot replace audit paths or utilities')
        if not isinstance(node, ast.Call):
            continue
        if ast.unparse(node.func) in safe:
            continue
        if not isinstance(node.func, ast.Attribute):
            raise ValueError('quality stop Python contains an unsupported operation')
        method = node.func.attr
        p = _path(node.func.value, paths)
        if method in ('read_text', 'read_bytes', 'exists'):
            _file(p, refs)
        elif method in ('write_text', 'write_bytes', 'mkdir'):
            if p is None or p == audit or not p.is_relative_to(audit):
                raise ValueError('quality stop Python writes must stay inside campaign audit')
            _file(p, refs)
        elif method == 'splitlines' and isinstance(node.func.value, ast.Call):
            prior = node.func.value.func
            if not isinstance(prior, ast.Attribute) or prior.attr != 'read_text':
                raise ValueError('quality stop splitlines must read local evidence')
        elif method == 'hexdigest' and isinstance(node.func.value, ast.Call):
            if ast.unparse(node.func.value.func) != 'hashlib.sha256':
                raise ValueError('quality stop digest must use SHA256')
        elif method == 'isoformat' and isinstance(node.func.value, ast.Call):
            if ast.unparse(node.func.value.func) != 'datetime.datetime.now':
                raise ValueError('quality stop timestamp must use the local clock')
        else:
            raise ValueError('quality stop Python contains an unsupported operation')


def validate(c, d, model, slot, session, events, batches, native):
    """Return frozen eligibility evidence; never modify the original transcript."""
    from tools import campaign_supervision as supervision
    outcome = supervision.events(events)
    event_ref = _ref(events)
    if (outcome.get('recovery_kind') != 'quality_process_violation'
            or outcome.get('session') != session or outcome.get('terminal') != 'turn.completed'
            or outcome.get('hard_stop') or outcome.get('approval_refused')
            or native.get('state') != 'completed' or native.get('exit_code') != 0):
        raise ValueError('only a clean original quality-stop turn may enter rework')
    commands = _commands(events, session)
    refs, reports = {}, []
    # Native errors, including otherwise recoverable transport notices, need a
    # separate recovery decision; this path only accepts an entirely clean turn.
    import json
    for line in Path(events).read_text().splitlines():
        row = json.loads(line)
        if row.get('type') == 'error':
            raise ValueError('quality stop cannot waive native/provider errors')
        item = row.get('item', {})
        if item.get('type') == 'agent_message':
            try:
                report = json.loads(item.get('text', ''))
            except ValueError:
                continue
            if isinstance(report, dict) and report.get('recovery_kind'):
                reports.append(report)
    if (len(reports) != 1 or reports[0].get('recovery_kind') != 'quality_process_violation'
            or reports[0].get('model') != model or reports[0].get('batch') not in batches
            or reports[0].get('submitted') != {'ok': 0, 'empty': 0, 'error': 0}
            or any(reports[0].get(k) != 0 for k in ('prepare_runs', 'dryrun_mismatch_seen', 'renamed_after_dryrun'))):
        raise ValueError('one same-model submitted-batch quality stop with zero production work required')
    for item in commands:
        if item['exit_code'] != 0:
            raise ValueError('quality stop cannot waive any failed command')
        shell = _shell(item['command'])
        script = re.fullmatch(r"python3 - <<'PY'\n(.*)\nPY", shell, flags=re.S)
        if script:
            _python(script[1], (d / 'audit').resolve(), refs)
            continue
        args = shlex.split(shell)
        if not args or any(x in shell for x in ('\n', '`', '$(', ';', '&&', '||', '|')):
            raise ValueError('quality stop command must be a single reviewed local read')
        if args[0] == 'cat' and len(args) == 2 and Path(args[1]).name == 'SKILL.md':
            _file(Path(args[1]).resolve(), refs)
            continue
        if (args[:2] == ['sed', '-n'] and len(args) == 4
                and re.fullmatch(r'\d+(?:,\d+)?p', args[2])):
            _file(Path(args[3]).resolve(), refs)
            continue
        entries = {str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser()),
                   '${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract'}
        if args[0] not in entries or args[1:2] != ['campaign']:
            raise ValueError('quality stop permits only state reads and private audit writes')
        if len(args) < 5 or args[2] not in ('rules', 'operation-status', 'status', 'report'):
            raise ValueError('quality stop cannot contain production or repair commands')
        if '>' in args:
            if args.count('>') != 1 or args[-2] != '>' or args[2] != 'rules':
                raise ValueError('quality stop supports only rules stdout to a private file')
            p = Path(args[-1]).resolve()
            if not p.is_relative_to(Path('/private/tmp')) or p.suffix != '.txt':
                raise ValueError('quality stop rules stdout must be a private temporary text file')
            _file(p, refs)
            args = args[:-2]
        flags = args[3:]
        if len(flags) % 2 or any(k not in ('--id', '--batch') for k in flags[::2]):
            raise ValueError('quality stop state query flags are not supported')
        if len(set(flags[::2])) != len(flags[::2]):
            raise ValueError('quality stop query cannot repeat identity flags')
        options = dict(zip(flags[::2], flags[1::2]))
        if options.get('--id') != c['id'] or options.get('--batch', reports[0]['batch']) not in batches:
            raise ValueError('quality stop state query differs from approved source scope')
    if not commands:
        raise ValueError('quality stop requires actual completed evidence commands')
    if _ref(events) != event_ref:
        raise ValueError('quality stop events changed during validation')
    return {'kind': 'quality_stop_own_submitted_rework', 'session': session, 'model': model, 'slot': slot,
            'batches': sorted(batches), 'events': event_ref, 'native': dict(native),
            'local_evidence': list(refs.values()), 'commands': len(commands), 'original_report': reports[0],
            'production_recovery_authorized': False, 'semantic_acceptance': False, 'new_source_quota': 0}
