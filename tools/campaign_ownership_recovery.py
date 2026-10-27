"""Explicit recovery from an interrupted, non-writing cross-slot review rejection.

This does not approve the rejected review or transfer its batch. The coordinator
reviews a frozen command inventory, acknowledges its digest, and permits only the
original native session's own slot to continue. Original failure bytes survive.
"""
from __future__ import annotations

import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import uuid

from tools import extract_campaign as ec
from tools.campaign_recovery import file_sha
from utils import process_receipts as receipts
from utils.extraction_quality import digest

KIND = 'cross_slot_review_scope_corrected'
MESSAGE = 'batch belongs to a different worker slot'


def _read(path):
    return json.loads(Path(path).read_text())


def _ref(path):
    path = Path(path).resolve(strict=True)
    return {'path': str(path), 'sha256': file_sha(path)}


def _unchanged(ref):
    return file_sha(Path(ref['path'])) == ref['sha256']


def _time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone-aware native timestamps required')
    return stamp.timestamp()


def _contract(c, model):
    return {'quota': c['quota'], 'batch': c['batch'], 'parallel': ec.parallelism(c),
            'slot_sessions': c['slot_sessions'], 'dispatch': c['dispatch'],
            'identity': {'model': model, 'run_id': c['models'][model]['run_id'], 'executor': c['executor'],
                         'prompt_sha256': c.get('prompt_sha256'), 'prompt_version': c.get('prompt_version'),
                         'chain': c.get('chain'), 'chain_mode': c.get('chain_mode'),
                         'runtime': ec.runtime_settings(c)}}


def _public_command(command):
    """One literal public command; never evaluate shell expressions."""
    entry = str(Path(os.environ.get('KG_HUB_CODE', '~/.local/share/kg-hub/current')).expanduser() / 'tools/extract')
    args = shlex.split(command)
    if len(args) == 3 and Path(args[0]).name in ('zsh', 'sh', 'bash') and args[1] in ('-lc', '-c'):
        if not args[2].lstrip().startswith((entry, shlex.quote(entry))):
            return None
        args = shlex.split(args[2])
    if not args or args[0] != entry:
        return None
    if any(x in args for x in (';', '&&', '||', '|', '>', '2>&1')):
        raise ValueError('compound public command cannot authorize continuation')
    return args[1:]


def _flags(args, allowed):
    if len(args) % 2:
        raise ValueError('exact public command flags required')
    values = {}
    for key, value in zip(args[::2], args[1::2]):
        if key not in allowed or key in values:
            raise ValueError('unexpected or duplicate public command flag')
        values[key] = value
    if set(values) != set(allowed):
        raise ValueError('incomplete public command identity')
    return values


@contextlib.contextmanager
def _idle_campaign(d):
    with contextlib.ExitStack() as stack:
        for directory in sorted({p.parent.resolve() for parent in (d / 'supervisors', d / 'operations')
                                 for p in parent.rglob('lease.lock')}):
            stack.enter_context(receipts.idle(directory))
        yield


