"""Coordinator-attested handoff of an accepted own-source rework to production.

This never clears a stop, changes source results, or certifies semantics itself.
It supplies a new, exact prerequisite for the original idle owner only.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from tools import extract_campaign as ec, campaign_supervision as supervision
from utils import process_receipts as receipts
from utils.extraction_quality import digest

VERSION = 'accepted-rework-handoff-v1'


def path(d, model, slot):
    return d / 'recovery/handoffs' / f'{ec.slug(model)}-{slot}.json'


def prerequisites(c, d, model, slot, *, locked=False):
    from tools import campaign_recovery_manager as manager
    root = ec.supervisor_dir(d, model, slot)
    worker = receipts.read(root / 'worker.json')
    def status(directory):
        # The caller acquired these exact idle leases. Probing our own exclusive
        # lock would report a live original writer and corrupt the binding.
        if locked:
            value = receipts.read(directory / 'latest.json')
            return {**value, 'state': value.get('state', 'not_started'), 'lease_held': False}
        return receipts.status(directory)
    native = status(root / 'native')
    event = Path(worker.get('events', '/missing')).resolve()
    session = c.get('slot_sessions', {}).get(model, {}).get(str(slot))
    outcome = supervision.events(event) if event.is_file() else {}
    batches = worker.get('batches', [])
    if (worker.get('mode') != 'quality_rework' or not batches
            or not event.is_relative_to((root / 'quality-reworks').resolve())
            or not session or worker.get('session') != session or outcome.get('session') != session
            or sum(session == sid for bindings in c.get('slot_sessions', {}).values()
                   for sid in bindings.values()) != 1
            or native.get('state') != 'completed' or native.get('exit_code') != 0
            or not native.get('ended_at') or native.get('lease_held')
            or outcome.get('terminal') != 'turn.completed'
            or outcome.get('hard_stop') or outcome.get('approval_refused')
            or not supervision.normal_recovery(outcome.get('recovery_kind'))):
        raise ValueError('exact clean original rework terminal required')
    contracts = []
    for bid in batches:
        b = ec.find_batch(c, bid)
        if b['model'] != model or ec.batch_slot(c, b) != slot or b['status'] != 'submitted':
            raise ValueError('handoff requires original own submitted rework batches')
        contracts.append({k: b.get(k) for k in ('id', 'model', 'slot', 'ids', 'chain', 'status')})
    pending = []
    for b in c['batches']:
        if b['model'] != model or ec.batch_slot(c, b) != slot or b['status'] in ('submitted', 'released'):
            continue
        op = status(d / 'operations' / b['id'])
        if (op.get('lease_held') or op.get('may_have_written') or op.get('requires_reconciliation')
                or op.get('state') not in ('not_started', 'completed', 'reconciled_not_written')
                or op.get('exit_code', 0) != 0):
            raise ValueError('pending original operation is not safely idle')
        pending.append({'batch': {k: b.get(k) for k in ('id', 'model', 'slot', 'ids', 'chain', 'status')},
                        'operation': op,
                        'frozen_batch': manager.ref(d / 'batches' / (b['id'] + '.json'))})
    if not pending or (c.get('dispatch') or {}).get('state') != 'running':
        raise ValueError('running dispatch with original pending production batch required')
    return {'campaign': c['id'], 'model': model, 'slot': slot, 'session': session,
            'dispatch': c['dispatch']['id'], 'contract': manager.config(d)['contract'],
            'worker': manager.ref(root / 'worker.json'), 'events': manager.ref(event),
            'native': native, 'rework_batches': contracts, 'pending': pending}


def acceptance_refs(d, value, evidence):
    from tools import campaign_recovery_manager as manager
    if len(evidence) != len(value['rework_batches']):
        raise ValueError('one explicit complete acceptance per original rework batch required')
    refs = []
    for batch, file in zip(value['rework_batches'], evidence):
        file = Path(file).resolve(strict=True)
        if not file.is_relative_to((d / 'audit').resolve()):
            raise ValueError('acceptance must stay inside campaign audit')
        doc = receipts.read(file)
        if (doc.get('batch') != batch['id'] or doc.get('current_quality_rework_scope_accepted') is not True
                or doc.get('sources') != len(batch['ids'])
                or set(doc.get('source_scopes', {})) != {str(i) for i in batch['ids']}):
            raise ValueError('explicit full original source-scope acceptance required')
        refs.append(manager.ref(file))
        for item in doc['source_scopes'].values():
            source = Path(item).resolve(strict=True)
            if not source.is_relative_to((d / 'audit').resolve()):
                raise ValueError('source acceptance outside campaign audit')
            refs.append(manager.ref(source))
    return refs


def approve(cid, model, slot, *, evidence, reason):
    from tools import campaign_recovery_manager as manager
    manager.coordinator()
    if not reason.strip():
        raise ValueError('explicit coordinator handoff reason required')
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d)
        manager.verify_contract(c, manager.config(d))
        slot = ec.worker_slot(c, model, slot)
        root = ec.supervisor_dir(d, model, slot)
        with contextlib.ExitStack() as locks:
            for p in [root, root / 'native', ec.supervisor_dir(d, model)]:
                locks.enter_context(receipts.idle(p))
            for b in c['batches']:
                if b['model'] == model and ec.batch_slot(c, b) == slot:
                    locks.enter_context(receipts.idle(d / 'operations' / b['id']))
            value = prerequisites(c, d, model, slot, locked=True)
            refs = acceptance_refs(d, value, evidence)
            receipt = {'version': VERSION, 'binding': value, 'acceptance': refs, 'reason': reason,
                       'semantic_verdict': 'explicit_coordinator_attestation', 'new_source_quota': 0}
            receipt['id'] = digest(receipt)
            previous = receipts.read(path(d, model, slot))
            if previous:
                if previous != receipt:
                    raise ValueError('existing exact handoff differs; do not reset its budget')
                return receipt
            receipts.write(d / 'recovery/handoff-history' / (receipt['id'] + '.json'), receipt)
            receipts.write(path(d, model, slot), receipt)
    return receipt


def valid(c, d, model, slot):
    from tools import campaign_recovery_manager as manager
    value = receipts.read(path(d, model, slot))
    if not value:
        return None
    try:
        payload = {k: v for k, v in value.items() if k != 'id'}
        if (value.get('version') != VERSION or value.get('id') != digest(payload)
                or value.get('binding') != prerequisites(c, d, model, slot)
                or not value.get('acceptance')):
            return None
        for item in value['acceptance']:
            manager.checked(item)
    except (ValueError, OSError, KeyError, TypeError):
        return None
    return value['id']
