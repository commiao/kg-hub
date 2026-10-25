"""Controlled single-worker -> slot-pool migration. Never submits or kills work."""
from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from pathlib import Path

from tools import extract_campaign as ec
from utils import process_receipts as receipts


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def core(c):
    return {k: c.get(k) for k in ('id', 'executor', 'quota', 'batch', 'models', 'start_contract',
                                 'prompt_version', 'prompt_sha256', 'admission', 'chain_mode')}


def batch_core(b):
    return {k: b.get(k) for k in ('id', 'model', 'ids', 'chain')}


def drain(cid: str, reason: str) -> dict:
    if not reason.strip():
        raise ValueError('drain reason required')
    ec.tracking_guard()
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d)
        ec.rules_text(d, c)
        if (c.get('dispatch') or {}).get('state') != 'draining':
            c['dispatch'] = {'state': 'draining', 'id': uuid.uuid4().hex,
                             'requested_at': ec._now(), 'reason': reason}
            ec.save(d, c)
        return {'campaign': cid, 'dispatch': c['dispatch'], 'action': 'finish_current_batch_then_exit'}


def logical_slot(c: dict, b: dict):
    if b.get('slot') is not None:
        return b['slot']
    if b['id'] in (c.get('migration') or {}).get('legacy_batches', []):
        return 1
    return None


def valid_migration(c: dict, d: Path) -> bool:
    marker = c.get('migration') or {}
    ident = marker.get('id', '')
    if len(ident) != 32 or any(x not in '0123456789abcdef' for x in ident):
        return False
    record = receipts.read(d / 'migrations' / ident / 'receipt.json')
    if (not record or record.get('id') != ident or digest(record) != marker.get('receipt_sha256')
            or record.get('parallel') != ec.parallelism(c)
            or record.get('core_sha256') != digest(core(c))
            or marker.get('legacy_batches') != list(record.get('batches', {}))):
        return False
    for bid, original in record['batches'].items():
        b = next((b for b in c['batches'] if b['id'] == bid), None)
        if (b is None or batch_core(b) != original['identity']
                or file_sha(d / 'batches' / (bid + '.json')) != original['source_sha256']):
            return False
    return True


def _inside(path, root):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('worker proof outside its model supervisor directory')
    return resolved