def _evidence(c, d, model, slot, event_path, exit_path, stop_path):
    from tools.campaign_supervision import events
    if c.get('executor') != 'codex' or (c.get('dispatch') or {}).get('state') != 'draining':
        raise ValueError('idle drained Codex campaign required')
    root = ec.supervisor_dir(d, model, slot)
    worker = receipts.read(root / 'worker.json')
    native = receipts.read(root / 'native/latest.json')
    session = c.get('slot_sessions', {}).get(model, {}).get(str(slot))
    if (not session or worker.get('session') != session or
            sum(session == sid for ss in c.get('slot_sessions', {}).values() for sid in ss.values()) != 1):
        raise ValueError('unique original worker slot/session required')
    if any(b['status'] not in ('submitted', 'released') for b in c['batches']):
        raise ValueError('all original source batches must have known submitted/released terminals')
    event_path, exit_path, stop_path = map(lambda p: Path(p).resolve(strict=True), (event_path, exit_path, stop_path))
    if event_path.name != 'stdout.jsonl' or exit_path != event_path.with_name('exit-status.json'):
        raise ValueError('paired native invocation stdout and exit required')
    process_path = event_path.with_name('process.json')
    process, terminal, stop = _read(process_path), _read(exit_path), _read(stop_path)
    if (process.get('original_session') != session or process.get('model') != model or process.get('slot') != slot
            or process.get('pid') != native.get('pid') or Path(process.get('stdout', '/missing')).resolve() != event_path
            or Path(process.get('exit_receipt', '/missing')).resolve() != exit_path
            or process.get('kind') != 'submitted_source_peer_repair_only'
            or process.get('new_source_quota') != 0):
        raise ValueError('paired submitted-source invocation identity differs')
    if (native.get('state') != 'completed' or native.get('exit_code') != 1
            or terminal.get('exit_code') != 1 or terminal.get('session') != session
            or abs(_time(terminal['ended_at']) - _time(native['ended_at'])) > 2
            or abs(_time(process['started_at']) - _time(native['started_at'])) > 2):
        raise ValueError('exact original interrupted native terminal required')
    if (stop.get('original_session') != session or stop.get('native_attempt') != native.get('attempt')
            or stop.get('child_pid') != native.get('child_pid')
            or not str(stop.get('signal', '')).startswith('SIGINT to Codex child PID only;')
            or not 0 <= _time(native['ended_at']) - _time(stop['at']) <= 10):
        raise ValueError('exact coordinator interruption evidence required')
    old_events = Path(worker.get('events', '/missing')).resolve(strict=True)
    if not old_events.is_relative_to(ec.supervisor_dir(d, model).resolve()):
        raise ValueError('original worker event ownership differs')
    previous = events(old_events)
    if (previous.get('session') != session or previous.get('terminal') != 'turn.completed'
            or previous.get('hard_stop') or previous.get('approval_refused')):
        raise ValueError('previous own-slot terminal must be clean')
    started, pending, commands, failures, repair_applies, repair_statuses = [], {}, [], [], [], {}
    for line in event_path.read_text().splitlines():
        row = json.loads(line)
        if row.get('type') == 'thread.started':
            started.append(row.get('thread_id'))
        if row.get('type') in ('error', 'turn.failed', 'turn.completed'):
            raise ValueError('provider failure or different native terminal cannot use ownership recovery')
        item = row.get('item', {})
        if item.get('type') != 'command_execution':
            continue
        cid = item.get('id')
        if row.get('type') == 'item.started':
            if not cid or cid in pending or any(x['id'] == cid for x in commands):
                raise ValueError('unique native command start required')
            pending[cid] = item.get('command')
        elif row.get('type') == 'item.completed':
            command = item.get('command', '')
            if pending.pop(cid, None) != command or type(item.get('exit_code')) is not int:
                raise ValueError('matched native command terminal required')
            output = str(item.get('aggregated_output', ''))
            if (item['exit_code'] and 'This action was rejected due to unacceptable risk' in output
                    or item['exit_code'] in (75, 76)):
                raise ValueError('approval/NAS refusal remains a hard stop')
            parsed = _public_command(command)
            commands.append({'id': cid, 'command': command, 'exit_code': item['exit_code'],
                             'output_sha256': digest(output)})
            if item['exit_code']:
                if item['exit_code'] != 1 or output.strip() != MESSAGE or not parsed or parsed[:2] != ['campaign', 'review']:
                    raise ValueError('only the exact non-writing cross-slot review rejection qualifies')
                flags = _flags(parsed[2:], ('--id', '--batch', '--obs', '--verdict', '--kind', '--note'))
                if flags['--id'] != c['id'] or flags['--kind'] != 'targeted' or flags['--verdict'] not in ('ok', 'error'):
                    raise ValueError('rejected command scope differs')
                failures.append({'event': cid, **flags})
            elif parsed and parsed[:2] == ['repair', 'apply']:
                flags = _flags(parsed[2:], ('--file', '--out'))
                request = _read(flags['--file'])
                reply = _read(flags['--out'])
                if (reply.get('status') != 'repaired' or reply.get('verified') is not True
                        or reply.get('repair_id') != request.get('repair_id')):
                    raise ValueError('original repair apply terminal is not verified')
                repair_applies.append({'request': _ref(flags['--file']), 'apply': _ref(flags['--out']),
                                       'repair_id': request['repair_id'], 'request_value': request})
            elif parsed and parsed[:2] == ['repair', 'status']:
                flags = _flags(parsed[2:], ('--repair-id', '--out'))
                value = _read(flags['--out']).get('repair', {})
                if (value.get('repair_id') != flags['--repair-id'] or value.get('state') != 'applied'
                        or value.get('result', {}).get('verified') is not True):
                    raise ValueError('original repair status must be applied and verified')
                repair_statuses[flags['--repair-id']] = {'ref': _ref(flags['--out']), 'value': value}
            elif parsed and parsed[:2] not in (['repair', 'preview'], ['agent', 'quality-sheet'], ['agent', 'quality-check']):
                raise ValueError('other public operations require separate reconciliation')
    if started != [session] or pending or len(failures) != 1:
        raise ValueError('one original session and one fully terminated ownership rejection required')
    failure = failures[0]
    batch = ec.find_batch(c, failure['--batch'])
    if batch['model'] != model or ec.batch_slot(c, batch) == slot or batch['status'] != 'submitted':
        raise ValueError('rejected review must address a submitted batch owned by another slot of this model')
    batch_path = d / 'batches' / (batch['id'] + '.json')
    source = next((x for x in _read(batch_path)['items'] if str(x['id']) == failure['--obs']), None)
    if source is None:
        raise ValueError('rejected source is not in the original batch')
    latest = {}
    for line in (d / 'log.jsonl').read_text().splitlines():
        row = json.loads(line)
        if row.get('batch') == batch['id']:
            latest[row.get('id')] = row
    if set(latest) != set(batch['ids']) or any(
            x.get('status') not in ('ok', 'empty') or x.get('model') != model
            or x.get('executor') != c['executor'] or x.get('run_id') != c['models'][model]['run_id']
            for x in latest.values()):
        raise ValueError('complete original formal source terminals required')
    for repair in repair_applies:
        request = repair.pop('request_value')
        if (request.get('source_description') != source['source_description']
                or request.get('source_obs_id') != source['source_obs_id']
                or repair['repair_id'] not in repair_statuses):
            raise ValueError('all prior writes must be verified repairs of the rejected source')
        status = repair_statuses[repair['repair_id']]
        normalized = dict(request)
        normalized.setdefault('version', 'extraction-repair-v1')
        status_request = dict(status['value'].get('request') or {})
        status_request.setdefault('version', 'extraction-repair-v1')
        if (status_request != normalized
                or status['value']['result'].get('episode_uuid') != latest[source['id']].get('episode_uuid')):
            raise ValueError('prior repair/source episode or exact reviewed request differs')
        repair['status'] = status['ref']
    return {'session': session, 'model': model, 'slot': slot, 'source_owner_slot': ec.batch_slot(c, batch),
            'rejected_batch': batch['id'], 'rejected_obs': source['id'], 'failure': failure,
            'native': _ref(root / 'native/latest.json'), 'worker': _ref(root / 'worker.json'),
            'previous_events': _ref(old_events), 'events': _ref(event_path), 'exit': _ref(exit_path),
            'process': _ref(process_path), 'stop': _ref(stop_path), 'batch': _ref(batch_path),
            'formal_source_terminals_sha256': digest(latest), 'commands': commands, 'verified_prior_repairs': repair_applies}


