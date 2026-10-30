"""Classify an original host terminal without rewriting native process evidence.

This intentionally narrow validator handles a single host failed completion of
a bounded source-selector/public-preview command. It does not waive any other
failed command, verify deployment history, or certify a complete write inventory.
"""
import ast
import datetime
import json
import re
import shlex


CAPACITY = 'Selected model is at capacity. Please try a different model.'
PY_TEMPLATE = r'''import json,pathlib
r=pathlib.Path(ROOT);p=r/AUDIT;b=json.loads((r/BATCH).read_text())
for n in IDS:
 s=next(x for x in b['items'] if x['id']==n);(p/f'{n}.selector.json').write_text(json.dumps({k:s[k] for k in ('source_description','source_obs_id')},ensure_ascii=False,indent=2)+'\n')'''
SHELL_TEMPLATE = '''if [ -n "$KG_HUB_PYTHON" ]; then py="$KG_HUB_PYTHON"; elif [ -x "$HOME/.local/share/kg-hub/venv/bin/python" ]; then py="$HOME/.local/share/kg-hub/venv/bin/python"; else py=python3; fi
for n in IDS; do "$py" "${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extraction_repair.py" preview --file "AUDIT/$n.selector.json" --out "AUDIT/$n.live-preview.json" || exit; done'''
CALL = re.compile(r'const r=await tools.exec_command\(\{cmd:("(?:[^"\\]|\\.)*"),"workdir":("(?:[^"\\]|\\.)*"),"yield_time_ms":1000,"max_output_tokens":500\}\);text\(r\)\n')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def one(rows, reason):
    require(len(rows) == 1, reason)
    return rows[0]


def milliseconds(timestamp):
    value = datetime.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    require(value.tzinfo is not None, 'timestamp must include timezone')
    return value.timestamp() * 1000


def bounded_preview(script, scope):
    match = re.fullmatch(r"python3 - <<'PY'\n(.*)\nPY\n(.*)", script, re.S)
    require(match, 'unknown command wrapper')
    tree = ast.parse(match[1])
    try:
        ids = ast.literal_eval(tree.body[4].iter)
    except (AttributeError, IndexError, ValueError, TypeError) as exc:
        raise ValueError('unknown selector loop') from exc
    require(isinstance(ids, tuple) and 0 < len(ids) <= 50
            and all(type(n) is int for n in ids)
            and len(ids) == len(set(ids)) and set(ids) <= set(scope['source_ids']),
            'selector IDs outside original batch')
    require(re.fullmatch(r'audit/[A-Za-z0-9_-]+', scope['audit_relative']), 'unbounded audit path')
    require(re.fullmatch(r'[A-Za-z0-9_-]+', scope['batch']), 'invalid batch')
    expected = PY_TEMPLATE.replace('ROOT', repr(scope['campaign_root'])).replace(
        'AUDIT', repr(scope['audit_relative'])).replace('BATCH', repr('batches/' + scope['batch'] + '.json')).replace('IDS', repr(ids))
    require(ast.dump(tree) == ast.dump(ast.parse(expected)), 'selector AST differs')
    shell = SHELL_TEMPLATE.replace('IDS', ' '.join(map(str, ids))).replace(
        'AUDIT', scope['campaign_root'] + '/' + scope['audit_relative'])
    require(match[2] == shell, 'shell is not the exact public read-only preview loop')
    return list(ids)


