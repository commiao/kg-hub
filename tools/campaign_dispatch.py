"""Lease-checked dispatch resumption and bounded in-quota observation windows."""
from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path

from tools import extract_campaign as ec
from utils import process_receipts as receipts


def window_batches(c, model):
    window = (c.get('dispatch') or {}).get('window') or {}
    original = set(window.get('existing_batches', []))
    return [b for b in c['batches'] if b['model'] == model and b['id'] not in original]


def pause_reason(c, model, slot=None):
    dispatch = c.get('dispatch') or {}
    if dispatch.get('state') == 'draining':
        return 'dispatch_draining'
    window = dispatch.get('window')
    if not window:
        return None
    if window['chain'] != c['chain']:
        return 'observation_chain_changed'
    if slot is not None and slot not in window['slots']:
        return 'observation_slot_not_selected'
    if len(window_batches(c, model)) >= window['batches_per_model']:
        return 'observation_budget_reached'
    return None


def drain_spent_window(c):
    dispatch = c.get('dispatch') or {}
    window = dispatch.get('window')
    if window and all(len(window_batches(c, m)) >= window['batches_per_model'] for m in c['models']):
        dispatch.update(state='draining', reason='observation window fully claimed', updated_at=ec._now())


def resume(cid, *, reason, batches_per_model=None):
    """Never changes quota/history; a bounded window only uses original slot 1."""
    if not reason.strip() or batches_per_model is not None and (type(batches_per_model) is not int or batches_per_model < 1):
        raise ValueError('reason and a positive observation batch budget required')
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT'):
        raise ValueError('only the coordinator may resume dispatch')
    ec.tracking_guard()
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d)
        ec.rules_text(d, c)
        if (c.get('dispatch') or {}).get('state') != 'draining':
            raise ValueError('dispatch must be draining before explicit resumption')
        from tools.campaign_migration import valid_migration
        from tools.campaign_supervision import events, normal_recovery
        from tools.campaign_recovery import stops_reconciled
        if c.get('migration') and not valid_migration(c, d):
            raise ValueError('migration receipt integrity differs')
        lock_dirs = {p.parent.resolve() for parent in (d / 'supervisors', d / 'operations')
                     for p in parent.rglob('lease.lock')}
        # Lock acquisition is nonblocking: a live owner must finish, not be killed.
        with contextlib.ExitStack() as stack:
            for directory in sorted(lock_dirs):
                stack.enter_context(receipts.idle(directory))
            operations = {b['id']: receipts.read(d / 'operations' / b['id'] / 'latest.json') for b in c['batches']}
            for model in c['models']:
                slots = range(1, ec.parallelism(c) + 1) if ec.parallelism(c) > 1 else [None]
                for slot in slots:
                    worker_root = ec.supervisor_dir(d, model, slot)
                    worker = receipts.read(worker_root / 'worker.json')
                    if not worker:
                        continue
                    model_root = ec.supervisor_dir(d, model).resolve()
                    event_path = Path(worker.get('events', '/missing')).resolve()
                    native = worker_root / 'native'
                    if not (native / 'latest.json').is_file():
                        native = Path(worker.get('adopted_from', '/missing'))
                    if not event_path.is_relative_to(model_root) or not native.resolve().is_relative_to(model_root):
                        raise ValueError('worker proof outside its model supervisor directory')
                    terminal = receipts.read(native / 'latest.json')
                    outcome = events(event_path)
                    # A new slot's first turn records its session binding when
                    # its native events end, before another spawn rewrites the
                    # initial worker.json. Require that existing binding rather
                    # than inventing an identity from the event alone.
                    bound_session = (c.get('slot_sessions', {}).get(model, {}).get(str(slot))
                                     if slot is not None else None)
                    session = worker.get('session') or bound_session
                    cleared = bool(outcome.get('hard_stop')) and stops_reconciled(
                        c, d, model, event_path, outcome, operation_records=operations)
                    if (not session or outcome.get('session') != session
                            or slot is not None and bound_session != session
                            or slot is not None and sum(session == sid for bindings in
                                c.get('slot_sessions', {}).values() for sid in bindings.values()) != 1
                            or terminal.get('state') != 'completed' or terminal.get('exit_code') != 0
                            or not terminal.get('ended_at')
                            or outcome.get('terminal') != 'turn.completed'
                            or outcome.get('hard_stop') and not cleared
                            or not normal_recovery(outcome.get('recovery_kind'))
                            and not (cleared and outcome.get('recovery_kind') == 'nas_refused')):
                        raise ValueError('worker clean terminal or recovery outcome unverified')
            for batch in c['batches']:
                op = operations[batch['id']]
                if batch['status'] not in ('submitted', 'released') and (
                        op.get('may_have_written') or op.get('exit_code', 0) != 0
                        and op.get('state') != 'reconciled_not_written'):
                    raise ValueError('pending operation requires reconciliation before resume')
            previous = (c.get('dispatch') or {}).get('window')
            pending = previous and any(b['status'] not in ('submitted', 'released')
                for model in c['models'] for b in window_batches(c, model))
            reuse = (previous and batches_per_model == previous['batches_per_model']
                     and c['chain'] == previous['chain'])
            if pending and not reuse:
                raise ValueError('finish the existing observation window before changing its budget')
            window = None if batches_per_model is None else (previous if reuse else {
                'id': uuid.uuid4().hex, 'chain': c['chain'], 'batches_per_model': batches_per_model,
                'slots': [1], 'existing_batches': [b['id'] for b in c['batches']]})
            ident = uuid.uuid4().hex
            record = {'id': ident, 'at': ec._now(), 'reason': reason,
                      'previous_dispatch': c['dispatch'], 'window': window, 'quota': c['quota']}
            receipts.write(d / 'dispatch-resumes' / (ident + '.json'), record)
            c['dispatch'] = {'state': 'running', 'id': ident, 'reason': reason, 'updated_at': ec._now()}
            if window:
                c['dispatch']['window'] = window
            ec.save(d, c)
    return {'campaign': cid, 'dispatch': c['dispatch'], 'quota': c['quota']}