def preview(cid, model, slot, *, events, terminal, stop, reason, out):
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None or not reason.strip():
        raise ValueError('explicit coordinator recovery reason required')
    ec.tracking_guard()
    d = ec.campaign_dir(cid)
    with ec.locked(d), _idle_campaign(d):
        c = ec.load(d)
        evidence = _evidence(c, d, model, slot, events, terminal, stop)
        plan = {'version': 'ownership-continuation-preview-v1', 'campaign': cid, 'kind': KIND,
                'at': receipts.now(), 'reason': reason, 'evidence': evidence,
                'contract': _contract(c, model),
                'operator_review_required': 'Read every command/output and prior repair status. Confirm there are no hidden/unknown writes or other refusals. This approval never authorizes the rejected cross-slot review.',
                'resume_scope': 'same original session, own slot only; rejected review remains unwritten; no batch transfer'}
        plan['sha256'] = digest(plan)
        if Path(out).exists():
            raise ValueError('preview path already exists')
        receipts.write(Path(out), plan)
        return plan


def apply(cid, *, plan_file, confirm, reviewer, reason):
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None or not reviewer.strip() or not reason.strip():
        raise ValueError('explicit coordinator reviewer and authorization reason required')
    ec.tracking_guard()
    plan = _read(plan_file)
    unsigned = {k: v for k, v in plan.items() if k != 'sha256'}
    if plan.get('campaign') != cid or plan.get('kind') != KIND or confirm != plan.get('sha256') or digest(unsigned) != confirm:
        raise ValueError('approved exact ownership preview digest required')
    d = ec.campaign_dir(cid)
    with ec.locked(d), _idle_campaign(d):
        c = ec.load(d)
        old = plan['evidence']
        root = ec.supervisor_dir(d, old['model'], old['slot'])
        active_path = root / 'ownership-continuation-active.json'
        active = receipts.read(active_path)
        if active:
            proof_path = Path(active['path'])
            proof = _read(proof_path)
            if (not proof_path.resolve().is_relative_to((root / 'ownership-continuations').resolve())
                    or not _unchanged(active) or proof.get('approved_preview_sha256') != confirm):
                raise ValueError('different ownership continuation already exists')
            if valid(c, d, old['model'], old['slot']):
                return _applied(proof_path, proof)
            # Recover only activation interrupted between the active pointer and
            # its exact worker update. No new proof or native attempt is made.
            if not _unchanged(old['worker']):
                raise ValueError('ownership continuation activation state differs')
        fresh = _evidence(c, d, old['model'], old['slot'], old['events']['path'], old['exit']['path'], old['stop']['path'])
        contract = _contract(c, old['model'])
        if fresh != old or contract != plan['contract']:
            raise ValueError('ownership preview is stale; read and review a new preview')
        if active:
            worker = receipts.read(root / 'worker.json')
            worker.update(events=proof['copied_events']['path'], ownership_recovery=KIND)
            if digest(worker) != proof['worker_after_sha256'] or not _unchanged(proof['copied_events']):
                raise ValueError('incomplete ownership activation evidence differs')
            receipts.write(root / 'worker.json', worker)
            return _applied(proof_path, proof)
        directory = root.resolve() / 'ownership-continuations' / uuid.uuid4().hex
        directory.mkdir(parents=True)
        # Store the actual failed transcript inside its model owner directory.
        # This is an exact copy, not a fabricated successful turn.
        event_copy = directory / 'original-failed.events.jsonl'
        event_copy.write_bytes(Path(old['events']['path']).read_bytes())
        (directory / 'previous-worker.json').write_bytes((root / 'worker.json').read_bytes())
        worker = receipts.read(root / 'worker.json')
        worker.update(events=str(event_copy), ownership_recovery=KIND)
        proof = {'version': 'ownership-continuation-v1', 'kind': KIND, 'campaign': cid,
                 'model': old['model'], 'slot': old['slot'], 'session': old['session'],
                 'approved_preview': _ref(plan_file), 'approved_preview_sha256': confirm,
                 'reviewer': reviewer, 'reason': reason, 'at': receipts.now(), 'evidence': old,
                 'copied_events': _ref(event_copy), 'worker_after_sha256': digest(worker)}
        proof['sha256'] = digest(proof)
        receipts.write(directory / 'proof.json', proof)
        receipts.write(root / 'ownership-continuation-active.json', _ref(directory / 'proof.json'))
        receipts.write(root / 'worker.json', worker)
        return _applied(directory / 'proof.json', proof)


