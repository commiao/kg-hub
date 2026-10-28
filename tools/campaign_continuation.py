"""Explicit, evidence-bound continuation of body-upload failure and capacity exits.

Retains original failures and acknowledgements. Never retries a response timeout,
rewrites a frozen batch, or treats graph presence as a success receipt.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sysconfig
import uuid

from tools import agent_extract as ax, extract_campaign as ec
from tools.campaign_recovery import command_scope, file_sha
from utils import process_receipts as receipts
from utils.extraction_quality import digest


def _sealed(value):
    return {**value, 'sha256': digest(value)}


def _read(path):
    value = receipts.read(path)
    if value.get('sha256') != digest({k: v for k, v in value.items() if k != 'sha256'}):
        raise ValueError('continuation proof integrity differs')
    return value


def _write(path, value):
    if path.exists():
        raise ValueError('continuation evidence is immutable')
    receipts.write(path, _sealed(value))


def _inputs(c, d, b):
    bid = b['id']
    batch = receipts.read(d / 'batches' / (bid + '.json'))
    results = ec._results(d, bid)
    prompt = ec.batch_rules_text(d, c, b)
    review = receipts.read(ax.quality_file(d, bid))
    identity = ec.batch_identity(c, b)
    sha = ec.preview_input_digest(batch, results, prompt, review, identity)
    return batch, results, prompt, identity, sha


def _worker(c, d, model, slot, *, locked=False):
    from tools.campaign_supervision import events
    root = ec.supervisor_dir(d, model, slot)
    worker = receipts.read(root / 'worker.json')
    native = root / 'native'
    if not (native / 'latest.json').is_file():
        native = Path(worker.get('adopted_from', '/missing'))
    event = Path(worker.get('events', '/missing')).resolve()
    owner = ec.supervisor_dir(d, model).resolve()
    bound = c.get('slot_sessions', {}).get(model, {}).get(str(slot))
    session = worker.get('session') or bound
    outcome = events(event)
    if (not session or session != bound or outcome.get('session') != session
            or worker.get('session') not in (None, session)
            or sum(session == sid for bindings in c.get('slot_sessions', {}).values()
                   for sid in bindings.values()) != 1
            or not event.is_relative_to(owner) or not native.resolve().is_relative_to(owner)):
        raise ValueError('original worker session binding differs')
    terminal = ({**receipts.read(native / 'latest.json'), 'lease_held': False}
                if locked else receipts.status(native))
    if terminal.get('lease_held') is not False or not terminal.get('ended_at'):
        raise ValueError('original native terminal required')
    return root, worker, native, event, session, terminal, outcome


@contextlib.contextmanager
def _operation_lock(directory):
    # Unlike idle(), reconciliation must inspect a failed may-have-written owner.
    with (directory / 'lease.lock').open('r+') as stream:
        try:
            receipts.exclusive_lock(stream)
        except BlockingIOError as exc:
            raise receipts.Busy('original operation remains live') from exc
        yield


def _upload_event(event_path, cid, bid):
    # _http sends one complete JSON bytes body, not a file/iterator. A timed
    # out sendall leaves a suffix unsent, so the receiver cannot parse the JSON.
    # A response/read timeout may follow a real write and is always rejected.
    stdlib = str(Path(sysconfig.get_path('stdlib')) / 'http/client.py')
    source = Path(stdlib).read_text().splitlines()
    selected = []
    pending = set()
    with event_path.open('rb') as stream:
        for line in stream:
            event = json.loads(line)
            item = event.get('item', {})
            if item.get('type') != 'command_execution':
                continue
            scope = command_scope(item.get('command', ''), (cid, bid))
            if not scope or (scope['campaign'], scope['batch'], scope['kind']) != (cid, bid, 'submit'):
                continue
            if event.get('type') == 'item.started':
                pending.add(item.get('id'))
            elif event.get('type') == 'item.completed':
                if item.get('id') not in pending:
                    raise ValueError('upload command start evidence missing')
                pending.remove(item['id'])
                if item.get('exit_code') != 1:
                    raise ValueError('ambiguous earlier formal submit command')
                output = item.get('aggregated_output', '')
                first = output.split('During handling of the above exception')[0]
                frames = re.findall(r'File "([^"\n]+)", line (\d+), in ([A-Za-z_]+)\n\s*([^\n]+)', first)
                body_send = [(int(n), text.strip()) for path, n, fn, text in frames
                             if path == stdlib and fn == '_send_output' and text.strip() == 'self.send(chunk)']
                socket_send = [(int(n), text.strip()) for path, n, fn, text in frames
                               if path == stdlib and fn == 'send' and text.strip() == 'self.sock.sendall(data)']
                if (len(body_send) != 1 or len(socket_send) != 1
                        or not first.rstrip().endswith('TimeoutError: timed out')
                        or not output.rstrip().endswith('urllib.error.URLError: <urlopen error timed out>')):
                    raise ValueError('not a proven incomplete JSON body upload; response/read failures cannot replay')
                for lineno, text in body_send + socket_send:
                    if source[lineno - 1].strip() != text:
                        raise ValueError('original Python transport source differs')
                selected.append({'command_id': item['id'], 'output_sha256': hashlib.sha256(output.encode()).hexdigest(),
                                 'stdlib': stdlib, 'stdlib_sha256': file_sha(Path(stdlib))})
    if pending or len(selected) != 1:
        raise ValueError('exactly one terminated body-upload failure required')
    return selected[0]


def remote_snapshot(keys, anchor, runner=subprocess.run):
    from tools.nas_gate import load_settings
    script = Path(__file__).with_name('campaign_continuation_remote.py').read_text()
    docker = os.environ.get('KG_HUB_NAS_DOCKER',
                            'sudo -n /var/packages/ContainerManager/target/usr/bin/docker')
    # Parse trusted configuration into argv; never interpolate source data into shell.
    command = shlex.join(shlex.split(docker) + ['exec', '-i', 'kg-hub-server', 'python', '-c', script])
    result = runner(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                     load_settings()['ssh_target'], command],
                    input=json.dumps({'keys': keys, 'anchor': anchor}),
                    capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise ValueError('read-only NAS continuation evidence unavailable')
    return json.loads(result.stdout)


def _states(keys, url, token, post):
    code, reply = post('POST', url.rstrip('/') + '/api/ingest_extracted/check', {'items': keys}, token)
    rows = reply.get('items', [])
    expected = {(r['source_description'], r['source_obs_id']) for r in keys}
    found = {(r.get('source_description'), r.get('source_obs_id')): r for r in rows}
    if code != 200 or len(rows) != len(keys) or set(found) != expected:
        raise ValueError('complete, unambiguous service source readback required')
    return found


def _prefix(c, d, b, batch, results, prompt):
    latest, counts = {}, {}
    log = d / 'log.jsonl'
    for line in log.read_text().splitlines() if log.exists() else []:
        row = json.loads(line)
        if row.get('batch') == b['id']:
            oid = row.get('id')
            counts[oid] = counts.get(oid, 0) + 1
            latest[oid] = row
    order = [int(r['id']) for r in results['items']]
    acknowledged = []
    missing = False
    items = {int(i['id']): i for i in batch['items']}
    version = ec.episode_prompt_version(c, b['chain'])
    identity = ax.quality.extraction_identity(b['model'], c['executor'], c['models'][b['model']]['run_id'])
    if len(order) != len(set(order)) or set(order) != set(items):
        raise ValueError('frozen result order differs')
    for result in results['items']:
        oid = int(result['id'])
        row = latest.get(oid)
        if row is None:
            missing = True
            continue
        expected_digest = ax.extraction_payload_digest(items[oid], result, version, prompt,
                                                     batch=batch, results=results, identity=identity)
        if (missing or counts[oid] != 1 or not result.get('entities')
                or row.get('status') != 'ok' or row.get('http') != 200 or not row.get('episode_uuid')
                or row.get('model') != b['model'] or row.get('executor') != c['executor']
                or row.get('run_id') != identity['run_id'] or row.get('payload_digest') != expected_digest):
            raise ValueError('original HTTP200 acknowledgement prefix or frozen payload differs')
        acknowledged.append(row)
    if not acknowledged or len(acknowledged) >= len(order) or set(latest) - set(order):
        raise ValueError('strict partially acknowledged batch required')
    return acknowledged


def _remote_verified(snapshot, keys, acknowledged, model, c):
    prefix = {(k['source_description'], k['source_obs_id']) for k in keys[:len(acknowledged)]}
    rows = snapshot.get('executions', [])
    pairs = [(r.get('source_description'), r.get('source_obs_id')) for r in rows]
    health = snapshot.get('health', {})
    anchors = snapshot.get('anchors', [])
    after = snapshot.get('post_anchor_sources', [])
    if (snapshot.get('version') != 'partial-upload-remote-v1' or snapshot.get('keys') != keys
            or snapshot.get('read_only') is not True or snapshot.get('observation_bodies_returned') is not False
            or len(rows) != len(prefix) or set(pairs) != prefix
            or any(r.get('state') != 'succeeded' or not r.get('finished_at') for r in rows)
            or health != {'status': 'ok', 'active_extractions': 0, 'draining': False}
            or len(anchors) != 1 or snapshot.get('unparsed_post_anchor_headers') != 0
            or len(after) != 1 or after[0].get('request_id') != snapshot.get('anchor_request_id')):
        raise ValueError('NAS terminal/receive evidence incomplete or an unacknowledged request was accepted')
    anchor = anchors[0]
    if (anchor.get('extractor_model') != model or anchor.get('extractor_tool') != c['executor']
            or anchor.get('run_id') != c['models'][model]['run_id']
            or anchor.get('extractor_session') != c['id']
            or (anchor.get('source_description'), anchor.get('source_obs_id')) != tuple(
                keys[len(acknowledged) - 1][k] for k in ('source_description', 'source_obs_id'))):
        raise ValueError('NAS accepted-input attribution differs')


def reconcile_upload(cid, bid, *, url, token, post=None, snapshot=None):
    if os.environ.get('KG_HUB_CAMPAIGN_SLOT'):
        raise ValueError('only the coordinator may reconcile an upload')
    ec.tracking_guard()
    d = ec.campaign_dir(cid)
    c = ec.load(d)
    b = ec.find_batch(c, bid)
    slot = ec.batch_slot(c, b)
    root, worker, native, event, session, terminal, outcome = _worker(c, d, b['model'], slot)
    with receipts.idle(root), receipts.idle(native), _operation_lock(d / 'operations' / bid):
        record = receipts.read(d / 'operations' / bid / 'latest.json')
        batch, results, prompt, identity, sha = _inputs(c, d, b)
        keys = [{k: i[k] for k in ('source_description', 'source_obs_id')}
                for i in batch['items']]
        expected_identity = {'campaign': cid, 'batch': bid, **identity}
        if (record.get('identity') != expected_identity or record.get('kind') != 'submit'
                or record.get('state') != 'failed' or record.get('error_type') != 'URLError'
                or record.get('source_keys') != keys or record.get('input_sha256') != sha
                or terminal.get('state') != 'completed' or terminal.get('exit_code') != 0
                or outcome.get('terminal') != 'turn.completed'
                or outcome.get('recovery_kind') != 'uncertain_submit' or outcome.get('hard_stop')):
            raise ValueError('original failed upload and clean stopped native identity required')
        transport = _upload_event(event, cid, bid)
        acknowledged = _prefix(c, d, b, batch, results, prompt)
        # The request order is result order, not necessarily frozen source order.
        items = {int(i['id']): i for i in batch['items']}
        ordered = [{k: items[int(r['id'])][k] for k in ('source_description', 'source_obs_id')}
                   for r in results['items']]
        found = _states(keys, url, token, post or ax._http)
        for index, key in enumerate(ordered):
            current = found[(key['source_description'], key['source_obs_id'])]
            if index < len(acknowledged):
                if current.get('status') != 'ok' or current.get('episode_uuid') != acknowledged[index]['episode_uuid']:
                    raise ValueError('acknowledged service result identity differs')
            elif current.get('status') is not None or current.get('episode_uuid') is not None:
                raise ValueError('unacknowledged source exists; do not replay or count')
        evidence = (snapshot or remote_snapshot)(ordered, [ordered[len(acknowledged) - 1][k]
                                                          for k in ('source_description', 'source_obs_id')])
        _remote_verified(evidence, ordered, acknowledged, b['model'], c)
        proof = {'version': 'partial-upload-continuation-v1', 'at': ec._now(),
                 'identity': expected_identity, 'original_attempt': record['attempt'],
                 'original_receipt': str(d / 'operations' / bid / (record['attempt'] + '.json')),
                 'original_receipt_sha256': file_sha(d / 'operations' / bid / (record['attempt'] + '.json')),
                 'session': session, 'events': str(event), 'events_sha256': file_sha(event),
                 'input_sha256': sha, 'source_keys': keys, 'transport': transport,
                 'acknowledged': acknowledged, 'remote': evidence,
                 'remaining_ids': [int(r['id']) for r in results['items'][len(acknowledged):]]}
        path = d / 'operations' / bid / ('partial-' + uuid.uuid4().hex + '.json')
        _write(path, proof)
        receipts.write(d / 'operations' / bid / 'partial-active.json',
                       {'path': str(path), 'sha256': file_sha(path)})
        record.update(state='reconciled_partial', requires_reconciliation=False,
                      partial_continuation={'path': str(path), 'sha256': file_sha(path)})
        # may_have_written and original exit1 remain true/1: the prefix was written.
        receipts.write(d / 'operations' / bid / 'latest.json', record)
    return {'campaign': cid, 'batch': bid, 'state': 'reconciled_partial',
            'acknowledged': len(acknowledged), 'remaining': len(proof['remaining_ids']),
            'original_failure_retained': True, 'graph_writes': 0}


def partial_records(c, d, b):
    if (d / 'operations' / b['id'] / 'settlement-active.json').exists():
        from tools.campaign_settlement import records_for
        return records_for(c, d, b)
    reference = receipts.read(d / 'operations' / b['id'] / 'partial-active.json')
    if not reference:
        return []
    path = Path(reference.get('path', '/missing'))
    if not path.resolve().is_relative_to((d / 'operations' / b['id']).resolve()) or file_sha(path) != reference.get('sha256'):
        raise ValueError('partial continuation reference differs')
    proof = _read(path)
    batch, results, prompt, identity, sha = _inputs(c, d, b)
    if (proof.get('identity') != {'campaign': c['id'], 'batch': b['id'], **identity}
            or proof.get('input_sha256') != sha
            or file_sha(Path(proof['events'])) != proof.get('events_sha256')
            or file_sha(Path(proof['original_receipt'])) != proof.get('original_receipt_sha256')):
        raise ValueError('partial continuation frozen input or original evidence differs')
    records = _prefix(c, d, b, batch, results, prompt)
    if records != proof.get('acknowledged'):
        raise ValueError('partial acknowledgement ledger differs')
    items = {int(i['id']): i for i in batch['items']}
    ordered = [{k: items[int(r['id'])][k] for k in ('source_description', 'source_obs_id')}
               for r in results['items']]
    _remote_verified(proof['remote'], ordered, records, b['model'], c)
    return records


def valid_partial(c, d, b, operation):
    if operation.get('settlement_continuation'):
        from tools.campaign_settlement import valid
        return valid(c, d, b, operation)
    if operation.get('state') != 'reconciled_partial' or operation.get('requires_reconciliation'):
        return False
    try:
        reference = receipts.read(d / 'operations' / b['id'] / 'partial-active.json')
        proof = _read(Path(reference['path']))
        return (operation.get('attempt') == proof['original_attempt']
                and operation.get('partial_continuation') == reference
                and bool(partial_records(c, d, b)))
    except (OSError, KeyError, ValueError, TypeError):
        return False


def reconcile_worker(cid, model, slot, *, reason, model_preflight=None):
    if not reason.strip() or os.environ.get('KG_HUB_CAMPAIGN_SLOT'):
        raise ValueError('explicit coordinator recovery reason required')
    ec.tracking_guard()
    d = ec.campaign_dir(cid)
    c = ec.load(d)
    root, worker, native, event, session, terminal, outcome = _worker(c, d, model, slot)
    with receipts.idle(root), receipts.idle(native):
        pending = [b for b in c['batches'] if b['model'] == model
                   and ec.batch_slot(c, b) == slot and b['status'] not in ('submitted', 'released')]
        if not pending or outcome.get('hard_stop'):
            raise ValueError('pending original worker without unresolved hard stops required')
        operations = {}
        for b in pending:
            record = receipts.status(d / 'operations' / b['id'])
            if (record.get('lease_held') is not False or not (
                    record.get('state') == 'reconciled_not_written' and record.get('may_have_written') is False
                    and record.get('reconciliation', {}).get('confirmed_not_written') is True
                    or valid_partial(c, d, b, record))):
                raise ValueError('all original pending operations require complete reconciliation')
            operations[b['id']] = digest(record)
        readiness = None
        if terminal.get('exit_code') == 1 and outcome.get('terminal') == 'turn.failed':
            errors = []
            with event.open('rb') as stream:
                for line in stream:
                    row = json.loads(line)
                    if row.get('type') == 'error':
                        errors.append(row.get('message'))
                    elif row.get('type') == 'turn.failed':
                        errors.append((row.get('error') or {}).get('message'))
            if errors != ['Selected model is at capacity. Please try a different model.'] * 2:
                raise ValueError('only the exact transient capacity refusal can be reconciled')
            if all(receipts.read(d / 'operations' / b['id'] / 'latest.json').get('settlement_continuation')
                   and valid_partial(c,d,b,receipts.status(d/'operations'/b['id'])) for b in pending):
                # The exact interrupted operation is settled. Ordinary bounded
                # capacity backoff resumes the owner; no extra model probe.
                kind = 'capacity_settled'
            else:
                settings = ec.runtime_settings(c)
                readiness = ec.model_readiness([model], c['executor'], [model_preflight], **settings)
                kind = 'capacity_recovered'
        elif (terminal.get('exit_code') == 0 and outcome.get('terminal') == 'turn.completed'
              and outcome.get('recovery_kind') == 'uncertain_submit'
              and all(valid_partial(c, d, b, receipts.status(d / 'operations' / b['id'])) for b in pending)):
            kind = 'partial_upload_reconciled'
        elif (terminal.get('exit_code') == 0 and outcome.get('terminal') == 'turn.completed'
              and outcome.get('recovery_kind') == 'nas_refused'
              and all((record := receipts.status(d / 'operations' / b['id'])).get('kind') in ('dryrun', 'submit')
                      and record.get('exit_code') in (75, 76)
                      and record.get('state') == 'reconciled_not_written'
                      and record.get('may_have_written') is False
                      and record.get('reconciliation', {}).get('confirmed_not_written') is True
                      for b in pending)):
            kind = 'nas_refusal_reconciled'
        else:
            raise ValueError('native failure is not an explicitly supported continuation')
        proof = {'version': 'native-continuation-v1', 'at': ec._now(), 'reason': reason,
                 'campaign': cid, 'model': model, 'slot': slot, 'session': session, 'kind': kind,
                 'worker_sha256': file_sha(root / 'worker.json'), 'native_sha256': file_sha(native / 'latest.json'),
                 'events': str(event), 'events_sha256': file_sha(event), 'operations': operations,
                 'model_preflight': str(Path(model_preflight).resolve()) if model_preflight else None,
                 'readiness': readiness}
        path = root / ('continuation-' + uuid.uuid4().hex + '.json')
        _write(path, proof)
        receipts.write(root / 'continuation-active.json', {'path': str(path), 'sha256': file_sha(path)})
    return {'model': model, 'slot': slot, 'session': session, 'kind': kind,
            'original_exit_code': terminal['exit_code'], 'original_failure_retained': True}


def worker_reconciled(c, d, model, slot, *, locked=False, operation_locked=False):
    from tools.campaign_local_recovery import valid as local_valid
    if local_valid(c,d,model,slot,locked=locked):
        return True
    from tools.campaign_released_quality import valid as released_quality_valid
    if released_quality_valid(c,d,model,slot,locked=locked,operation_locked=operation_locked):
        return True
    from tools.campaign_ownership_recovery import valid as ownership_valid
    from tools.campaign_rework_completion import valid as completion_valid
    if completion_valid(c, d, model, slot, locked=locked):
        return True
    if ownership_valid(c, d, model, slot):
        return True
    try:
        root, worker, native, event, session, terminal, outcome = _worker(c, d, model, slot, locked=locked)
        reference = receipts.read(root / 'continuation-active.json')
        path = Path(reference['path'])
        if not path.resolve().is_relative_to(root.resolve()) or file_sha(path) != reference['sha256']:
            return False
        proof = _read(path)
        if (proof.get('campaign') != c['id'] or proof.get('model') != model or proof.get('slot') != slot
                or proof.get('session') != session or file_sha(root / 'worker.json') != proof['worker_sha256']
                or file_sha(native / 'latest.json') != proof['native_sha256']
                or file_sha(event) != proof['events_sha256']):
            return False
        for bid, expected in proof['operations'].items():
            current = receipts.read(d / 'operations' / bid / 'latest.json')
            # status() adds the observed lease flag; here the caller may own its idle lock.
            if digest({**current, 'lease_held': False}) != expected:
                return False
        if proof['kind'] == 'capacity_recovered':
            settings = ec.runtime_settings(c)
            return ec.model_readiness([model], c['executor'], [proof['model_preflight']], **settings) == proof['readiness']
        if proof['kind'] == 'capacity_settled':
            pending = [b for b in c['batches'] if b['model']==model and ec.batch_slot(c,b)==slot
                       and b['status'] not in ('submitted','released')]
            return (bool(pending) and set(proof['operations'])=={b['id'] for b in pending}
                    and all(valid_partial(c,d,b,receipts.read(d/'operations'/b['id']/'latest.json')) for b in pending))
        if proof['kind'] == 'nas_refusal_reconciled':
            pending = [b for b in c['batches'] if b['model'] == model
                       and ec.batch_slot(c, b) == slot and b['status'] not in ('submitted', 'released')]
            if set(proof['operations']) != {b['id'] for b in pending}:
                return False
            for b in pending:
                record = receipts.read(d / 'operations' / b['id'] / 'latest.json')
                if (record.get('kind') not in ('dryrun', 'submit') or record.get('exit_code') not in (75, 76)
                        or record.get('state') != 'reconciled_not_written'
                        or record.get('may_have_written') is not False
                        or record.get('reconciliation', {}).get('confirmed_not_written') is not True):
                    return False
            return bool(pending)
        return proof['kind'] == 'partial_upload_reconciled' and bool(proof['operations'])
    except (OSError, KeyError, ValueError, TypeError):
        return False
