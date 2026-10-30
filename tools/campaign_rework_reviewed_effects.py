"""Bind a reviewed incident's source effects to original immutable bytes.

The private profile contains hashes and classifications, no executable source.
Only that reviewed incident is recognized. This is not an arbitrary-shell safety
analyzer, historical import trace, quality verdict, or permission to retry.
"""
from collections import Counter
import json
from pathlib import Path

from tools.campaign_rework_evidence import digest, read_regular, unique_json
from tools.campaign_rework_host_receipt import require

PROFILE_SHA256 = '75c19ddfca3e5ec3d924dcdc83303721025e6ae2123d8443526e64911bf5fac1'
PROFILE_RELATIVE = 'recovery-profiles/' + PROFILE_SHA256 + '.json'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def inspect(context, campaign_root, releases_root, sessions_root):
    """Read the private profile at the code-pinned path and digest; no caller-selected approval."""
    profile = unique_json(read_regular(Path(campaign_root), PROFILE_RELATIVE, PROFILE_SHA256))
    result = _inspect(context, Path(campaign_root), Path(releases_root), Path(sessions_root), profile)
    result['review_profile_sha256'] = PROFILE_SHA256
    return result


def _inspect(context, campaign_root, releases_root, sessions_root, profile):
    require(profile.get('schema') == 'reviewed-rework-source-effects-v1'
            and profile.get('restart_allowed') is False, 'invalid reviewed profile')
    identity, manifest = context['identity'], context['manifest']
    require(digest(encoded(identity)) == profile['identity_sha256']
            and manifest.get('identity') == identity
            and digest(encoded(manifest)) == profile['manifest_sha256'],
            'original context differs from independently reviewed pins')
    roots = {'campaign': campaign_root, 'release': releases_root / identity['release'],
             'sessions': sessions_root}
    # Roots come from the official caller configuration, and every file's
    # relative path and hash remain bound to the independently pinned manifest.
    # No process state is inferred from these reads.
    original = {}
    for role, ref in manifest['files'].items():
        original[role] = read_regular(roots[ref['anchor']], ref['path'], ref['sha256'])
    require(digest(original['events']) == profile['events_sha256'], 'unreviewed original transcript')
    rows = [unique_json(line) for line in original['events'].splitlines()]
    expected = {x['item']: x for x in profile['commands']}
    require(len(expected) == len(profile['commands']), 'duplicate reviewed command')
    starts, ends, changes = {}, {}, []
    for row in rows:
        item = row.get('item', {}); key = item.get('id')
        if item.get('type') not in ('command_execution', 'file_change'): continue
        if row.get('type') == 'item.started':
            require(key not in starts, 'duplicate original start'); starts[key] = item
        elif row.get('type') == 'item.completed':
            require(key in starts and key not in ends, 'unpaired original completion'); ends[key] = item
    commands = {k:v for k,v in starts.items() if v['type']=='command_execution'}
    require(set(commands)==set(expected), 'review does not cover every original command')
    for key, item in commands.items():
        end = ends.get(key)
        terminal = None if end is None else {'status':end.get('status'), 'exit_code':end.get('exit_code')}
        require(digest(item['command'].encode()) == expected[key]['command_sha256']
                and terminal == expected[key]['native_terminal']
                and (end is None or end.get('command') == item['command']),
                'reviewed command or original terminal differs')
    for key, item in starts.items():
        if item['type'] != 'file_change': continue
        end = ends.get(key, {})
        require(end.get('changes') == item['changes'] and end.get('status') == 'completed',
                'original file change incomplete')
        changes.append({'item':key,'kind':'file_change',
                        'native_terminal':{'status':end['status'],'exit_code':end.get('exit_code')},
                        'classification':'assigned_audit_file_change','changes':item['changes']})
    require(len(changes) == profile['file_change_count']
            and digest(encoded(changes)) == profile['file_changes_sha256'], 'file effects differ from reviewed set')
    references = {}
    for anchor, pins in ((roots['release'],profile['release_files']),
                         (campaign_root,profile['campaign_files'])):
        for relative, sha in pins.items():
            read_regular(anchor,relative,sha)
            references[str(anchor/relative)]={'sha256':sha}
    inventory=profile['repair_inventory']
    applies={k for k,v in expected.items() if v['classification']=='explicit_public_repair_apply'}
    require(len(inventory)==len(applies) and {x['item'] for x in inventory}==applies,
            'reviewed graph write inventory incomplete')
    return {'review_profile_canonical_sha256':digest(encoded(profile)),
            'command_count':len(commands),'file_change_count':len(changes),
            'classifications':dict(Counter(x['classification'] for x in expected.values())),
            'repair_inventory':inventory,'retained_source_references':references,
            'source_effect_review_bound':True,'original_process_import_trace_available':False,
            'live_repairs_reconciled':False,'actual_process_locks_validated':False,
            'semantic_acceptance':False,'restart_allowed':False}
