"""Source decision admission; explicit input is not semantic truth proof."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_campaign_parallel as fixtures
from tools import campaign_decisions as decisions, extract_campaign as ec, agent_extract as ax
from utils import process_receipts as receipts


class DecisionTests(unittest.TestCase):
    setUp, tearDown, claim = (fixtures.ParallelCampaignTests.setUp,
        fixtures.ParallelCampaignTests.tearDown, fixtures.ParallelCampaignTests.claim)

    def prepare(self):
        self.old = self.claim()['batch']
        c = ec.load(self.d); c['dispatch'] = {'state': 'draining'}; ec.save(self.d, c)
        decisions.enable(self.c['id'], reason='explicit new-batch policy')
        c = ec.load(self.d); c['dispatch']['state'] = 'running'; ec.save(self.d, c)
        self.bid = self.claim('m', 2)['batch']
        self.b = ec.find_batch(ec.load(self.d), self.bid)
        self.batch, _ = ec._batch_rows(self.d, self.bid)
        self.results = {'items': [{'id': oid, 'entities': [], 'edges': []} for oid in self.b['ids']]}
        (self.d / 'results' / (self.bid + '.json')).write_text(json.dumps(self.results))
        self.prompt = ec.batch_rules_text(self.d, ec.load(self.d), self.b)
        self.sheet = ax.quality.review_sheet(self.batch, self.results, self.prompt,
                                             ec.batch_identity(ec.load(self.d), self.b))
        self.sheet['reviewer'] = 'named source reviewer'
        for row in self.sheet['items']:
            row.update(verdict='ok', note=f"Source {row['id']} explicit decision after full original review",
                       **{k: True for k in decisions.FLAGS})

    def record_all(self):
        for row in self.sheet['items']:
            decisions.record(self.c['id'], self.bid, row, reviewer=self.sheet['reviewer'])

    def test_old_batch_retains_contract_and_new_batch_requires_source_ledger(self):
        self.prepare()
        old = ec.find_batch(ec.load(self.d), self.old)
        self.assertNotIn('decision_protocol', old)
        self.assertEqual(decisions.errors(self.d, old, {}), [])
        self.assertEqual(len(decisions.errors(self.d, self.b, self.sheet)), 50)
        self.record_all()
        self.assertEqual(decisions.errors(self.d, self.b, self.sheet), [])

    def test_full_sheet_list_or_implicit_flags_cannot_be_recorded(self):
        self.prepare()
        for value in [self.sheet, self.sheet['items'], {'id': self.b['ids'][0], 'verdict': 'ok'}]:
            with self.assertRaisesRegex(ValueError, 'exactly one source'):
                decisions.record(self.c['id'], self.bid, value, reviewer='r')

    def test_context_change_rejects_old_decision(self):
        self.prepare()
        row = self.sheet['items'][0]
        self.results['items'][0]['edges'] = [{'fact': 'changed candidate'}]
        (self.d / 'results' / (self.bid + '.json')).write_text(json.dumps(self.results))
        with self.assertRaisesRegex(ValueError, 'changed'):
            decisions.record(self.c['id'], self.bid, row, reviewer=self.sheet['reviewer'])

    def test_later_sheet_edit_does_not_reuse_registered_approval(self):
        self.prepare(); self.record_all()
        self.sheet['items'][0]['note'] = 'a different review decision'
        self.assertIn('changed_or_unregistered', decisions.errors(self.d, self.b, self.sheet)[0])

    def test_repeated_record_is_idempotent_and_revision_is_retained(self):
        self.prepare(); row = self.sheet['items'][0]
        one = decisions.record(self.c['id'], self.bid, row, reviewer='r')
        two = decisions.record(self.c['id'], self.bid, row, reviewer='r')
        self.assertEqual(one, two)
        row['note'] = 'Explicit corrected source judgment'
        three = decisions.record(self.c['id'], self.bid, row, reviewer='r')
        self.assertNotEqual(one['sha256'], three['sha256'])
        self.assertTrue(Path(one['decision']).is_file())

    def test_quality_check_refuses_unregistered_bulk_true_before_copy(self):
        self.prepare(); path = self.d / 'synthetic-review.json'
        path.write_text(json.dumps(self.sheet))
        with mock.patch.object(ax, 'check_quality_file') as check:
            self.assertFalse(ec.cmd_quality_check(self.c['id'], self.bid, str(path))['ok'])
            check.assert_not_called()

    def test_direct_dryrun_cannot_bypass_source_admission(self):
        self.prepare()
        path = ax.quality_file(self.d, self.bid); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.sheet))
        with mock.patch.object(ec, 'cmd_lint', return_value=[]), mock.patch.object(ax, 'submit_results') as post:
            with self.assertRaisesRegex(SystemExit, 'source_decision_missing'):
                ec.cmd_submit(self.c['id'], self.bid, dry_run=True, url='http://invalid', token=None)
            post.assert_not_called()

    def test_wrong_slot_cannot_register_someone_elses_decision(self):
        self.prepare()
        with mock.patch.dict('os.environ', {'KG_HUB_CAMPAIGN_ID': self.c['id'],
                             'KG_HUB_CAMPAIGN_MODEL': 'm', 'KG_HUB_CAMPAIGN_SLOT': '1'}):
            with self.assertRaises(SystemExit):
                decisions.record(self.c['id'], self.bid, self.sheet['items'][0], reviewer='r')


if __name__ == '__main__': unittest.main()