def _prepare(c, d, parallel, proof):
    from tools import campaign_supervision as supervision
    if type(parallel) is not int or parallel < 2 or ec.parallelism(c) != 1 or c.get('migration'):
        raise ValueError('migration requires an unmigrated single-worker campaign and parallel >= 2')
    if (c.get('dispatch') or {}).get('state') != 'draining':
        raise ValueError('freeze new claims with campaign drain before migration')
    if c['executor'] != 'codex' or set(proof.get('workers', {})) != set(c['models']):
        raise ValueError('exact Codex campaign worker coverage required')
    ec.rules_text(d, c)
    handoff = receipts.read(d / 'handoff-status.json').get('workers', {})
    workers, counts, pending, locks = {}, {}, {}, []
    sessions = set()
    for model in c['models']:
        root = ec.supervisor_dir(d, model)
        worker = proof['workers'][model]
        known = handoff.get(model, {})
        sid = worker.get('session')
        if not sid or sid in sessions or sid != known.get('native_session_id'):
            raise ValueError('original native session identity missing, duplicated or changed')
        sessions.add(sid)
        native = _inside(worker['runtime_receipt_dir'], root)
        events = _inside(worker['events'], root)
        if (native != Path(known.get('runtime_receipt_dir', '/missing')).resolve()
                or events != Path(known.get('stdout', '/missing')).resolve()):
            raise ValueError('proof differs from the recorded active worker')
        state = receipts.status(native)
        outcome = supervision.events(events)
        if (state['lease_held'] is not False or state['state'] != 'completed'
                or state.get('exit_code') != 0 or not state.get('ended_at')
                or outcome.get('session') != sid or outcome.get('terminal') != 'turn.completed'
                or outcome.get('recovery_kind') not in (None, 'none', 'diagnostic_unavailable', 'migration_drained')):
            raise ValueError(f'{model}: original native worker is live or its clean terminal is unverified')
        if outcome.get('hard_stop'):
            from tools.campaign_recovery import stops_reconciled
            if not stops_reconciled(c, d, model, events, outcome):
                raise ValueError(f'{model}: unresolved original NAS refusal')
        # macOS temp paths can alias /var and /private/var. Lock each inode's
        # resolved directory once; acquiring the same flock twice conflicts.
        directories = {p.resolve() for p in (root, root / 'native', native, *root.glob('*.native'))}
        for directory in directories:
            status = receipts.status(directory)
            if status['lease_held'] is not False or status['state'] == 'uncertain' or status.get('requires_reconciliation'):
                raise ValueError(f'{model}: live or uncertain original execution remains')
            if (directory / 'lease.lock').exists():
                locks.append(directory)
        owned = [b for b in c['batches'] if b['model'] == model]
        unfinished = [b for b in owned if b['status'] not in ('submitted', 'released')]
        if len(unfinished) > 1:
            raise ValueError('more than one unfinished batch for the original worker')
        for b in owned:
            if not (d / 'batches' / (b['id'] + '.json')).is_file():
                raise ValueError('original frozen batch file missing')
            ids = set(b['ids']) - set(b.get('released_ids') or [])
            statuses = ec.final_statuses(d, b['id'], ids)
            if b['status'] == 'submitted' and (set(statuses) != ids or any(s not in ('ok', 'empty') for s in statuses.values())):
                raise ValueError(f'{b["id"]}: incomplete formal results')
            if b in unfinished and (b['status'] not in ('claimed', 'dry_run') or statuses):
                raise ValueError(f'{b["id"]}: partial submit or unsupported pending state; finish/reconcile first')
            op_dir = d / 'operations' / b['id']
            if b in unfinished:
                op = receipts.status(op_dir)
                if (op['state'] not in ('not_started', 'completed', 'reconciled_not_written')
                        or op.get('requires_reconciliation') or op.get('may_have_written')
                        or op.get('exit_code', 0) != 0 and op['state'] != 'reconciled_not_written'):
                    raise ValueError(f'{b["id"]}: unfinished operation terminal unverified')
            if (op_dir / 'lease.lock').exists():
                status = receipts.status(op_dir)
                if status['lease_held'] is not False or status.get('requires_reconciliation') or status['state'] == 'uncertain':
                    raise ValueError(f'{b["id"]}: operation still live or uncertain')
                locks.append(op_dir.resolve())
        workers[model] = {'session': sid, 'events': str(events), 'events_sha256': file_sha(events),
                          'runtime_receipt_dir': str(native), 'runtime_sha256': file_sha(native / 'latest.json')}
        confirmed = supervision.completed(c, d, model)
        expected = sum(len(b['ids']) - len(b.get('released_ids') or []) for b in owned if b['status'] == 'submitted')
        if confirmed != expected:
            raise ValueError(f'{model}: formal results do not match the frozen model/run/executor identity')
        counts[model] = {'confirmed': confirmed, 'quota': c['quota']}
        pending[model] = [b['id'] for b in unfinished]
    return {'workers': workers, 'counts': counts, 'pending_original_batches': pending}, locks


def preview(cid: str, parallel: int, proof: dict, output: Path) -> dict:
    ec.tracking_guard()
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d)
        checked, _ = _prepare(c, d, parallel, proof)
        plan = {'version': 'campaign-slot-migration-v1', 'id': uuid.uuid4().hex, 'campaign': cid,
                'parallel': parallel, 'created_at': ec._now(), 'campaign_sha256': file_sha(d / 'campaign.json'),
                'log_sha256': file_sha(d / 'log.jsonl'), 'proof': proof, **checked}
        plan['plan_sha256'] = digest(plan)
        with output.open('x') as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        return plan


