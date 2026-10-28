"""A native's stale recursion marker never authorizes a business backend."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tools import extract_cli


class EpochTest(unittest.TestCase):
    def test_known_old_publication_reselects_verified_active_adapter_once(self):
        with tempfile.TemporaryDirectory() as temp:
            parent=Path(temp);current=parent/('a'*40);old=parent/('b'*40)
            for root in (current,old):
                root.mkdir();(root/'RELEASE.json').write_text(json.dumps({'name':'kg-hub','commit':root.name}))
            env={'LLM_EVAL_ACTIVE_BUNDLE':'active.json','LLM_EVAL_DELEGATED_BACKEND':old.name}
            def common(args):
                self.assertNotIn('LLM_EVAL_DELEGATED_BACKEND',os.environ)
                self.assertEqual(os.environ['LLM_EVAL_BACKEND_RESELECTED'],'1')
                self.assertEqual(args,['campaign','--scenario','kg_hub.entity_extract','--','operation-status','--id','original'])
                return 0
            with mock.patch.object(extract_cli,'CODE_ROOT',current),mock.patch.dict(os.environ,env),mock.patch('llm_eval.cli.main',side_effect=common) as call:
                self.assertEqual(extract_cli.main(['campaign','operation-status','--id','original']),0)
                call.assert_called_once();self.assertEqual(os.environ['LLM_EVAL_DELEGATED_BACKEND'],old.name)
                self.assertNotIn('LLM_EVAL_BACKEND_RESELECTED',os.environ)
            for marker,hop in [('c'*40,''),(old.name,'1'),('../foreign','')]:
                with mock.patch.object(extract_cli,'CODE_ROOT',current),mock.patch.dict(os.environ,{**env,'LLM_EVAL_DELEGATED_BACKEND':marker,'LLM_EVAL_BACKEND_RESELECTED':hop}),mock.patch('llm_eval.cli.main') as call:
                    with self.assertRaisesRegex(SystemExit,'identity differs'):extract_cli.main(['campaign','status'])
                    call.assert_not_called()


if __name__=='__main__':unittest.main()
