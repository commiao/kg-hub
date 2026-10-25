"""Adopt a legacy pre-dispatch gate timeout without erasing its original evidence."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time
import uuid

from utils import process_receipts as receipts


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def command_scope(command: str, expected: tuple[str, str] | None = None) -> dict | None:
    """Parse a single public command, never evaluate shell text or compound commands."""
    args = shlex.split(command)
    entry = str(Path(os.environ.get('KG_HUB_CODE', '~/.local/share/kg-hub/current')).expanduser() / 'tools/extract')
    if len(args) == 3 and Path(args[0]).name in ('zsh', 'bash', 'sh') and args[1] in ('-c', '-lc'):
        # Unrelated Python here-docs need not be valid shlex word streams.
        if not args[2].lstrip().startswith((entry, shlex.quote(entry))):
            return None
        args = shlex.split(args[2])
    if len(args) < 3 or args[:3] != [entry, 'campaign', 'submit']:
        return None
    flags, log = {}, None
    args = args[3:]
    if args in (['--help'], ['-h']):
        return None
    if expected and all(args.count(flag) == 1 and args.index(flag) + 1 < len(args) for flag in ('--id', '--batch')):
        if (args[args.index('--id') + 1], args[args.index('--batch') + 1]) != expected:
            return None
    while args:
        flag = args.pop(0)
        if flag in ('--id', '--batch') and args and flag not in flags:
            flags[flag] = args.pop(0)
        elif flag == '--dry-run' and flag not in flags:
            flags[flag] = True
        elif flag == '>' and len(args) == 2 and args[1] == '2>&1':
            log = args[0]
            args = []
        else:
            raise ValueError('legacy evidence requires one exact public submit command')
    if not flags.get('--id') or not flags.get('--batch'):
        raise ValueError('legacy command identity missing')
    return {'campaign': flags['--id'], 'batch': flags['--batch'],
            'kind': 'dryrun' if flags.get('--dry-run') else 'submit', 'log': log}


def legacy_proof(exit_path: Path, events_path: Path, session: str, cid: str, bid: str) -> dict:
    if (not exit_path.name.endswith('-exit-status.json')
            or events_path != exit_path.with_name(exit_path.name.removesuffix('-exit-status.json') + '-stdout.jsonl')):
        raise ValueError('legacy exit/events must be the same unique invocation pair')
    before = file_sha(events_path)
    terminal = receipts.read(exit_path)
    if type(terminal.get('exit_code')) is not int or terminal['exit_code'] != 0:
        raise ValueError('legacy native exit not successful')
    ended = datetime.fromisoformat(terminal['ended_at'].replace('Z', '+00:00'))
    if ended.tzinfo is None or ended.timestamp() > time.time() + 30:
        raise ValueError('legacy native exit timestamp must include timezone')
    native_session, last_terminal, last_event, pending, latest = None, None, None, set(), None
    with events_path.open() as stream:
        for line in stream:
            event = json.loads(line)
            last_event = event.get('type')
            if event.get('type') == 'thread.started':
                if native_session is not None:
                    raise ValueError('ambiguous legacy native session')
                native_session = event.get('thread_id')
            if event.get('type') in ('turn.completed', 'turn.failed', 'error'):
                last_terminal = event['type']
            item = event.get('item', {})
            if item.get('type') != 'command_execution':
                continue
            scope = command_scope(item.get('command', ''), (cid, bid))
            if not scope or (scope['campaign'], scope['batch']) != (cid, bid):
                continue
            if not item.get('id'):
                raise ValueError('legacy native command id missing')
            if event.get('type') == 'item.started':
                pending.add(item['id'])
            elif event.get('type') == 'item.completed':
                pending.discard(item['id'])
                latest = {**scope, 'command_id': item['id'], 'exit_code': item.get('exit_code')}
    if native_session != session or last_terminal != 'turn.completed' or last_event != 'turn.completed' or pending:
        raise ValueError('legacy session terminal or command completion unverified')
    if not latest or latest['exit_code'] != 76 or not latest['log']:
        raise ValueError('legacy proof must show the last exact command ended at the NAS gate')
    console = Path(latest['log'])
    if not console.is_absolute() or console.stat().st_mtime > ended.timestamp() + 1:
        raise ValueError('legacy console is not frozen before native exit')
    last = console.read_text().splitlines()[-1]
    pattern = r'(?:排队等锁超过 \d+ 秒|等 NAS 降温超过 \d+ 秒（最后一次 [\d.]+°C）)，未放行 ' + latest['kind']
    if re.fullmatch(pattern, last) is None or before != file_sha(events_path):
        raise ValueError('legacy evidence does not prove the gate never released this operation')
    return {'version': 'legacy-gate-evidence-v1', 'session': session, **latest,
            'events_sha256': before, 'exit_sha256': file_sha(exit_path), 'console_sha256': file_sha(console),
            'events': str(events_path), 'exit': str(exit_path), 'ended_at': terminal['ended_at']}


def evidence_unchanged(evidence: dict) -> bool:
    try:
        return all(file_sha(Path(evidence[path])) == evidence[sha] for path, sha in
                   [('events', 'events_sha256'), ('exit', 'exit_sha256'), ('log', 'console_sha256')])
    except (OSError, KeyError, ValueError):
        return False


def adopt(directory: Path, identity: dict, batch_sha: str, source_keys: list, evidence: dict, verify) -> dict:
    """One owner, fresh complete source check, immutable evidence; never submits."""
    with receipts.idle(directory):
        if (directory / 'latest.json').exists():
            raise ValueError('operation already has a receipt; use normal reconciliation')
        record = {'version': 'process-receipt-v1', 'attempt': uuid.uuid4().hex, 'identity': identity,
                  'kind': evidence['kind'], 'phase': 'starting', 'state': 'legacy_unverified',
                  'may_have_written': False, 'legacy_evidence': evidence, 'batch_sha256': batch_sha,
                  'source_keys': source_keys}
        proof = {'version': 'operation-reconciliation-v1', 'at': receipts.now(),
                 'attempt': record['attempt'], **verify(record)}
        if not evidence_unchanged(evidence):
            raise ValueError('legacy evidence changed during source verification')
        receipts.write(directory / ('reconcile-' + record['attempt'] + '.json'), proof)
        if proof.get('confirmed_not_written') is True:
            record.update(state='reconciled_not_written', phase='legacy_gate_never_released',
                          exit_code=76, requires_reconciliation=False, reconciliation=proof)
            receipts.write(directory / (record['attempt'] + '.json'), record)
            receipts.write(directory / 'latest.json', record)
        return proof


def stops_reconciled(c: dict, d: Path, model: str, events_path: Path, outcome: dict) -> bool:
    """Clear only the exact recorded gate refusals that a coordinator reconciled."""
    from utils.extraction_quality import digest
    from tools.extract_campaign import batch_identity
    stops = outcome.get('hard_stops') or []
    if not stops:
        return False
    for stop in stops:
        scope = command_scope(stop['command'])
        batch = next((b for b in c['batches'] if scope and b['id'] == scope['batch'] and b['model'] == model), None)
        if not batch or scope['campaign'] != c['id']:
            return False
        record = receipts.status(d / 'operations' / batch['id'])
        if (record['state'] != 'reconciled_not_written' or record.get('kind') != scope['kind']
                or record.get('identity') != {'campaign': c['id'], 'batch': batch['id'], **batch_identity(c, batch)}
                or record.get('may_have_written') is not False
                or record.get('reconciliation', {}).get('confirmed_not_written') is not True
                or record.get('reconciliation', {}).get('attempt') != record.get('attempt')):
            return False
        evidence = record.get('legacy_evidence')
        if evidence:
            frozen = json.loads((d / 'batches' / (batch['id'] + '.json')).read_text())
            if (not evidence_unchanged(evidence)
                    or evidence.get('events_sha256') != file_sha(events_path)
                    or evidence.get('command_id') != stop.get('id')
                    or evidence.get('session') != outcome.get('session')
                    or record.get('batch_sha256') != digest(frozen)):
                return False
    return True