def _applied(path, proof):
    e = proof['evidence']
    return {'kind': KIND, 'model': e['model'], 'slot': e['slot'], 'session': e['session'],
            'original_exit_code': 1, 'rejected_review_replayed': False, 'batch_owner_unchanged': True,
            'scope': 'original session own slot only; rejected review remains unwritten', 'proof': str(path)}


def valid(c, d, model, slot):
    """A changed native attempt, source history or proof revokes this exception."""
    try:
        root = ec.supervisor_dir(d, model, slot)
        ref = receipts.read(root / 'ownership-continuation-active.json')
        p = Path(ref['path']).resolve(strict=True)
        if not p.is_relative_to((root / 'ownership-continuations').resolve()) or not _unchanged(ref):
            return False
        proof = _read(p)
        if (proof['sha256'] != digest({k: v for k, v in proof.items() if k != 'sha256'})
                or proof['kind'] != KIND or (proof['campaign'], proof['model'], proof['slot']) != (c['id'], model, slot)
                or proof['session'] != c['slot_sessions'][model][str(slot)]):
            return False
        e = proof['evidence']
        for key in ('native', 'events', 'exit', 'process', 'stop', 'batch', 'previous_events'):
            if not _unchanged(e[key]):
                return False
        for repair in e['verified_prior_repairs']:
            if not all(_unchanged(repair[key]) for key in ('request', 'apply', 'status')):
                return False
        if not _unchanged(proof['approved_preview']) or not _unchanged(proof['copied_events']):
            return False
        approved = _read(proof['approved_preview']['path'])
        if (approved.get('sha256') != proof['approved_preview_sha256']
                or digest({k: v for k, v in approved.items() if k != 'sha256'}) != approved.get('sha256')
                or any(approved['contract'][key] != c.get(key) for key in ('quota', 'batch', 'slot_sessions'))
                or approved['contract']['parallel'] != ec.parallelism(c)
                or approved['contract']['identity'] != _contract(c, model)['identity']):
            return False
        worker = receipts.read(root / 'worker.json')
        if digest(worker) != proof['worker_after_sha256'] or worker['events'] != proof['copied_events']['path']:
            return False
        batch = ec.find_batch(c, e['rejected_batch'])
        if batch['status'] != 'submitted' or ec.batch_slot(c, batch) != e['source_owner_slot']:
            return False
        latest = {}
        for line in (d / 'log.jsonl').read_text().splitlines():
            row = json.loads(line)
            if row.get('batch') == batch['id']:
                latest[row.get('id')] = row
        return digest(latest) == e['formal_source_terminals_sha256']
    except (OSError, KeyError, ValueError, TypeError):
        return False
