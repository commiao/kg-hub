"""Incident-bound local failure evidence. No runtime gate is changed.

The historical builder is deliberately pinned to reviewed original bytes,
not an arbitrary helper whose current contents happen to look safe. The caller must authenticate these bytes against the original rollout and
deployment evidence before any production consumer could use this review.
"""
import ast
import hashlib
import json
import re
import shlex

from tools.campaign_rework_host_receipt import require

BUILDER_SHA = '8ff17abfc0ecb2e44dabd17b26fc1d10cb1057bfcf7daf1db0b7d9923c34e0e9'
QUALITY_SHA = 'fbd25700a6cbc8bd8a0da559b519221148d09cab2f69e5b3e31bf9be17dc6ea4'
ENTRY_SHA = 'd34e383155e3bdc1e690bd2f15c7a6c7ba5b6f1e2d1a2161d31b1926d1c3a09f'
INSPECT_TEMPLATE = '''import json,pathlib
p=pathlib.Path(FILE);d=json.loads(p.read_text())['snapshot'];print('sha',d.get('sha256'));print('nodes',[(x['props'].get('name'),x['props'].get('type'),x['props'].get('summary','')[:100]) for x in d['nodes']]);print('edges',[(e['props']['uuid'],e['props'].get('fact'),e['props'].get('episodes')) for e in d['edges']])'''
PYTHON_SELECT = '''if [ -n "$KG_HUB_PYTHON" ]; then py="$KG_HUB_PYTHON"; elif [ -x "$HOME/.local/share/kg-hub/venv/bin/python" ]; then py="$HOME/.local/share/kg-hub/venv/bin/python"; else py=python3; fi'''


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def review(item, scope, historical):
    require(item.get('status') == 'failed' and type(item.get('exit_code')) is int,
            'not a completed local failure')
    argv = shlex.split(item['command'])
    require(len(argv) == 3 and argv[:2] == ['/bin/zsh', '-lc'], 'unsupported shell')
    shell = argv[2]; output = item.get('aggregated_output', '')
    audit = scope['campaign_root'] + '/' + scope['audit_relative']
    require(re.fullmatch(r'audit/[A-Za-z0-9_-]+', scope['audit_relative']), 'unbounded audit')
    disposition = None
    if shell == '"${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract" campaign repair --help':
        require(item['exit_code'] == 2 and output.startswith('usage: extract_cli.py ')
                and "invalid choice: 'repair'" in output and 'Traceback' not in output,
                'not parser-only help failure')
        disposition = 'argument_parser_help_only'
    inline = re.fullmatch(r"python3 - <<'PY'\n(.*)\nPY", shell, re.S)
    if inline:
        tree = ast.parse(inline[1])
        try:
            path = ast.literal_eval(tree.body[1].value.args[0])
        except (AttributeError, TypeError, ValueError, IndexError) as exc:
            raise ValueError('unknown local inspection') from exc
        allowed = {audit + '/' + str(n) + '.live-preview.json' for n in scope['source_ids']}
        require(path in allowed, 'foreign inspected source')
        expected = INSPECT_TEMPLATE.replace('FILE', repr(path))
        require(ast.dump(tree) == ast.dump(ast.parse(expected)), 'not the exact readonly inspection AST')
        require(item['exit_code'] == 1 and output == "sha None\nTraceback (most recent call last):\n  File \"<stdin>\", line 2, in <module>\nKeyError: 'nodes'\n", 'unexpected inspection failure')
        disposition = 'local_frozen_preview_inspection_key_error'
    builder = audit + '/build-title-repairs.py'
    if shell == 'python3 ' + builder:
        source = historical['builder']; quality = historical['quality_module']
        require(sha(source) == BUILDER_SHA and sha(quality) == QUALITY_SHA
                and historical['utils_init'] == '', 'historical code/import bytes differ from reviewed evidence')
        patch_input = historical['original_patch_input']
        m = re.fullmatch(r'const patch=("(?:[^"\\]|\\.)*");\ntext\(await tools.apply_patch\(patch\)\)\n', patch_input)
        require(m, 'unknown historical patch wrapper')
        patch = json.loads(m[1]); lines = patch.splitlines()
        require(lines[:2] == ['*** Begin Patch', '*** Add File: ' + builder]
                and lines[-1] == '*** End Patch' and all(x.startswith('+') for x in lines[2:-1]),
                'historical patch contains other changes')
        require('\n'.join(x[1:] for x in lines[2:-1]) + '\n' == source, 'original patch bytes disagree')
        loops = [n for n in ast.parse(source).body if isinstance(n, ast.For)]
        require(len(loops) == 1, 'one reviewed historical loop required')
        ids = ast.literal_eval(loops[0].iter)
        require(scope['campaign_root'] in source and type(ids) is tuple
                and all(type(n) is int and n in scope['source_ids'] for n in ids),
                'historical helper outside selected batch')
        expected = ('Traceback (most recent call last):\n'
                    f'  File "{builder}", line 46, in <module>\n'
                    "    assert live['episode']['source_obs_id'] == source['source_obs_id']\n"
                    '           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^\n'
                    "KeyError: 'source_obs_id'\n")
        require(item['exit_code'] == 1 and output == expected, 'not the original first-iteration prewrite failure')
        disposition = 'reviewed_historical_builder_first_source_prewrite_key_error'
    if shell.startswith(PYTHON_SELECT + '\n'):
        require(sha(historical['shell_entry']) == ENTRY_SHA, 'different shell entry bytes')
        suffix = shell[len(PYTHON_SELECT) + 1:]
        for n in scope['source_ids']:
            expected = ('"$py" "${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract"'
                        f' campaign repair-decision --id {scope["campaign_id"]} --batch {scope["batch"]}'
                        f' --file {audit}/{n}.reviewed-request.json --reviewer \'gpt-6-sol/codex source rework\'')
            if suffix == expected:
                break
        else:
            raise ValueError('not the exact nonexecuted decision command')
        try:
            ast.parse(historical['shell_entry'])
        except SyntaxError as exc:
            require(exc.lineno == 4, 'different parser failure boundary')
        else:
            raise ValueError('entry can execute as Python')
        expected_output = (f'  File "{scope["published_entry"]}", line 4\n'
                           '    code_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
                           '              ^\nSyntaxError: invalid syntax\n')
        require(item['exit_code'] == 1 and output == expected_output, 'not compile-only shell-entry failure')
        disposition = 'python_parser_rejected_shell_entry_before_body'
    require(disposition is not None, 'unclassified failed command remains blocked')
    return {'item': item['id'], 'evidence_disposition': disposition,
            'original_status': item['status'], 'original_exit_code': item['exit_code'],
            'semantic_acceptance': False, 'trusted_loader_validated': False,
            'restart_allowed': False}
