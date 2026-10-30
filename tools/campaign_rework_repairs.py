"""Reconcile a separately audited set of explicit repair writes using fresh GETs.

This module does not classify the other commands, authorize a retry, attest
semantic correctness, or substitute stored repair status for live graph readback.
The caller must independently pin the original inventory and expected item IDs,
and derive the audit directory, source keys and provenance from original scope.
"""
import re
import shlex
from urllib.parse import urlencode

from tools.campaign_rework_evidence import read_regular, unique_json
from tools.campaign_rework_host_receipt import require
from utils.extraction_repair import parse_request, digest, encoded


PYTHON_WRAPPER = ('if [ -n "$KG_HUB_PYTHON" ]; then py="$KG_HUB_PYTHON"; '
                  'elif [ -x "$HOME/.local/share/kg-hub/venv/bin/python" ]; then '
                  'py="$HOME/.local/share/kg-hub/venv/bin/python"; else py=python3; fi')
REPAIR_CLI = '"$py" "${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extraction_repair.py"'


def read_status(repair_id, url, http=None):
    """Read through the same authenticated public HTTP client as repair CLI."""
    require(type(repair_id) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,119}', repair_id),
            'invalid repair ID')
    from tools import agent_extract as ax
    get = http or ax._http
    return get('GET', url.rstrip('/') + '/api/extraction/repair?' +
               urlencode({'repair_id': repair_id}), None, ax.api_token())


def reconcile(native_rows, inventory, *, expected_items, audit, source_keys,
              model, executor, run_id, status_get):
    """Check original pinned local writes, then request each current public status.

    status_get is an injected GET transport, never a loaded status-file mapping.
    Results remain observations; the future recovery consumer must hold its
    actual process locks and enforce all independent gates.
    """
    require(type(inventory) is list and inventory and type(expected_items) is set
            and len(inventory) == len(expected_items), 'explicit inventory size mismatch')
    require(all(type(x) is dict and set(x) == {'item', 'source_id', 'request_sha256', 'apply_sha256'}
                for x in inventory), 'invalid explicit inventory entry')
    require({x['item'] for x in inventory} == expected_items, 'explicit inventory item mismatch')
    starts, ends = {}, {}
    for row in native_rows:
        item = row.get('item', {})
        if item.get('id') not in expected_items:
            continue
        require(item.get('type') == 'command_execution', 'repair tool type changed')
        if row.get('type') in ('item.started', 'item.completed'):
            dest = starts if row['type'] == 'item.started' else ends
            require(item['id'] not in dest, 'duplicate original repair event')
            require(dest is starts or item['id'] in starts, 'repair completion precedes start')
            dest[item['id']] = item
    require(set(starts) == expected_items == set(ends), 'missing original repair start or terminal')
    prepared, seen_repairs, seen_sources = [], set(), set()
    for entry in inventory:
        key, source = entry['item'], entry['source_id']
        require(type(source) is int and source in source_keys and source not in seen_sources,
                'repair source outside original batch or repeated')
        seen_sources.add(source)
        start, end = starts[key], ends[key]
        require(start.get('command') == end.get('command') and end.get('status') == 'completed'
                and type(end.get('exit_code')) is int and end['exit_code'] == 0,
                'original repair command did not complete successfully')
        args = shlex.split(end['command'])
        request_name, apply_name = f'{source}.reviewed-request.json', f'{source}.apply.json'
        tail = f'{REPAIR_CLI} apply --file {audit}/{request_name} --out {audit}/{apply_name}'
        require(len(args) == 3 and args[:2] == ['/bin/zsh', '-lc']
                and args[2] in (PYTHON_WRAPPER + '; ' + tail, PYTHON_WRAPPER + '\n' + tail),
                'original repair argv differs from exact bounded apply')
        original = unique_json(read_regular(audit, request_name, entry['request_sha256']))
        applied = unique_json(read_regular(audit, apply_name, entry['apply_sha256']))
        request = parse_request(original)
        rid = request['repair_id']
        require(rid not in seen_repairs, 'duplicate repair ID')
        seen_repairs.add(rid)
        identity = source_keys[source]
        require((request['source_description'], request['source_obs_id']) == tuple(identity),
                'original repair source identity changed')
        replacement = request['replacement']
        require(type(replacement) is dict
                and (replacement.get('source_description'), replacement.get('source_obs_id')) == tuple(identity)
                and replacement.get('extractor_model') == model
                and replacement.get('extractor_tool') == executor and replacement.get('run_id') == run_id,
                'original replacement provenance changed')
        require(type(applied) is dict and type(applied.get('http')) is int and applied['http'] == 200
                and applied.get('status') == 'repaired' and applied.get('verified') is True
                and applied.get('repair_id') == rid and applied.get('duplicate') is False,
                'original apply receipt is not a verified first write')
        require(encoded(unique_json(end.get('aggregated_output', ''))) == encoded(applied),
                'retained apply receipt differs from original terminal output')
        prepared.append((entry, request, applied))
    # Validate every local entry before any network request. No shell is executed.
    statuses, results = [], []
    for entry, request, applied in prepared:
        rid = request['repair_id']
        code, body = status_get(rid)
        require(type(code) is int and code == 200 and type(body) is dict and body.get('status') == 'ok',
                'fresh public repair status unavailable')
        repair = body.get('repair', {})
        require(type(repair) is dict and repair.get('repair_id') == rid and repair.get('state') == 'applied'
                and repair.get('request_sha256') == digest(request)
                and encoded(repair.get('request')) == encoded(request),
                'fresh public request is not the exact original canonical request')
        result = repair.get('result')
        require(type(result) is dict and result.get('verified') is True
                and encoded(result) == encoded({k: v for k, v in applied.items() if k not in ('http', 'duplicate')}),
                'fresh public result differs from original verified apply')
        statuses.append({'repair_id': rid, 'http': code, 'body': body})
        results.append({'item': entry['item'], 'source_id': entry['source_id'], 'repair_id': rid,
                        'canonical_request_sha256': digest(request), 'state': 'applied', 'verified': True})
    return {'explicit_repairs': results, 'public_status_observations': statuses,
            'full_command_effects_validated': False, 'live_graph_readback_validated': False,
            'semantic_acceptance': False, 'restart_allowed': False}