def inspect(native_rows, host_rows, scope):
    """Return evidence classification, explicitly NOT a resume permission.

    scope is independently bound by the caller to frozen assignment/source data.
    Raw-file hashes, path/symlink constraints, release identity, live write
    reconciliation and other local failures are intentionally separate gates.
    """
    pending, completed, sessions, local_failures = {}, set(), [], []
    terminal = False
    for row in native_rows:
        require(not terminal, 'native event after terminal')
        kind = row.get('type')
        if kind == 'thread.started':
            sessions.append(row.get('thread_id'))
        require(kind != 'turn.completed', 'native turn was successful')
        if kind == 'turn.failed':
            require(row.get('error', {}).get('message') == CAPACITY, 'not exact native capacity')
            terminal = True
        item = row.get('item', {})
        if item.get('type') != 'command_execution':
            continue
        key = item.get('id')
        if kind == 'item.started':
            require(key and key not in pending and key not in completed, 'duplicate native start')
            pending[key] = item
        elif kind == 'item.completed':
            start = pending.pop(key, None)
            require(start and start.get('command') == item.get('command'), 'unmatched native completion')
            require(type(item.get('exit_code')) is int and item.get('status') in ('failed', 'completed'), 'missing native exit')
            require(item['exit_code'] != 0 or item['status'] == 'completed', 'contradictory native exit')
            completed.add(key)
            if item['exit_code']:
                local_failures.append(key)
    require(sessions == [scope['session']] and terminal, 'wrong native session/terminal')
    key, start = one(list(pending.items()), 'must have exactly one unmatched native command')
    argv = shlex.split(start['command'])
    require(len(argv) == 3 and argv[:2] == ['/bin/zsh', '-lc'], 'unsupported native shell')
    ids = bounded_preview(argv[2], scope)

    calls = []
    for row in host_rows:
        p = row.get('payload', {})
        if p.get('type') == 'custom_tool_call' and p.get('name') == 'exec':
            m = CALL.fullmatch(p.get('input', ''))
            if m and json.loads(m[1]) == argv[2]:
                calls.append((row, m))
    call, match = one(calls, 'unique original matching invocation required')
    payload = call['payload']
    require(json.loads(match[2]) == scope['cwd'], 'wrong invocation cwd')
    require(payload.get('internal_chat_message_metadata_passthrough', {}).get('turn_id') == scope['turn'], 'wrong invocation turn')
    outputs = [r for r in host_rows if r.get('payload', {}).get('type') == 'custom_tool_call_output'
               and r['payload'].get('call_id') == payload.get('call_id')]
    output = one(outputs, 'unique original yielded output required')
    require(output['payload'].get('internal_chat_message_metadata_passthrough', {}).get('turn_id') == scope['turn'], 'wrong yielded turn')
    yields = []
    for part in output['payload'].get('output', []):
        try:
            value = json.loads(part.get('text', ''))
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and 'session_id' in value:
            yields.append(value)
    yielded = one(yields, 'unique process session required')
    require(type(yielded['session_id']) is int and yielded['session_id'] > 0
            and 'exit_code' not in yielded and yielded.get('output') == '', 'not a pending empty-output yield')
    hosts = [r for r in host_rows if r.get('type') == 'event_msg'
             and r.get('payload', {}).get('type') == 'item_completed'
             and r['payload'].get('item', {}).get('type') == 'CommandExecution'
             and (r['payload']['item'].get('process_id') == str(yielded['session_id'])
                  or r['payload']['item'].get('command') == argv)]
    host = one(hosts, 'unique matching host completion required')
    p = host['payload']; item = p['item']
    require(isinstance(item.get('id'), str) and re.fullmatch(
        r'exec-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', item['id']), 'invalid host item ID')
    require(p.get('thread_id') == scope['session'] and p.get('turn_id') == scope['turn'], 'foreign host session/turn')
    require(item.get('command') == argv and item.get('process_id') == str(yielded['session_id']), 'host argv/process mismatch')
    require(item.get('cwd') == 'file://' + scope['cwd'] and item.get('source') == 'unified_exec_startup', 'foreign host origin')
    require(item.get('status') == 'failed' and type(item.get('exit_code')) is int
            and item['exit_code'] == -1 and item.get('aggregated_output') == '', 'not original host failed/-1')
    end = one([r for r in host_rows if r.get('payload', {}).get('type') == 'task_complete'
               and r['payload'].get('turn_id') == scope['turn']], 'unique host failed turn required')
    require(end['payload'].get('error') == {'message': CAPACITY, 'codex_error_info': 'server_overloaded'}, 'wrong host turn failure')
    a, b = p.get('started_at_ms'), p.get('completed_at_ms')
    require(type(a) is int and type(b) is int, 'host times missing')
    require(milliseconds(scope['native_started_at']) <= milliseconds(call['timestamp']) <= a
            <= milliseconds(output['timestamp']) <= milliseconds(end['timestamp']) <= b
            <= milliseconds(scope['native_ended_at']), 'host time order or native lifetime mismatch')
    require(abs(milliseconds(host['timestamp']) - b) <= 1, 'host timestamp differs')
    duration = item.get('duration', {})
    require(type(duration.get('secs')) is int and type(duration.get('nanos')) is int
            and 0 <= duration['nanos'] < 1_000_000_000, 'invalid duration')
    require(abs(b - a - (duration['secs'] * 1000 + duration['nanos'] / 1_000_000)) <= 2, 'duration differs')
    return {'schema': 'host-command-terminal-evidence-v1', 'native_item': key,
            'host_item': item.get('id'), 'process_id': item['process_id'],
            'host_status': 'failed', 'host_exit_code': -1, 'source_ids': ids,
            'disposition': 'host_failed_completion_of_bounded_readonly_preview_command',
            'normal_os_exit_proven': False, 'preview_success_proven': False,
            'other_failed_commands_not_waived': local_failures,
            'all_recovery_gates_validated': False, 'restart_allowed': False}
