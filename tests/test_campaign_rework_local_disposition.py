"""Test disposition contracts with synthetic reviewed-code versions."""
import copy
import json
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tools import campaign_rework_local_disposition as m

SCOPE={'campaign_root':'/tmp/campaign','audit_relative':'audit/rework','source_ids':[101,102],
       'campaign_id':'campaign','batch':'model-001','published_entry':'/tmp/published/tools/extract'}
AUDIT='/tmp/campaign/audit/rework'


def item(script,output,code=1):
    return {'id':'local','status':'failed','exit_code':code,'command':shlex.join(['/bin/zsh','-lc',script]),'aggregated_output':output}


def builder_fixture():
    # Only the digest allowlist is replaced in this fixture; production accepts
    # its reviewed historic version, never arbitrary submitted helper bytes.
    src="root = '/tmp/campaign'\nfor obs_id in (101,102):\n    pass\n"
    patch='*** Begin Patch\n*** Add File: '+AUDIT+'/build-title-repairs.py\n'+''.join('+'+line+'\n' for line in src.splitlines())+'*** End Patch'
    historical={'builder':src,'quality_module':'import json\n','utils_init':'',
                'original_patch_input':'const patch='+json.dumps(patch)+';\ntext(await tools.apply_patch(patch))\n'}
    output=('Traceback (most recent call last):\n  File "'+AUDIT+'/build-title-repairs.py", line 46, in <module>\n'
            "    assert live['episode']['source_obs_id'] == source['source_obs_id']\n"
            '           ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^\n'+"KeyError: 'source_obs_id'\n")
    return historical,item('python3 '+AUDIT+'/build-title-repairs.py',output)


class LocalDispositionTests(unittest.TestCase):
    def test_exact_readonly_failure_and_mutations(self):
        script="python3 - <<'PY'\n"+m.INSPECT_TEMPLATE.replace('FILE',repr(AUDIT+'/101.live-preview.json'))+'\nPY'
        output="sha None\nTraceback (most recent call last):\n  File \"<stdin>\", line 2, in <module>\nKeyError: 'nodes'\n"
        original=item(script,output)
        self.assertEqual(m.review(original,SCOPE,{})['evidence_disposition'],'local_frozen_preview_inspection_key_error')
        for transform in (lambda s:s+'; touch /tmp/other',lambda s:s.replace('101.live','999.live'),lambda s:s.replace('print(',"open('/tmp/write','w');print(",1)):
            with self.assertRaises(ValueError):m.review(item(transform(script),output),SCOPE,{})
        with self.assertRaises(ValueError):m.review(item(script,output+'prior output'),SCOPE,{})

    def test_parser_help_cannot_waive_other_command(self):
        script='"${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract" campaign repair --help'
        output="usage: extract_cli.py ... invalid choice: 'repair'\n"
        r=m.review(item(script,output,2),SCOPE,{})
        self.assertIs(r['restart_allowed'],False)
        for cmd in (script+'; touch /tmp/other',script.replace('--help','--apply')):
            with self.assertRaises(ValueError):m.review(item(cmd,output,2),SCOPE,{})

    def test_historical_builder_requires_exact_reviewed_code_and_patch(self):
        history,original=builder_fixture()
        with mock.patch.multiple(m,BUILDER_SHA=m.sha(history['builder']),QUALITY_SHA=m.sha(history['quality_module'])):
            result=m.review(original,SCOPE,history)
            self.assertEqual(result['original_exit_code'],1)
            self.assertIs(result['restart_allowed'],False)
            for field in ('builder','quality_module','utils_init','original_patch_input'):
                changed=copy.deepcopy(history);changed[field]+='\n'
                with self.subTest(field=field),self.assertRaises(ValueError):m.review(original,SCOPE,changed)
            bad=copy.deepcopy(original);bad['aggregated_output']='prior write\n'+bad['aggregated_output']
            with self.assertRaises(ValueError):m.review(bad,SCOPE,history)
            scope=copy.deepcopy(SCOPE);scope['source_ids']=[999]
            with self.assertRaises(ValueError):m.review(original,scope,history)
        # The synthetic version is never accepted by the production allowlist.
        with self.assertRaises(ValueError):m.review(original,SCOPE,history)

    def test_compile_only_shell_entry_and_wrong_body(self):
        shell='#!/bin/sh\n# entry\nset -eu\ncode_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
        cmd=m.PYTHON_SELECT+'\n'+('"$py" "${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract"'
             +" campaign repair-decision --id campaign --batch model-001 --file "+AUDIT+"/101.reviewed-request.json --reviewer 'gpt-6-sol/codex source rework'")
        output='  File "/tmp/published/tools/extract", line 4\n    code_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n              ^\nSyntaxError: invalid syntax\n'
        with mock.patch.object(m,'ENTRY_SHA',m.sha(shell)):
            self.assertEqual(m.review(item(cmd,output),SCOPE,{'shell_entry':shell})['evidence_disposition'],'python_parser_rejected_shell_entry_before_body')
            with self.assertRaises(ValueError):m.review(item(cmd+'; touch /tmp/other',output),SCOPE,{'shell_entry':shell})
            with self.assertRaises(ValueError):m.review(item(cmd,output),SCOPE,{'shell_entry':'print(1)\n'})

if __name__=='__main__':unittest.main()
