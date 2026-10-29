"""One explicitly authorized own-slot quality-rework turn while claims stay drained."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import uuid

from tools import agent_extract as ax, extract_campaign as ec, campaign_supervision as supervision
from tools.campaign_ownership_recovery import MESSAGE, _public_command, _ref, _time, valid as ownership_valid
from utils import process_receipts as receipts

VM_OPTIONS = {'WEBIDE_VM_OPTIONS', 'PYCHARM_VM_OPTIONS', 'JETBRAINSCLIENT_VM_OPTIONS', 'PHPSTORM_VM_OPTIONS',
              'GOLAND_VM_OPTIONS', 'APPCODE_VM_OPTIONS', 'DEVECOSTUDIO_VM_OPTIONS', 'IDEA_VM_OPTIONS',
              'CLION_VM_OPTIONS', 'WEBSTORM_VM_OPTIONS', 'DATASPELL_VM_OPTIONS', 'STUDIO_VM_OPTIONS',
              'GATEWAY_VM_OPTIONS', 'DATAGRIP_VM_OPTIONS', 'RIDER_VM_OPTIONS', 'JETBRAINS_CLIENT_VM_OPTIONS',
              'RUBYMINE_VM_OPTIONS'}


def _adoption(c, d, model, slot, session, native, events, terminal, batches):
    events, terminal = Path(events).resolve(strict=True), Path(terminal).resolve(strict=True)
    process_path = events.with_name('process.json')
    process, end = receipts.read(process_path), receipts.read(terminal)
    if (events.name != 'stdout.jsonl' or terminal != events.with_name('exit-status.json')
            or end.get('session') != session or end.get('exit_code') != 0
            or process.get('original_session') != session or process.get('model') != model or process.get('slot') != slot
            or process.get('pid') != native.get('pid')
            or Path(process.get('stdout', '/missing')).resolve() != events
            or Path(process.get('exit_receipt', '/missing')).resolve() != terminal
            or abs(_time(end['ended_at']) - _time(native['ended_at'])) > 2
            or abs(_time(process['started_at']) - _time(native['started_at'])) > 2):
        raise ValueError('exact same-session successful native invocation pair required')
    outcome = supervision.events(events)
    if outcome.get('session') != session or outcome.get('terminal') != 'turn.completed' or outcome.get('hard_stop') or outcome.get('approval_refused'):
        raise ValueError('adopted rework invocation has an unresolved refusal')
    pending, sessions, completed = {}, [], set()
    for line in events.read_text().splitlines():
        row = json.loads(line)
        if row.get('type') == 'thread.started':sessions.append(row.get('thread_id'))
        if row.get('type') in ('error', 'turn.failed'):raise ValueError('provider errors require separate recovery')
        item = row.get('item', {})
        if item.get('type') != 'command_execution':continue
        if row.get('type') == 'item.started':
            if not item.get('id') or not item.get('command') or item['id'] in pending or item['id'] in completed:
                raise ValueError('unique adopted command identity required')
            pending[item['id']] = item['command']
        elif row.get('type') == 'item.completed':
            if item.get('id') not in pending or pending.pop(item['id']) != item.get('command'):
                raise ValueError('matched adopted command terminal required')
            completed.add(item['id'])
            if type(item.get('exit_code')) is not int:
                raise ValueError('adopted command exit required')
            if item['exit_code'] and (_public_command(item['command'], output_root=d / 'audit')
                    or str(item.get('aggregated_output', '')).strip() == MESSAGE or item['exit_code'] in (75, 76)):
                raise ValueError('failed public operation cannot be resumed by rework')
    if sessions != [session] or pending:raise ValueError('one complete adopted session required')
    if not supervision.normal_recovery(outcome.get('recovery_kind')):
        # A clean owner may have reported another slot's refusal. It may only
        # resume its own work after that exact peer incident was explicitly
        # reconciled; never clear an actual command refusal in this transcript.
        linked = False
        if outcome.get('recovery_kind') == 'business_permission_denied':
            for peer in range(1, ec.parallelism(c) + 1):
                root = ec.supervisor_dir(d, model, peer)
                ref = receipts.read(root / 'ownership-continuation-active.json')
                if not ref or not ownership_valid(c, d, model, peer):continue
                proof = receipts.read(Path(ref['path']))
                e = proof['evidence']
                if e['source_owner_slot'] == slot and e['rejected_batch'] in batches:linked = True
        if not linked:raise ValueError('reported refusal needs its exact published ownership continuation')
    return {'events': _ref(events), 'exit': _ref(terminal), 'process': _ref(process_path)}


def run(cid, model, slot, *, batches, assignment, reason, adopt_events=None, adopt_exit=None,
        maintenance_review=None, retry_startup=False, retry_capacity=False, spawn=subprocess.Popen, preflight=None):
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None or not reason.strip() or bool(adopt_events) != bool(adopt_exit):
        raise ValueError('coordinator reason and paired optional adoption evidence required')
    if maintenance_review and adopt_events:
        raise ValueError('maintenance continuation cannot adopt another invocation')
    if retry_startup and (maintenance_review or adopt_events):
        raise ValueError('startup retry cannot adopt or waive another continuation')
    if retry_capacity and (retry_startup or maintenance_review or adopt_events):
        raise ValueError('capacity retry cannot combine other continuations')
    ec.tracking_guard()
    d = ec.campaign_dir(cid)
    c = ec.load(d)
    slot = ec.worker_slot(c, model, slot)
    root = ec.supervisor_dir(d, model, slot)
    # Match the original supervisor's identity exactly. Single-worker campaigns
    # never acquired a slot identity or a slot_sessions manifest binding.
    identity = {'campaign': cid, 'model': model, **({'slot': slot} if slot is not None else {})}
    with receipts.attempt(root, kind='quality_rework', identity=identity) as update:
        c = ec.load(d)
        if (c.get('dispatch') or {}).get('state') != 'draining':
            raise ValueError('rework requires claims to remain draining')
        selected = sorted(set(batches))
        if not selected or len(selected) != len(batches):raise ValueError('unique explicit rework batches required')
        for bid in selected:
            b = ec.find_batch(c, bid)
            if b['model'] != model or ec.batch_slot(c, b) != slot or b['status'] != 'submitted':
                raise ValueError('rework batches must be submitted and owned by this original model/slot')
        with receipts.idle(root / 'native'):
            native = receipts.read(root / 'native/latest.json')
            worker = receipts.read(root / 'worker.json')
            if slot is None:
                session = worker.get('session')
                bindings = [receipts.read(ec.supervisor_dir(d, name) / 'worker.json').get('session')
                            for name in c['models']]
            else:
                session = c.get('slot_sessions', {}).get(model, {}).get(str(slot))
                bindings = [sid for ss in c.get('slot_sessions', {}).values() for sid in ss.values()]
            startup = None
            capacity = None
            owner_worker, owner_native = worker, native
            if retry_startup:
                from tools.campaign_rework_startup import validate as startup_validate
                owner_worker, owner_native, startup = startup_validate(
                    c, root, model, slot, session, worker, native, selected, assignment)
            if retry_capacity:
                from tools.campaign_rework_capacity import validate as capacity_validate
                capacity = capacity_validate(c, root, model, slot, session, worker, native, selected)
            # The ownership protocol already bound this exact failed native,
            # copied refusal transcript, original SID and unchanged ownership.
            # It authorizes only subsequent own-slot work, never the denied
            # cross-slot review. A new native attempt consumes that proof.
            owner_continuation = slot is not None and native.get('exit_code') == 1 and ownership_valid(c, d, model, slot)
            if (not session or worker.get('session') != session or owner_native.get('state') != 'completed'
                    or not owner_native.get('ended_at') or owner_native.get('exit_code') != 0 and not (owner_continuation or capacity)
                    or bindings.count(session) != 1):
                raise ValueError('original clean idle native/session required; rework does not waive a failed exit')
            if adopt_events:
                if owner_continuation:
                    raise ValueError('ownership continuation cannot adopt a different invocation')
                adopted = _adoption(c, d, model, slot, session, native, adopt_events, adopt_exit, selected)
            else:
                path = Path(owner_worker.get('events', '/missing')).resolve()
                if not path.is_relative_to(root.resolve()):raise ValueError('own-slot event ownership required')
                outcome = supervision.events(path)
                from tools.campaign_rework_completion import valid as completion_valid
                quality_stop = None
                maintenance = None
                if maintenance_review:
                    from tools import campaign_rework_maintenance as maintenance_protocol
                    maintenance = maintenance_protocol.validate(c,d,model,slot,selected,maintenance_review)
                if outcome.get('recovery_kind') == 'quality_process_violation':
                    from tools.campaign_quality_stop import validate
                    quality_stop = validate(c, d, model, slot, session, path, selected, owner_native)
                expected_terminal = None if owner_continuation else 'turn.failed' if capacity else 'turn.completed'
                if (outcome.get('session') != session or outcome.get('terminal') != expected_terminal
                        or outcome.get('hard_stop') or outcome.get('approval_refused')
                        or not owner_continuation and not supervision.normal_recovery(outcome.get('recovery_kind'))
                        and quality_stop is None
                        and maintenance is None
                        and not completion_valid(c, d, model, slot, locked=True)):
                    raise ValueError('original clean rework events required')
                adopted = None
            for bid in selected:
                with receipts.idle(d / 'operations' / bid):pass
            # No token/model probe. This is the same service preflight used by
            # supervision, and does not consume a new evaluation/source budget.
            flight = (preflight or ax.runtime_preflight)(os.environ.get('KG_HUB_URL', ax.DEFAULT_URL), ax.api_token())
            if not flight.get('ok'):raise ValueError('service preflight failed')
            if startup:
                for ref in startup.values():
                    if _ref(ref['path']) != ref:
                        raise ValueError('startup failure evidence changed before retry')
            if capacity and capacity_validate(c, root, model, slot, session, worker, native, selected) != capacity:
                raise ValueError('capacity evidence changed during preflight')
            if maintenance_review:
                maintenance_protocol.restored(maintenance,flight)
                if maintenance_protocol.validate(c,d,model,slot,selected,maintenance_review)!=maintenance:
                    raise ValueError('maintenance evidence changed during preflight')
            if not adopt_events and quality_stop is not None:
                for ref in [quality_stop['events'], *quality_stop['local_evidence']]:
                    if _ref(ref['path']) != ref:
                        raise ValueError('quality stop evidence changed before native startup')
            job = root.resolve() / 'quality-reworks' / uuid.uuid4().hex
            job.mkdir(parents=True)
            (job / 'previous-worker.json').write_bytes((root / 'worker.json').read_bytes())
            (job / 'previous-native.json').write_bytes((root / 'native/latest.json').read_bytes())
            if capacity:
                frozen = {}
                proof_dir = job / 'capacity-evidence'
                proof_dir.mkdir()
                for index, (name, ref) in enumerate(capacity['evidence'].items()):
                    if _ref(ref['path']) != ref:
                        raise ValueError('capacity evidence changed before snapshot')
                    target = proof_dir / (str(index) + '-' + Path(ref['path']).name)
                    target.write_bytes(Path(ref['path']).read_bytes())
                    copied = _ref(target)
                    if copied['sha256'] != ref['sha256']:
                        raise ValueError('capacity snapshot differs from reviewed evidence')
                    frozen[name] = copied
                receipts.write(job / 'capacity-retry.json', {**capacity, 'evidence': frozen,
                    'failed_native': _ref(job / 'previous-native.json'),
                    'failed_worker': _ref(job / 'previous-worker.json')})
            if startup:
                # latest.json and worker.json will advance during this attempt;
                # retain verifiable references to their immutable saved copies.
                receipts.write(job / 'startup-retry.json', {**startup,
                    'failed_native': _ref(job / 'previous-native.json'),
                    'failed_worker': _ref(job / 'previous-worker.json')})
            if owner_continuation:
                receipts.write(job / 'ownership-continuation.json',
                               _ref(root / 'ownership-continuation-active.json'))
            if adopted:receipts.write(job / 'adoption.json', adopted)
            if not adopt_events and quality_stop is not None:
                receipts.write(job / 'quality-stop-continuation.json', quality_stop)
            if maintenance_review:
                receipts.write(job / 'maintenance-continuation.json', maintenance)
            assignment = Path(assignment).resolve(strict=True)
            text = assignment.read_text()
            (job / 'assignment.md').write_text(text)
            args = supervision.native_args(c, model, session, slot, quality_rework=True)
            args[-1] = (args[-1] + '\n当前是协调器明确批准的已提交批次质量返工，不是配额恢复。'
                'dispatch必须保持draining，禁止claim/submit/release/修改原结果历史，禁止处理名单外来源。'
                '只对以下原本属于本槽的已提交批次逐来源审核及公共修复；保存原错/原质量表，'
                '另写有效候选、逐条语义决定和质量凭据。成本仅写独立rework-cost.json，未知值保留null；'
                '禁止campaign cost（包括仅修改note）或overhead，不覆盖原批成本。'
                '本次返工以本段为准，不执行普通生产brief中的领批、提交或记成本命令。'
                '全部终态经协调器验收前不能宣布完成。'
                '\n冻结批次名单：' + json.dumps(selected) + '\n具体返工材料与要求：\n' + text)
            entry = Path(os.environ.get('KG_HUB_CODE', '~/.local/share/kg-hub/current')).expanduser() / 'tools/extract'
            args = [str(entry), 'runtime', 'exec', '--proxy-mode', ec.runtime_settings(c)['proxy_mode'],
                    '--receipt-dir', str(root / 'native'), '--', *args]
            env = dict(os.environ, KG_HUB_CAMPAIGN_ID=cid, KG_HUB_CAMPAIGN_MODEL=model,
                       KG_HUB_CAMPAIGN_SLOT=str(slot if slot is not None else 1),
                       KG_HUB_REWORK_BATCHES=json.dumps(selected))
            dependencies = ec.dependency_pythonpath()
            if dependencies:env['PYTHONPATH'] = dependencies
            removed = []
            for key in VM_OPTIONS:
                if any(0xD800 <= ord(ch) <= 0xDFFF for ch in env.get(key, '')):
                    env.pop(key);removed.append(key)
            out, err = job / 'events.jsonl', job / 'stderr.log'
            receipts.write(job / 'scope.json', {'campaign': cid, 'model': model, 'slot': slot, 'session': session,
                'batches': selected, 'reason': reason, 'assignment': _ref(job / 'assignment.md'),
                'assignment_origin_path': str(assignment), 'preflight': flight,
                'new_source_quota': 0, 'removed_invalid_utf8_jetbrains_keys': sorted(removed)})
        # Release the observed native idle lock before its runtime acquires it;
        # the held supervisor lease prevents a second managed call of this slot.
        saved = {'session': session, 'events': str(out), 'stderr': str(err), 'mode': 'quality_rework', 'batches': selected}
        if capacity:
            saved['capacity_retries'] = capacity['retry']
        receipts.write(root / 'worker.json', saved)
        with out.open('xb') as stdout, err.open('xb') as stderr:
            child = spawn(args, cwd=ec.workspace_dir(), stdin=subprocess.DEVNULL, stdout=stdout,
                          stderr=stderr, env=env, start_new_session=True)
            update(phase='quality_rework_running', session=session, events=str(out), batches=selected)
            code = child.wait()  # Do not time out, signal or replay a NAS request.
        receipts.write(job / 'exit-status.json', {'exit_code': code, 'session': session, 'ended_at': receipts.now()})
        latest = receipts.status(root / 'native')
        outcome = supervision.events(out)
        clean = (code == 0 and latest.get('attempt') != native.get('attempt') and latest.get('exit_code') == 0
                 and latest.get('state') == 'completed' and latest.get('lease_held') is False
                 and outcome.get('session') == session and outcome.get('terminal') == 'turn.completed'
                 and not outcome.get('hard_stop') and not outcome.get('approval_refused')
                 and supervision.normal_recovery(outcome.get('recovery_kind')))
        update(phase='quality_rework_turn_completed' if clean else 'coordinator_required', exit_code=0 if clean else 2)
        return {'model': model, 'slot': slot, 'session': session, 'state': 'rework_turn_completed' if clean else 'blocked',
                'batch_semantic_acceptance': 'pending_coordinator_verification', 'new_source_quota': 0,
                'dispatch': ec.load(d).get('dispatch'), 'evidence_dir': str(job), 'native_exit_code': code}
