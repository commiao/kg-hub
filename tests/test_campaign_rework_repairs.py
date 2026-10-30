"""Synthetic explicit repair evidence; status reads never mutate the graph."""
import copy
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import campaign_rework_repairs as repairs
from tools.campaign_rework_evidence import digest


class RepairReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.audit = Path(self.tmp.name).resolve()
        self.request = {'repair_id': 'synthetic-1', 'source_description': 'synthetic source',
            'source_obs_id': 'source-1', 'expected_snapshot_sha256': 'a'*64,
            'reason': ' source-backed correction ', 'reviewer': ' reviewer ',
            'remove_edge_uuids': ['edge-z', 'edge-a'], 'replacement': {
                'source_description': 'synthetic source', 'source_obs_id': 'source-1',
                'extractor_model': 'model', 'extractor_tool': 'executor', 'run_id': 'run'}}
        self.applied = {'http': 200, 'duplicate': False, 'status': 'repaired',
                        'repair_id': 'synthetic-1', 'verified': True, 'episode_uuid': 'episode-1'}
        self.inventory = [{'item': 'item-1', 'source_id': 101}]
        self.save()
        tail = (f'{repairs.REPAIR_CLI} apply --file {self.audit}/101.reviewed-request.json '
                f'--out {self.audit}/101.apply.json')
        command = shlex.join(['/bin/zsh', '-lc', repairs.PYTHON_WRAPPER + '; ' + tail])
        start = {'id': 'item-1', 'type': 'command_execution', 'command': command}
        end = {**start, 'status': 'completed', 'exit_code': 0, 'aggregated_output': json.dumps(self.applied)}
        self.rows = [{'type': 'item.started', 'item': start}, {'type': 'item.completed', 'item': end}]
        request = repairs.parse_request(self.request)
        self.body = {'status': 'ok', 'repair': {'repair_id': 'synthetic-1', 'state': 'applied',
            'request_sha256': repairs.digest(request), 'request': request,
            'result': {k: v for k, v in self.applied.items() if k not in ('http', 'duplicate')}}}
        self.calls = []
    def save(self):
        for suffix, obj, field in [('reviewed-request', self.request, 'request_sha256'),
                                   ('apply', self.applied, 'apply_sha256')]:
            raw = json.dumps(obj).encode(); (self.audit/f'101.{suffix}.json').write_bytes(raw)
            self.inventory[0][field] = digest(raw)
    def status(self, rid):
        self.calls.append(rid); return 200, copy.deepcopy(self.body)
    def run_check(self, **overrides):
        kwargs = dict(expected_items={'item-1'}, audit=self.audit,
            source_keys={101: ('synthetic source', 'source-1')}, model='model', executor='executor',
            run_id='run', status_get=self.status)
        kwargs.update(overrides)
        return repairs.reconcile(self.rows, self.inventory, **kwargs)
    def test_canonical_sorted_request_and_fresh_read_no_authorization(self):
        for separator in ('; ', '\n'):
            args = shlex.split(self.rows[0]['item']['command'])
            args[2] = args[2].replace(repairs.PYTHON_WRAPPER+'; ', repairs.PYTHON_WRAPPER+separator)
            for row in self.rows: row['item']['command'] = shlex.join(args)
            result = self.run_check()
            self.assertEqual(len(result['explicit_repairs']), 1)
            for flag in ('restart_allowed', 'full_command_effects_validated', 'semantic_acceptance',
                         'live_graph_readback_validated'):
                self.assertIs(result[flag], False)
        self.assertEqual(self.calls, ['synthetic-1', 'synthetic-1'])
    def test_original_terminal_command_and_inventory_reject_before_network(self):
        original = copy.deepcopy(self.rows); inventory = copy.deepcopy(self.inventory)
        mutations = [
            lambda: self.rows.pop(),
            lambda: self.rows.reverse(),
            lambda: self.rows.append(copy.deepcopy(self.rows[-1])),
            lambda: self.rows[-1]['item'].update(exit_code=-1),
            lambda: self.rows[-1]['item'].update(exit_code=False),
            lambda: self.rows[-1]['item'].update(status='failed'),
            lambda: self.rows[-1]['item'].update(aggregated_output='{}'),
            lambda: self.rows[-1]['item'].update(command='other'),
            lambda: self.inventory[0].update(source_id=999),
            lambda: self.inventory[0].update(request_sha256='0'*64),
            lambda: self.inventory[0].update(apply_sha256='0'*64),
            lambda: self.inventory[0].update(extra=True),
            lambda: self.inventory[0].update(item='item-other')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.rows = copy.deepcopy(original); self.inventory = copy.deepcopy(inventory); mutate()
                with self.assertRaises((ValueError, KeyError)): self.run_check()
                self.assertEqual(self.calls, [])
        self.rows = copy.deepcopy(original); self.inventory = copy.deepcopy(inventory)
        for suffix in ('; echo extra', ' && true', ' --extra', '\nexit 0'):
            for row in self.rows:
                args = shlex.split(original[0]['item']['command']); args[2] += suffix
                row['item']['command'] = shlex.join(args)
            with self.assertRaises(ValueError): self.run_check()
        self.assertEqual(self.calls, [])
    def test_local_request_provenance_and_receipt_binding(self):
        original = copy.deepcopy(self.request)
        for field in ('source_description', 'source_obs_id', 'extractor_model', 'extractor_tool', 'run_id'):
            self.request = copy.deepcopy(original); self.request['replacement'][field] = 'foreign'; self.save()
            with self.subTest(field=field), self.assertRaises(ValueError): self.run_check()
        self.request = copy.deepcopy(original); self.request['source_obs_id'] = 'foreign'; self.save()
        with self.assertRaises(ValueError): self.run_check()
        self.assertEqual(self.calls, [])
    def test_fresh_server_status_request_and_result_must_all_match(self):
        original = copy.deepcopy(self.body)
        mutations = [lambda b: b.update(status='error'),
            lambda b: b['repair'].update(state='prepared'),
            lambda b: b['repair'].update(repair_id='other'),
            lambda b: b['repair'].update(request_sha256='0'*64),
            lambda b: b['repair']['request'].update(reason='different reason'),
            lambda b: b['repair']['request'].update(source_obs_id='other'),
            lambda b: b['repair']['result'].update(verified=False),
            lambda b: b['repair']['result'].update(episode_uuid='other'),
            lambda b: b['repair']['result'].update(verified=1)]
        for mutate in mutations:
            self.body = copy.deepcopy(original); mutate(self.body)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError): self.run_check()
        self.body = original
        for code in (404, 500, True):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.run_check(status_get=lambda rid: (code, self.body))
    def test_public_transport_uses_only_get_and_validates_id(self):
        calls = []
        def http(*args): calls.append(args); return 200, {'status': 'ok'}
        with patch('tools.agent_extract.api_token', return_value='synthetic-token'):
            self.assertEqual(repairs.read_status('repair-1', 'https://example.invalid/', http),
                             (200, {'status': 'ok'}))
            with self.assertRaises(ValueError): repairs.read_status('../other', 'https://example.invalid', http)
        self.assertEqual(calls, [('GET', 'https://example.invalid/api/extraction/repair?repair_id=repair-1',
                                 None, 'synthetic-token')])


if __name__ == '__main__': unittest.main()
