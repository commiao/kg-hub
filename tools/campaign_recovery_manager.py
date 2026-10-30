"""Persistent, bounded recovery of existing owners; never clears a refusal.

The coordinator registers rework and approves a particular drained generation.
The manager only executes those public contracts. Semantic acceptance remains a
separate decision, not a consequence of a clean process exit or a moving counter.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import time

from tools import extract_campaign as ec, campaign_supervision as supervision, agent_extract as ax
from utils import process_receipts as receipts
from utils.extraction_quality import digest

VERSION = 'campaign-recovery-v1'


def ref(path):
    path = Path(path).resolve(strict=True)
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def checked(value):
    if not isinstance(value, dict) or ref(value['path']) != value:
        raise ValueError('registered recovery evidence changed')
    return Path(value['path'])


def coordinator():
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT') is not None:
        raise ValueError('recovery management requires the coordinator')
    ec.tracking_guard()


def config(d):
    return receipts.read(d / 'recovery/config.json')


def enable(cid, *, reason, max_stalled=3):
    coordinator()
    if not reason.strip() or type(max_stalled) is not int or max_stalled < 1:
        raise ValueError('reason and positive stalled-turn budget required')
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d)
        if c['executor'] != 'codex' or ec.parallelism(c) < 2:
            raise ValueError('recovery manager requires an existing Codex slot campaign')
        previous = config(d)
        value = {'version': VERSION, 'campaign': cid, 'enabled': True,
                 'reason': reason, 'max_stalled': max_stalled,
                 'contract': {'executor': c['executor'], 'quota': c['quota'],
                              'models': c['models'], 'parallel': ec.parallelism(c),
                              'prompt_sha256': c['prompt_sha256'], 'runtime': ec.runtime_settings(c)}}
        # Changes are explicit and preserved; no historical batch is relabelled.
        receipts.write(d / 'recovery/config.json', value)
        receipts.write(d / 'recovery/config-history' / (digest(value) + '.json'),
                       {'at': receipts.now(), 'previous': previous, 'current': value})
    return value


def verify_contract(c, policy):
    actual = {'executor': c['executor'], 'quota': c['quota'], 'models': c['models'],
              'parallel': ec.parallelism(c), 'prompt_sha256': c['prompt_sha256'], 'runtime': ec.runtime_settings(c)}
    if policy.get('version') != VERSION or policy.get('campaign') != c['id'] or policy.get('contract') != actual:
        raise ValueError('registered recovery campaign contract changed')


def disable(cid, *, reason):
    coordinator()
    if not reason.strip(): raise ValueError('coordinator reason required')
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        policy = config(d); verify_contract(ec.load(d), policy)
        policy.update(enabled=False, disabled_reason=reason)
        receipts.write(d / 'recovery/config.json', policy)
    return {'state': 'disabled', 'in_flight_policy': 'leave original owners intact'}


def service_plist(cid, output):
    """Generate only; fleet-ops launchd_reload installs the reviewed service.

    StartInterval re-enters durable state after login/reboot and observer exits.
    It never kills a native or uses KeepAlive to replay a blocked operation.
    """
    coordinator()
    d = ec.campaign_dir(cid); verify_contract(ec.load(d), config(d))
    entry = Path('~/.local/share/kg-hub/current/tools/extract').expanduser()
    native = shutil.which('codex')
    if not native: raise ValueError('registered native CLI unavailable in the service preparation environment')
    env = {'HOME': str(Path.home()),
           'PATH': str(Path(native).parent) + ':/opt/homebrew/bin:/usr/local/bin:' + str(Path.home() / '.local/bin') + ':/usr/bin:/bin:/usr/sbin:/sbin',
           'KG_HUB_CAMPAIGN_WORKSPACE': str(ec.workspace_dir())}
    for key in ('KG_HUB_AGENT_EXTRACT_DIR', 'KG_HUB_CLAIMS_DIR', 'KG_HUB_NAS_GATE_DIR'):
        if os.environ.get(key): env[key] = os.environ[key]
    value = {'Label': 'com.kg-hub.campaign-recovery.' + cid,
             'ProgramArguments': [str(entry), 'campaign', 'recovery-watch', '--id', cid, '--once'],
             'WorkingDirectory': str(ec.workspace_dir()), 'EnvironmentVariables': env,
             'RunAtLoad': True, 'StartInterval': 45, 'ThrottleInterval': 10,
             'ProcessType': 'Background', 'LowPriorityIO': True,
             'StandardOutPath': str(d / 'recovery/service.stdout.log'),
             'StandardErrorPath': str(d / 'recovery/service.stderr.log')}
    output = Path(output).expanduser()
    if output.exists() and output.read_bytes() != plistlib.dumps(value):
        raise ValueError('existing recovery service differs; review and retain it before replacement')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(plistlib.dumps(value)); output.chmod(0o600)
    return {'plist': ref(output), 'label': value['Label'], 'installed': False}


def register_rework(cid, model, slot, *, batches, assignment, checkpoint, reason):
    coordinator()
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d); verify_contract(c, config(d))
        slot = ec.worker_slot(c, model, slot)
        session = c.get('slot_sessions', {}).get(model, {}).get(str(slot))
        if not session or not reason.strip() or len(set(batches)) != len(batches) or not batches:
            raise ValueError('original bound session, explicit batches and reason required')
        ids, inputs, contracts, rules = [], [], [], {}
        for bid in batches:
            b = ec.find_batch(c, bid)
            if b['model'] != model or ec.batch_slot(c, b) != slot or b['status'] != 'submitted':
                raise ValueError('registered rework must use original own submitted batches')
            ids.extend(b['ids'])
            contracts.append({k: b.get(k) for k in ('id', 'model', 'slot', 'chain', 'ids', 'status')})
            inputs.append(ref(d / 'batches' / (bid + '.json')))
            for path in (d / 'results' / (bid + '.json'), ax.quality_file(d, bid)):
                if path.exists(): inputs.append(ref(path))
            rules[bid] = digest(ec.batch_rules_text(d, c, b))
        checkpoint = Path(checkpoint).resolve()
        if not checkpoint.is_relative_to((d / 'audit').resolve()):
            raise ValueError('checkpoint must stay inside this campaign audit directory')
        assignment = ref(assignment)
        value = {'version': VERSION, 'model': model, 'slot': slot,
                 'session': session, 'batches': batches, 'source_ids': sorted(ids),
                 'assignment': assignment, 'checkpoint': str(checkpoint), 'reason': reason,
                 'inputs': inputs, 'batch_contracts': contracts, 'rules': rules}
        key = digest(value); value['id'] = key
        history = d / 'recovery/rework-registrations' / (key + '.json')
        if not history.exists(): receipts.write(history, value)
        receipts.write(d / 'recovery/reworks' / (f'{ec.slug(model)}-{slot}.json'), value)
    return value


def checkpoint_progress(registration):
    value = receipts.read(Path(registration['checkpoint']))
    reviewed, pending = value.get('reviewed'), value.get('pending')
    if (not isinstance(reviewed, list) or not isinstance(pending, list)
            or any(type(x) is not int for x in reviewed + pending)
            or len(set(reviewed + pending)) != len(reviewed + pending)
            or sorted(reviewed + pending) != registration['source_ids']):
        raise ValueError('complete disjoint source-bound rework checkpoint required')
    return {'reviewed': len(reviewed), 'pending': len(pending),
            'repairs_verified': len(set(value.get('repairs_verified', []))),
            'in_flight': value.get('in_flight'), 'checkpoint_sha256': digest(value)}


def classify(outcome, native, worker=None, policy=None):
    """Every stop has an explicit disposition.

    Class B (approval, NAS 75, quality, business permission, uncertain write) keeps
    its human-only disposition verbatim. Class A (capacity, NAS gate timeout 76,
    transport/service failures) and class C (unknown terminal) continue the same
    session under the campaign's bounded backoff; once the persisted worker state
    shows that budget spent, the slot becomes coordinator_required with the raw
    terminal attached by `snapshot` instead of an opaque label.
    """
    if native.get('lease_held'):
        return 'wait_original', 'slot'
    if outcome.get('approval_refused') or outcome.get('recovery_kind') == 'approval_rejected':
        return 'approval_required', 'campaign'
    if outcome.get('hard_stop') == 75:
        return 'nas_reconciliation_required', 'campaign'
    if outcome.get('recovery_kind') == 'quality_process_violation':
        return 'registered_rework_required', 'campaign'
    if outcome.get('recovery_kind') in ('business_permission_denied', 'uncertain_submit'):
        return 'engineering_reconciliation_required', 'campaign'
    if outcome.get('hard_stop') == 76:
        policy = policy or supervision.recovery_policy(None)
        if supervision.backoff_exhausted(worker, policy, 'infrastructure'):
            return 'coordinator_required', 'campaign'
        return 'bounded_infrastructure_continuation', 'slot'
    if outcome.get('hard_stop') or outcome.get('recovery_kind') == 'nas_refused':
        return 'nas_reconciliation_required', 'campaign'
    if supervision.capacity_terminal(outcome, native):
        return 'bounded_capacity_continuation', 'slot'
    if (native.get('state') == 'completed' and native.get('exit_code') == 0
            and native.get('ended_at') and outcome.get('terminal') == 'turn.completed'
            and supervision.normal_recovery(outcome.get('recovery_kind'))):
        return 'same_session_continuation', 'slot'
    verdict = supervision.classify_terminal(outcome, native)
    if verdict['class'] == 'business':
        return 'engineering_reconciliation_required', 'campaign'
    policy = policy or supervision.recovery_policy(None)
    if supervision.backoff_exhausted(worker, policy, verdict['class']):
        return 'coordinator_required', 'campaign'
    return ('bounded_infrastructure_continuation' if verdict['class'] == 'infrastructure'
            else 'bounded_unknown_continuation'), 'slot'


def isolated_slot_failure(result):
    # A real approval, NAS, unknown-write or quality incident remains systemic.
    return result.get('reason') in ('model_capacity_exhausted', 'no_formal_progress')


def conditions(c, d, model, slot, registration, progress, *, formal_counts=None):
    """Eligibility excludes native attempt IDs and peers' formal counters.

    A new failed invocation is not a changed prerequisite. Persisted blocked
    results survive observer restarts and policy disable/enable cycles.
    """
    pending = [b for b in c['batches'] if b['model']==model and ec.batch_slot(c,b)==slot
               and b['status'] not in ('submitted','released')]
    files=[]
    for b in pending:
        for p in [d/'results'/(b['id']+'.json'), ax.quality_file(d,b['id'])]:
            if p.exists():files.append(ref(p))
        files.extend(ref(p) for p in sorted((d/'source-decisions'/b['id']).glob('*/latest.json')))
    return digest({'session':c.get('slot_sessions',{}).get(model,{}).get(str(slot)),
        'own_formal':(supervision.completed(c,d,model,slot) if formal_counts is None
                      else formal_counts.get((model,slot),0)),
        'pending':[{k:b.get(k) for k in ('id','model','slot','ids','chain','status','decision_protocol')} for b in pending],
        'inputs':files,
        'registration':registration.get('id'),'progress':progress,
        'manager_code':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})


def unchanged_block(d, row):
    for path in (d/'recovery/events').glob('*/result.json'):
        result=receipts.read(path)
        if (result.get('state')=='blocked' and result.get('model')==row['model']
                and result.get('slot')==row['slot']
                and result.get('conditions_sha256')==row['conditions_sha256']):
            return {'event':path.parent.name,'reason':result.get('reason'),
                    **({'detail': result['detail']} if result.get('detail') else {}),
                    **({'message': result['message']} if result.get('message') else {})}
    return None


def snapshot(cid):
    d = ec.campaign_dir(cid); c = ec.load(d); policy = config(d)
    if policy.get('enabled') is not True:
        raise ValueError('explicit recovery-enable registration required')
    verify_contract(c, policy)
    try:
        policy_values = supervision.recovery_policy(c)
    except ValueError:
        policy_values = supervision.recovery_policy(None)
    rows = []
    formal_counts = supervision.completed_counts(c, d)
    for model in c['models']:
        for slot in range(1, ec.parallelism(c) + 1):
            root = ec.supervisor_dir(d, model, slot)
            worker = receipts.read(root / 'worker.json')
            native, supervisor = receipts.status(root / 'native'), receipts.status(root)
            session = c.get('slot_sessions', {}).get(model, {}).get(str(slot))
            outcome = supervision.events(Path(worker['events'])) if worker.get('events') else {}
            disposition, scope = classify(outcome, native, worker, policy_values)
            continuation = False
            if not native.get('lease_held') and not supervisor.get('lease_held') and session and outcome.get('session') == session:
                from tools.campaign_continuation import worker_reconciled
                continuation = worker_reconciled(c, d, model, slot)
                if continuation:
                    disposition = 'published_continuation'
            registration = receipts.read(d / 'recovery/reworks' / (f'{ec.slug(model)}-{slot}.json'))
            progress = None
            if registration:
                if registration['session'] != session:
                    raise ValueError('registered rework owner changed')
                checked(registration['assignment'])
                for value in registration['inputs']: checked(value)
                for original in registration['batch_contracts']:
                    b = ec.find_batch(c, original['id'])
                    if {k: b.get(k) for k in original} != original or digest(ec.batch_rules_text(d, c, b)) != registration['rules'][b['id']]:
                        raise ValueError('registered original batch or frozen rules changed')
                progress = checkpoint_progress(registration)
            pool = receipts.status(ec.supervisor_dir(d, model))
            if native.get('lease_held') or supervisor.get('lease_held') or pool.get('lease_held'):
                action = 'wait_original'
            elif not session or worker.get('session') not in (None, session) or outcome.get('session') != session:
                action = 'owner_reconciliation_required'
            elif registration and progress['pending'] and not progress['in_flight']:
                # Public rework repeats all exact original terminal validations.
                # A refusal is never converted into a production continuation.
                action = 'rework' if disposition in ('same_session_continuation', 'registered_rework_required', 'published_continuation') else disposition
            elif registration and not progress['pending']:
                action = 'coordinator_semantic_acceptance_required'
            elif (c.get('dispatch') or {}).get('state') == 'draining':
                action = 'dispatch_draining'
            elif disposition in ('same_session_continuation', 'bounded_capacity_continuation', 'published_continuation',
                                 'bounded_infrastructure_continuation', 'bounded_unknown_continuation'):
                action = 'supervise'
            else:
                action = disposition
            own = [b for b in c['batches'] if b['model'] == model and ec.batch_slot(c, b) == slot]
            for b in own:
                op = receipts.status(d / 'operations' / b['id'])
                if op.get('lease_held'):
                    action = 'wait_original'; break
                if b['status'] not in ('submitted', 'released'):
                    from tools.campaign_continuation import valid_partial
                    partial=valid_partial(c,d,b,op)
                    if not partial and (op.get('may_have_written') or op.get('state') == 'uncertain' or op.get('requires_reconciliation')):
                        action = 'unknown_write_reconciliation_required'; scope = 'campaign'
                        batch=receipts.read(d/'batches'/(b['id']+'.json'))
                        keys=[{k:i.get(k) for k in ('source_description','source_obs_id')} for i in batch.get('items',[])]
                        if (op.get('identity')=={'campaign':cid,'batch':b['id'],**ec.batch_identity(c,b)}
                                and op.get('source_keys')==keys and keys):scope='slot'
                        break
                    if op.get('exit_code') == 75 or (op.get('exit_code') == 76 and not supervision.gate_timeout_before_write(op)
                                                     and op.get('state') != 'reconciled_not_written'):
                        action = 'nas_reconciliation_required'; scope = 'campaign'; break
            pending=[b for b in own if b['status'] not in ('submitted','released')]
            if action=='engineering_reconciliation_required' and len(pending)==1 and outcome.get('recovery_kind')=='infrastructure_failed':
                op=receipts.status(d/'operations'/pending[0]['id']);statuses=op.get('by_status',{})
                if (op.get('kind')=='dryrun' and op.get('state')=='completed' and op.get('exit_code')==0
                        and op.get('may_have_written') is False and statuses.get('http_502')
                        and not set(statuses)-{'dry_run','empty','http_502'}):
                    action='reconcile_preview';scope='slot'
            if formal_counts.get((model, None), 0) >= c['quota'] and action == 'supervise':
                action = 'quota_complete'
            handoff = None
            if worker.get('mode') == 'quality_rework' and action == 'supervise':
                from tools.campaign_rework_handoff import valid
                handoff = valid(c, d, model, slot)
                if not handoff:
                    action = 'coordinator_rework_handoff_required'; scope = 'slot'
            row = {'model': model, 'slot': slot, 'session': session, 'action': action,
                   'scope': scope, 'native_attempt': native.get('attempt'),
                   'worker': worker, 'outcome': outcome, 'progress': progress,
                   'registration': registration, 'dispatch_id': (c.get('dispatch') or {}).get('id')}
            row['published_continuation_valid'] = continuation
            row['conditions_sha256'] = conditions(c,d,model,slot,registration,progress,formal_counts=formal_counts)
            if handoff:
                row['accepted_rework_handoff'] = handoff
                row['conditions_sha256'] = digest({'conditions': row['conditions_sha256'], 'handoff': handoff})
            if action in ('coordinator_required', 'engineering_reconciliation_required'):
                # The raw terminal, not just a label: stderr tail, events terminal,
                # exit code and the spent retry budget.
                row['blocked'] = {'reason': action, **supervision.blocked_detail(worker, outcome, native)}
            if action in ('supervise','rework','reconcile_preview'):
                blocked=unchanged_block(d,row)
                if blocked:
                    row.update(action='unchanged_blocked',scope='slot',blocked=blocked)
            row['event_id'] = digest(row)
            rows.append(row)
    return {'version': VERSION, 'campaign': cid, 'observed_at': receipts.now(), 'dispatch': c.get('dispatch'),
            'slots': rows, 'formal': {m: formal_counts.get((m,None),0) for m in c['models']}}


def approve_resume(cid, *, evidence, reason):
    """Coordinator acceptance, not a model verdict or automatic quality waiver."""
    coordinator()
    d = ec.campaign_dir(cid)
    with ec.locked(d):
        c = ec.load(d); verify_contract(c, config(d))
        if (c.get('dispatch') or {}).get('state') != 'draining' or not reason.strip() or not evidence:
            raise ValueError('drained generation, evidence and coordinator reason required')
        refs = [ref(p) for p in evidence]
        if any(not Path(r['path']).is_relative_to((d / 'audit').resolve()) for r in refs):
            raise ValueError('acceptance evidence must stay inside campaign audit')
        request = {'campaign': cid, 'dispatch_id': c['dispatch']['id'], 'reason': reason, 'evidence': refs}
        receipts.write(d / 'recovery/resume-approvals' / (digest(request) + '.json'), request)
        receipts.write(d / 'recovery/resume-request.json', request)
    return request


def step(cid, event_id):
    coordinator()
    d = ec.campaign_dir(cid); path = d / 'recovery/events' / event_id
    if not event_id or any(x not in '0123456789abcdef' for x in event_id) or len(event_id) != 64:
        raise ValueError('exact recovery event digest required')
    request = receipts.read(path / 'request.json')
    if request.get('event_id') != event_id:
        raise ValueError('recovery event identity differs')
    with receipts.attempt(path, kind='recovery_action', allow_abandoned=True) as update:
        if (path / 'result.json').exists():
            return receipts.read(path / 'result.json')
        current = next((r for r in snapshot(cid)['slots'] if r['event_id'] == event_id), None)
        if current != request:
            result = {'state': 'stale', 'action': 'reobserve_original_owner'}
        else:
            history = [receipts.read(p) for p in (d / 'recovery/events').glob('*/result.json')]
            epoch = (current.get('registration') or {}).get('id') or current['dispatch_id']
            same = [r for r in history if r.get('model') == current['model'] and r.get('slot') == current['slot']
                    and r.get('progress_epoch') == epoch]
            before = current.get('progress') or {'formal': supervision.completed(ec.load(d),d,current['model'],current['slot'])}
            token = {k: v for k, v in before.items() if k in ('reviewed', 'repairs_verified', 'formal')}
            stalled = sum(r.get('progress_token') == token and r.get('action') == current['action'] for r in same)
            if stalled >= config(d)['max_stalled']:
                result = {'state': 'blocked', 'reason': 'recovery_no_progress_budget_exhausted'}
            else:
                try:
                    if current['action'] == 'rework':
                        from tools.campaign_rework import run
                        reg = current['registration']; checked(reg['assignment'])
                        result = run(cid, current['model'], current['slot'], batches=reg['batches'],
                                     assignment=reg['assignment']['path'], reason=reg['reason'])
                    elif current['action'] == 'supervise':
                        result = supervision.run(cid, current['model'], slot=current['slot'])
                    elif current['action']=='reconcile_preview':
                        from tools.campaign_local_recovery import reconcile
                        result={'state':'reconciled',**reconcile(cid,current['model'],current['slot'],
                            reason='bounded source-scoped read-only preview recovery',url=ax.DEFAULT_URL,token=ax.api_token())}
                    else:
                        result = {'state': 'blocked', 'reason': 'recovery_action_no_longer_executable'}
                except (Exception, SystemExit) as exc:
                    # A rejected exact event is terminal. Never spin every
                    # observer tick retrying a real public refusal.
                    result = {'state': 'blocked', 'reason': 'public_recovery_refused',
                              'error_type': type(exc).__name__, 'message': str(exc)}
            result = {**result, 'model': current['model'], 'slot': current['slot'],
                      'action': current['action'], 'progress_token': token, 'progress_epoch': epoch,
                      'conditions_sha256':current['conditions_sha256']}
        receipts.write(path / 'result.json', result)
        update(phase='action_completed', exit_code=0 if result.get('state') != 'blocked' else 2)
        return result


def _retire_accepted(d, plan, request):
    for row in plan['slots']:
        if row.get('progress') and not row['progress']['pending']:
            p = d / 'recovery/reworks' / (f"{ec.slug(row['model'])}-{row['slot']}.json")
            if p.exists(): p.rename(p.with_suffix('.accepted-' + request['dispatch_id'] + '.json'))


def tick(cid, *, spawn=None):
    coordinator()
    spawn = spawn or subprocess.Popen
    d = ec.campaign_dir(cid)
    with receipts.attempt(d / 'recovery/observer', kind='recovery_observer', allow_abandoned=True) as update:
        if config(d).get('enabled') is False:
            update(phase='disabled', exit_code=0)
            return {'state': 'disabled', 'campaign': cid}
        plan = snapshot(cid)
        shared = {'approval_required', 'nas_reconciliation_required', 'registered_rework_required',
                  'unknown_write_reconciliation_required', 'engineering_reconciliation_required',
                  'coordinator_required'}
        incidents = [r for r in plan['slots'] if r['action'] in shared and r['scope']=='campaign']
        for row in incidents:
            path = d / 'recovery/incidents' / (row['event_id'] + '.json')
            if not path.exists(): receipts.write(path, row)
        if plan['dispatch'].get('state') == 'running' and incidents:
            from tools.campaign_migration import drain
            stopped = drain(cid, 'recovery manager shared stop: ' + ', '.join(
                f"{r['model']}/{r['slot']}:{r['action']}:{r['event_id']}" for r in incidents))
            plan = {**snapshot(cid), 'shared_stop': stopped}
        request = receipts.read(d / 'recovery/resume-request.json')
        if request and plan['dispatch'].get('state') == 'running':
            resumed = receipts.read(d / 'dispatch-resumes' / (plan['dispatch']['id'] + '.json'))
            if resumed.get('previous_dispatch', {}).get('id') == request['dispatch_id']:
                # Observer may die after public resume but before retiring
                # accepted registrations. Re-enter that exact transaction.
                for value in request['evidence']: checked(value)
                _retire_accepted(d, plan, request)
                plan = snapshot(cid)
        if request and plan['dispatch'].get('state') == 'draining' and request['dispatch_id'] == plan['dispatch'].get('id'):
            for value in request['evidence']: checked(value)
            # A live owner is waited for. Public resume checks all native and
            # operation terminals again under their actual idle locks.
            if any(r.get('progress') and (r['progress']['pending'] or r['progress']['in_flight']) for r in plan['slots']):
                plan['resume'] = {'state': 'blocked', 'reason': 'registered_rework_incomplete'}
            elif not any(r['action'] == 'wait_original' for r in plan['slots']):
                from tools.campaign_dispatch import resume
                try:
                    outcome = resume(cid, reason=request['reason'])
                except (ValueError, receipts.Busy, receipts.Uncertain) as exc:
                    plan['resume'] = {'state': 'blocked', 'reason': str(exc)}
                else:
                    plan['resume'] = outcome
                    # Retire completed registrations only after the coordinator
                    # explicitly accepted this exact drained generation.
                    _retire_accepted(d, plan, request)
                    plan = {**snapshot(cid), 'resume': outcome}
        for row in plan['slots']:
            if row['action'] not in ('rework', 'supervise','reconcile_preview'): continue
            directory = d / 'recovery/events' / row['event_id']
            if (directory / 'result.json').exists() or receipts.status(directory).get('lease_held'):
                continue
            directory.mkdir(parents=True, exist_ok=True)
            if not (directory / 'request.json').exists(): receipts.write(directory / 'request.json', row)
            entry = Path(os.environ.get('KG_HUB_CODE', '~/.local/share/kg-hub/current')).expanduser() / 'tools/extract'
            # A repeated launch after an observer crash uses the SAME event
            # lease; only one step can reach the existing native owner guard.
            with (directory / 'stdout.json').open('ab') as out, (directory / 'stderr.log').open('ab') as err:
                env = dict(os.environ)
                # A child enters the public bundle selector itself. Do not
                # carry a parent delegate marker across an atomic publication.
                env.pop('LLM_EVAL_DELEGATED_BACKEND', None)
                child = spawn([str(entry), 'campaign', 'recovery-step', '--id', cid,
                               '--event', row['event_id']], cwd=ec.workspace_dir(),
                              stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, env=env)
                receipts.write(directory / 'launch.json', {'pid': child.pid, 'at': receipts.now()})
        receipts.write(d / 'recovery/latest.json', plan)
        update(phase='observed', exit_code=0)
        return plan


def watch(cid, *, once=False, interval=45, sleep=time.sleep):
    if interval < 1 or interval > 60:
        raise ValueError('recovery observer interval must be 1..60 seconds')
    while True:
        result = tick(cid)
        if once: return result
        sleep(interval)
