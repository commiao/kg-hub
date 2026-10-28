"""Summary-only governance must preserve all existing graph/source identities."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_graph_governance import Graph, run, GROUP
from tools import graph_governance as cli
from utils import graph_governance as gov


class NoEmbedder:
    async def create_batch(self, values):
        raise AssertionError('a summary-only correction must not call embedding/model services')


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph()
        self.graph.node('n', 'Tool', 'worker dashboard', summary='Telemetry and an unsupported review instruction.',
                        created_at='2026-10-01T00:00:00Z', extractor_model='original-model')
        self.graph.node('x', 'Concept', 'worker telemetry')
        self.graph.edge('e1', 'n', 'x', 'Dashboard displays telemetry.', ['p1'], '2026-10-01T00:00:00Z', 'displays')
        self.graph.edge('e2', 'n', 'x', 'Dashboard displays queue history.', ['p2'], '2026-10-02T00:00:00Z', 'displays')
        self.graph.episode('p1', ['e1'], ['n', 'x'])
        self.graph.episode('p2', ['e2'], ['n', 'x'])
        self.graph.episodes['p1']['content'] = 'The dashboard displays active worker telemetry.'
        self.graph.episodes['p2']['content'] = 'The dashboard also displays current and historical queue trends.'
        self.g = SimpleNamespace(driver=self.graph, embedder=NoEmbedder())
        self.scope = {'operation': 'summary', 'node': {'uuid': 'n'},
                      'summary_to': 'Displays active worker telemetry and current and historical queue trends.'}

    def reviewed(self):
        first = run(gov.preview(self.g, self.scope, GROUP))
        reviews = [{'uuid': e['uuid'], 'sha256': gov.digest(e),
                    'note': 'Read the full source and retained only its supported dashboard behavior: ' + e['uuid']}
                   for e in first['snapshot']['episodes']]
        second = run(gov.preview(self.g, {**self.scope, 'reviewed_episodes': reviews}, GROUP))
        request = {**second['scope'], 'governance_id': 'summary-1', 'reason': 'Remove an unsupported review cue.',
                   'reviewer': 'coordinator', 'expected_snapshot_sha256': second['snapshot']['snapshot_sha256']}
        return request, second

    def test_listing_is_read_only_and_exposes_every_complete_source(self):
        before = deepcopy((self.graph.nodes, self.graph.edges, self.graph.mentions, self.graph.episodes))
        p = run(gov.preview(self.g, self.scope, GROUP))
        self.assertFalse(p['plan']['applicable'])
        self.assertEqual(self.graph.mutations, [])
        self.assertEqual(before, (self.graph.nodes, self.graph.edges, self.graph.mentions, self.graph.episodes))
        self.assertEqual({s['uuid'] for s in p['plan']['summary_sources']}, {'p1', 'p2'})
        text = cli.render_markdown(p)
        for e in self.graph.episodes.values():self.assertIn(e['content'], text)
        with self.assertRaises(gov.GovernanceError):
            gov.parse_request({**p['scope'], 'governance_id': 'summary-1', 'reason': 'r', 'reviewer': 'c',
                               'expected_snapshot_sha256': p['snapshot']['snapshot_sha256']})

    def test_apply_preserves_full_node_origin_vectors_edges_mentions_and_sources(self):
        request, p = self.reviewed()
        before = deepcopy((self.graph.nodes, self.graph.edges, self.graph.mentions, self.graph.episodes))
        result = run(gov.apply(self.g, request, GROUP))
        self.assertTrue(result['verified'])
        expected = before[0]['n']['props']
        expected.update(summary=self.scope['summary_to'], governance_ids=['summary-1'])
        self.assertEqual(before[0], self.graph.nodes)
        self.assertEqual(before[1:], (self.graph.edges, self.graph.mentions, self.graph.episodes))
        audit = run(gov.audit(self.graph, 'summary-1'))
        self.assertIn('unsupported review instruction', audit['before']['nodes'][0]['props']['summary'])
        self.assertEqual(audit['request']['scope']['reviewed_episodes'], request['reviewed_episodes'])
        self.assertEqual(p['plan']['edges'], [])
        self.assertEqual(p['plan']['mentions'], [])
        self.assertEqual(p['plan']['episode_refs'], [])
        writes = len(self.graph.mutations)
        self.assertTrue(run(gov.apply(self.g, request, GROUP))['duplicate'])
        self.assertEqual(len(self.graph.mutations), writes)

    def test_shared_sources_cannot_be_omitted_or_replaced_by_stale_hashes(self):
        request, _ = self.reviewed()
        for reviews, code in [(request['reviewed_episodes'][:1], 'summary_review_incomplete'),
                              ([{**r, 'sha256': '0' * 64} for r in request['reviewed_episodes']], 'stale_summary_evidence')]:
            with self.subTest(code=code), self.assertRaises(gov.GovernanceError) as caught:
                run(gov.preview(self.g, {**self.scope, 'reviewed_episodes': reviews}, GROUP))
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.graph.mutations, [])

    def test_source_or_node_changes_after_review_prevent_any_write(self):
        request, _ = self.reviewed()
        self.graph.episodes['p2']['content'] += ' A later source assertion.'
        with self.assertRaises(gov.GovernanceError) as caught:run(gov.apply(self.g, request, GROUP))
        self.assertEqual(caught.exception.code, 'stale_snapshot')
        self.assertEqual(self.graph.mutations, [])

    def test_interruption_after_summary_write_replays_without_rebuilding_any_edge(self):
        request, _ = self.reviewed()
        edges = deepcopy(self.graph.edges)
        self.graph.fail_at = 'gov:node-summary'
        with self.assertRaises(RuntimeError):run(gov.apply(self.g, request, GROUP))
        self.assertEqual(self.graph.audits['summary-1']['state'], 'pending')
        self.assertTrue(run(gov.apply(self.g, request, GROUP))['verified'])
        self.assertEqual(self.graph.edges, edges)
        self.assertEqual(self.graph.nodes['n']['props']['governance_ids'], ['summary-1'])

    def test_pending_replay_never_overwrites_unexpected_source_or_property_changes(self):
        request, _ = self.reviewed()
        self.graph.fail_at = 'gov:node-summary'
        with self.assertRaises(RuntimeError):run(gov.apply(self.g, request, GROUP))
        self.graph.edges['e1']['props']['fact'] = 'A new independent fact.'
        with self.assertRaises(gov.GovernanceError) as caught:run(gov.apply(self.g, request, GROUP))
        self.assertEqual(caught.exception.code, 'governance_origin_changed')
        self.assertEqual(self.graph.edges['e1']['props']['fact'], 'A new independent fact.')
        self.assertEqual(self.graph.audits['summary-1']['state'], 'pending')

    def test_verify_detects_lost_original_node_properties_and_edge_vectors(self):
        request, _ = self.reviewed()
        def damage(tag, params):
            if tag == 'gov:node-summary':self.graph.edges['e1']['props']['fact_embedding'] = [9.0]
        self.graph.after = damage
        with self.assertRaises(gov.GovernanceError) as caught:run(gov.apply(self.g, request, GROUP))
        self.assertEqual(caught.exception.code, 'governance_origin_changed')
        self.assertEqual(self.graph.audits['summary-1']['state'], 'pending')

    def test_verify_rejects_changed_original_creation_time(self):
        request, _ = self.reviewed()
        def damage(tag, params):
            if tag == 'gov:node-summary':self.graph.nodes['n']['props']['created_at'] = 'changed'
        self.graph.after = damage
        with self.assertRaises(gov.GovernanceError) as caught:run(gov.apply(self.g, request, GROUP))
        self.assertEqual(caught.exception.code, 'governance_origin_changed')

    def test_too_many_sources_are_rejected_before_partial_review(self):
        for i in range(gov.MAX_SUMMARY_EPISODES):
            self.graph.episode('extra-' + str(i), [], ['n'])
            self.graph.episodes['extra-' + str(i)]['content'] = 'Extra source content.'
        with self.assertRaises(gov.GovernanceError) as caught:run(gov.preview(self.g, self.scope, GROUP))
        self.assertEqual(caught.exception.code, 'governance_too_large')
        self.assertEqual(self.graph.mutations, [])

    def test_missing_source_or_content_prevents_summary_correction(self):
        del self.graph.episodes['p2']
        with self.assertRaises(gov.GovernanceError) as caught:run(gov.preview(self.g, self.scope, GROUP))
        self.assertEqual(caught.exception.code, 'summary_sources_incomplete')
        self.graph.episodes['p2'] = {'uuid': 'p2', 'entity_edges': ['e2']}
        with self.assertRaises(gov.GovernanceError):run(gov.preview(self.g, self.scope, GROUP))
        self.assertEqual(self.graph.mutations, [])

    def test_identity_changes_and_summary_changes_on_other_operations_are_rejected(self):
        for reviews in (None, False, {}, ""):
            with self.subTest(reviews=reviews), self.assertRaises(gov.GovernanceError):
                gov.parse_scope({**self.scope, 'reviewed_episodes': reviews}, bound=False)
        for extra in ({'rename_to': 'different'}, {'retype_to': 'File'}, {'partitions': []}):
            with self.subTest(extra=extra), self.assertRaises(gov.GovernanceError):
                gov.parse_scope({**self.scope, **extra}, bound=False)
        with self.assertRaises(gov.GovernanceError):
            gov.parse_scope({'operation': 'rename', 'node': {'uuid': 'n'}, 'rename_to': 'other',
                             'summary_to': 'not supported'}, bound=False)

    def test_worker_cannot_read_token_or_issue_shared_summary_preview_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'scope.json';p.write_text(json.dumps(self.scope))
            with mock.patch.dict(os.environ, {'KG_HUB_CAMPAIGN_SLOT': '3'}), mock.patch.object(cli.ax, 'api_token') as token:
                for command in ['preview', 'apply']:
                    post = mock.Mock()
                    with self.assertRaisesRegex(SystemExit, 'coordinator review'):
                        cli.main([command, '--file', str(p)], http=post)
                    post.assert_not_called()
                token.assert_not_called()


if __name__ == '__main__':
    unittest.main()
