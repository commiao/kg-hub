"""Coordinator acceptance of a worker's denied summary *preview*.

The denied governance action stays denied. An identity-only read and completed
source repairs may allow the original owner to resume unrelated batch work.
This never grants workers summary governance or certifies all shared assertions.
"""
import json
from pathlib import Path
import shlex
from urllib.parse import urlencode

from tools import agent_extract as ax, campaign_continuation as cc
from tools.campaign_rework_completion import _shell

DENIAL = 'shared node summary correction requires coordinator review of every backing source'


def validate(c, d, b, model, slot, session, event, rows, handoff, *, url, token, http=None):
    inputs = {}

    def retained(reference):
        path = Path(reference['path']).resolve(strict=True)
        if not path.is_relative_to((d / 'audit').resolve()) or cc.file_sha(path) != reference['sha256']:
            raise ValueError('hash-bound campaign audit evidence required')
        inputs[str(path)] = reference['sha256']
        return json.loads(path.read_text())

    path = Path(handoff).resolve(strict=True)
    value = retained({'path': str(path), 'sha256': cc.file_sha(path)})
    expected = {'version': 'shared-summary-handoff-v1', 'campaign': c['id'], 'batch': b['id'],
                'model': model, 'slot': slot, 'session': session, 'events_sha256': cc.file_sha(event)}
    if (any(value.get(k) != v for k, v in expected.items()) or not value.get('reviewer', '').strip()
            or not value.get('note', '').strip() or value.get('accepted') is not True
            or value.get('disposition') != 'identity_only_no_summary_change'):
        raise ValueError('explicit original-owner identity-only coordinator acceptance required')
    failed = [r for r in rows if r['exit_code']]
    if not failed:
        raise ValueError('original public summary preview refusal required')
    rejected = failed[-1]
    if (rejected['id'] != value.get('command_id') or rejected['exit_code'] != 1
            or rejected.get('aggregated_output', '').strip() != DENIAL):
        raise ValueError('exact original public summary preview refusal required')
    if any(r['exit_code'] in (75, 76) or r['exit_code'] != 0 and any(word in r.get('aggregated_output', '') for word in (
            'This action was rejected due to unacceptable risk', 'PermissionError:',
            'batch belongs to a different worker slot', 'HTTP 403', 'HTTP 401')) for r in rows):
        raise ValueError('approval, NAS or other permission refusal requires its own disposition')
    # A preparatory here-doc is allowed, but the refused public invocation must
    # be the final literal shell line. Never execute or replay the command.
    args = shlex.split(_shell(rejected['command']).strip().splitlines()[-1])
    entry = str(Path('~/.local/share/kg-hub/current/tools/extract').expanduser())
    if args and args[0] == '${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract':
        args[0] = entry
    if (len(args) != 7 or args[:4] != [entry, 'nodes', 'preview', '--file'] or args[5] != '--out'):
        raise ValueError('literal public nodes preview --file --out required')
    scope = retained(value['scope'])
    if Path(args[4]).resolve() != Path(value['scope']['path']).resolve() or scope.get('operation') != 'summary':
        raise ValueError('original refused summary scope differs')
    preview = retained(value['identity_preview'])
    nodes = preview.get('snapshot', {}).get('nodes', [])
    selected = [n for n in nodes if n.get('uuid') == value.get('node_uuid')]
    if preview.get('http') != 200 or len(selected) != 1:
        raise ValueError('complete coordinator identity read required')
    node = selected[0]; selector = scope.get('node', {})
    if (selector not in ({'uuid': node['uuid']}, {'name': node.get('props', {}).get('name')})
            or not node.get('props', {}).get('summary') or preview.get('scope', {}).get('operation') != 'summary'
            or preview.get('scope', {}).get('node') != {'uuid': node['uuid']}):
        raise ValueError('reviewed identity differs from the refused node')
    # Each original review error needs its own applied/verified source repair.
    # A coordinator assertion alone cannot stand in for service readback.
    errors = {r['obs'] for r in b.get('reviews', []) if r.get('verdict') == 'error'}
    repairs = value.get('repairs', [])
    if (not errors or {r.get('source_id') for r in repairs} != errors or len(repairs) != len(errors)):
        raise ValueError('all original review errors require distinct verified repairs')
    frozen = json.loads((d / 'batches' / (b['id'] + '.json')).read_text())
    sources = {i['id']: i for i in frozen['items']}
    readbacks = []
    for repair in repairs:
        acceptance = retained(repair['acceptance'])
        if acceptance.get('accepted') is not True or acceptance.get('original_random_error_retained') is not True:
            raise ValueError('explicit source acceptance preserving the original error required')
        code, body = (http or ax._http)('GET', url.rstrip('/') + '/api/extraction/repair?' +
                                      urlencode({'repair_id': repair['repair_id']}), None, token)
        actual = body.get('repair', {}); request = actual.get('request', {}); result = actual.get('result', {})
        source = sources[repair['source_id']]
        replacement = request.get('replacement', {})
        if (code != 200 or body.get('status') != 'ok' or actual.get('state') != 'applied'
                or actual.get('repair_id') != repair['repair_id'] or result.get('verified') is not True
                or any(request.get(k) != source.get(k) for k in ('source_description', 'source_obs_id'))
                or replacement.get('extractor_model') != model or replacement.get('extractor_tool') != c['executor']
                or replacement.get('run_id') != c['models'][model]['run_id']):
            raise ValueError('fresh applied verified repair for the exact source and extraction identity required')
        readbacks.append({'source_id': repair['source_id'], 'repair_id': repair['repair_id'],
                          'request_sha256': actual.get('request_sha256'), 'verified': True})
    for evidence in value.get('evidence', []):
        retained(evidence)
    return inputs, {'reviewer': value['reviewer'], 'disposition': value['disposition'],
                    'node_uuid': node['uuid'], 'repairs': readbacks, 'shared_summary_write_authorized': False}
