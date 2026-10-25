"""Shared adapter/bundle contracts, no API or graph calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from kg_hub_eval import EntityExtraction,pin_campaign_backend
from llm_eval.evidence import read,seal


class SharedAdapterTest(unittest.TestCase):
    def test_graph_cases_group_without_changing_original_id_or_lookahead(self):
        adapter=EntityExtraction()
        cases=[{'id':'case-'+str(i),'input':{'id':i,'episode_body':'[DISCOVERY] Test '+str(i)},
                'context':{'lookahead':['later']}} for i in (3,4,5)]
        requests=adapter.build_requests(cases,{'chunk':2})
        self.assertEqual([r['case_ids'] for r in requests],[['case-3','case-4'],['case-5']])
        self.assertEqual(requests[0]['observation_ids'],[3,4])
        self.assertIn('later',requests[0]['query'])
        parsed=adapter.parse_response(requests[0],json.dumps({'items':[{'id':3,'entities':[],'edges':[]}]}))
        self.assertFalse(parsed['valid'])
        self.assertTrue(adapter.aggregate_case(cases[0],[parsed])['valid'])
        self.assertFalse(adapter.aggregate_case(cases[1],[parsed])['valid'])

    def test_formal_kg_policy_cannot_be_replaced_by_generic_diagnostic_score(self):
        desc=EntityExtraction().describe()
        self.assertEqual(set(desc['modes']),{'smoke','admission','blind','campaign'})
        self.assertFalse(desc['policy']['calibrated'])
        from kg_hub_eval.business.tools.extract_bench import THRESHOLDS,ADMISSION_MIN_ITEMS,BLIND_N
        self.assertEqual(ADMISSION_MIN_ITEMS,30);self.assertEqual(BLIND_N,10)
        self.assertEqual(THRESHOLDS['hit_zh_pct'],90)
        self.assertEqual(THRESHOLDS['blind_max_errors'],1)

    def test_campaign_preserves_argv_and_refuses_changed_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'release';root.mkdir()
            required=['tools/extract','tools/extract_cli.py','tools/extract_campaign.py','tools/agent_extract.py',
                'utils/extraction_quality.py','utils/semantic_regression.py','utils/bench_reference.py',
                'utils/extracted_ingest.py','config/agent_extract_prompt.md']
            for name in required:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('synthetic backend fixture')
            (root/'RELEASE.json').write_text(json.dumps({'name':'kg-hub','commit':'a'*40}))
            path=Path(temp)/'backend.json';manifest=pin_campaign_backend(root,path)
            with patch('kg_hub_eval.subprocess.run',return_value=Mock(returncode=0)) as call:
                argv=['submit','--campaign','private-run','--results','x.json','--dry-run']
                self.assertEqual(EntityExtraction().campaign(argv,backend_manifest=manifest),0)
                self.assertEqual(call.call_args.args[0],[str(root.resolve()/'tools/extract'),'campaign',*argv])
                self.assertFalse(call.call_args.kwargs['check'])
            (root/'utils/extraction_quality.py').write_text('changed quality gate')
            with patch('kg_hub_eval.subprocess.run') as call:
                with self.assertRaisesRegex(ValueError,'changed'):
                    EntityExtraction().campaign(argv,backend_manifest=manifest)
                call.assert_not_called()

if __name__=='__main__':unittest.main()