def apply(cid: str, plan_path: Path) -> dict:
    ec.tracking_guard()
    plan = json.loads(plan_path.read_text())
    unsigned = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    ident = plan.get('id', '')
    if (len(ident) != 32 or any(x not in '0123456789abcdef' for x in ident)
            or plan.get('version') != 'campaign-slot-migration-v1' or plan.get('campaign') != cid
            or digest(unsigned) != plan.get('plan_sha256')):
        raise ValueError('migration preview integrity/identity mismatch')
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d)
        if (c.get('migration') or {}).get('id') == plan['id'] and valid_migration(c, d):
            return {'campaign': cid, 'applied': True, 'already_applied': True, 'parallel': ec.parallelism(c)}
        if (file_sha(d / 'campaign.json') != plan['campaign_sha256'] or file_sha(d / 'log.jsonl') != plan['log_sha256']):
            raise ValueError('campaign changed after preview; prepare a fresh migration preview')
        checked, lock_dirs = _prepare(c, d, plan['parallel'], plan['proof'])
        if any(checked[k] != plan[k] for k in checked):
            raise ValueError('worker proof changed after preview')
        with contextlib.ExitStack() as stack:
            for directory in sorted(set(lock_dirs)):
                stack.enter_context(receipts.idle(directory))
            for w in checked['workers'].values():
                if (file_sha(Path(w['events'])) != w['events_sha256']
                        or file_sha(Path(w['runtime_receipt_dir']) / 'latest.json') != w['runtime_sha256']):
                    raise ValueError('original execution changed during migration locking')
            seeds = {}
            for model, worker in checked['workers'].items():
                target = ec.supervisor_dir(d, model, 1) / 'worker.json'
                seed = {'session': worker['session'], 'events': worker['events'],
                        'migration_id': plan['id'], 'adopted_from': worker['runtime_receipt_dir']}
                existing = receipts.read(target)
                if existing and existing != seed:
                    raise ValueError('destination slot already belongs to another execution')
                for slot in range(1, plan['parallel'] + 1):
                    root = ec.supervisor_dir(d, model, slot)
                    if slot != 1 and (root / 'worker.json').exists():
                        raise ValueError('destination slot already has a worker identity')
                    if (root / 'latest.json').exists() or (root / 'native' / 'latest.json').exists():
                        raise ValueError('destination slot already has runtime history')
                seeds[target] = seed
            backup = d / 'migrations' / plan['id']
            backup.mkdir(parents=True, exist_ok=True)
            before = backup / 'campaign-before.json'
            if before.exists() and file_sha(before) != plan['campaign_sha256']:
                raise ValueError('migration backup differs from the preview')
            if not before.exists():
                before.write_bytes((d / 'campaign.json').read_bytes())
            receipts.write(backup / 'preview.json', plan)
            record = {'version': plan['version'], 'id': plan['id'], 'parallel': plan['parallel'],
                      'core_sha256': digest(core(c)), 'original_cursor': c['cursor'], **checked,
                      'batches': {b['id']: {'identity': batch_core(b),
                          'source_sha256': file_sha(d / 'batches' / (b['id'] + '.json'))} for b in c['batches']}}
            for target, seed in seeds.items():
                receipts.write(target, seed)
            receipts.write(backup / 'receipt.json', record)
            c['parallel'] = plan['parallel']
            c['migration'] = {'id': plan['id'], 'receipt_sha256': digest(record), 'applied_at': ec._now(),
                              'legacy_batches': list(record['batches'])}
            c['slot_sessions'] = {m: {'1': w['session']} for m, w in checked['workers'].items()}
            c['dispatch'] = {'state': 'running', 'migration_id': plan['id'], 'updated_at': ec._now()}
            ec.save(d, c)
    return {'campaign': cid, 'applied': True, 'parallel': plan['parallel'], **checked}


def rollback(cid: str) -> dict:
    """Only undo activation before any pool slot has ever started or claimed."""
    ec.tracking_guard()
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d)
        if not valid_migration(c, d):
            raise ValueError('verified migration required')
        ident = c['migration']['id']
        backup = d / 'migrations' / ident
        original = json.loads((backup / 'campaign-before.json').read_text())
        if c['batches'] != original['batches'] or c['cursor'] != original['cursor']:
            raise ValueError('work progressed after migration; rollback would discard history')
        with contextlib.ExitStack() as stack:
            for model in c['models']:
                stack.enter_context(receipts.idle(ec.supervisor_dir(d, model)))
                for slot in range(1, ec.parallelism(c) + 1):
                    root = ec.supervisor_dir(d, model, slot)
                    if (root / 'latest.json').exists() or (root / 'native/latest.json').exists():
                        raise ValueError('a slot has execution history; reconcile rather than rollback')
                    stack.enter_context(receipts.idle(root))
            receipts.write(backup / 'rollback.json', {'at': ec._now(), 'id': ident, 'before_activation_only': True})
            ec.save(d, original)
    return {'campaign': cid, 'rolled_back': True, 'dispatch': 'draining', 'parallel': 1}
