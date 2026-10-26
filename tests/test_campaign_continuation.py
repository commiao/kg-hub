"""Real receipt locks and v5 reviewed synthetic batches; no model, SSH or graph calls."""
import contextlib
import copy
import json
from pathlib import Path
import sysconfig
import sys
import unittest
from unittest import mock
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import test_campaign_parallel as parallel
import test_campaign_prepare as prepared
from tools import agent_extract as ax, extract_campaign as ec, campaign_supervision as supervision
from tools import campaign_continuation as continuation, campaign_dispatch as dispatch
from tools.campaign_recovery import command_scope
from utils import process_receipts as receipts


class ContinuationTests(unittest.TestCase):
    setUpBase, tearDown = parallel.ParallelCampaignTests.setUp, parallel.ParallelCampaignTests.tearDown

    def setUp(self):
        self.setUpBase()
        self.c['batch'] = 3
        ec.save(self.d, self.c)
        self.stack.enter_context(mock.patch.object(ec, 'split_guard'))
        self.stack.enter_context(mock.patch.object(ax, 'gather_batch', side_effect=self.gather))
        self.out = ec.cmd_claim(self.c['id'], 'm', slot=2, url='http://invalid', token=None, check=lambda r: {})
        self.bid = self.out['batch']
        self.batch = receipts.read(Path(self.out['batch_file']))
        self.results = {'items': [prepared.result_item(i['id']) for i in self.batch['items']]}
        receipts.write(Path(self.out['results_file']), self.results)
        review = self.root / 'review.json'
        ec.cmd_quality_sheet(self.c['id'], self.bid, str(review))
        receipts.write(review, prepared.confirm(receipts.read(review), self.batch))
        ec.cmd_quality_check(self.c['id'], self.bid, str(review))
        ec.cmd_submit(self.c['id'], self.bid, dry_run=True, url='http://invalid', token=None,
                      post=self.post, gate=lambda k: contextlib.nullcontext())
        self.sent = []
        with self.assertRaises(URLError):
            ec.cmd_submit(self.c['id'], self.bid, dry_run=False, url='http://invalid', token=None,
                          post=self.fail_upload, gate=lambda k: contextlib.nullcontext())
        self.worker('m', 2, kind='uncertain_submit', submit=True)
        with ec.locked(self.d):
            c = ec.load(self.d)
            c['dispatch'] = {'state': 'draining'}
            ec.save(self.d, c)
        self.original_receipt = receipts.read(self.d / 'operations' / self.bid / 'latest.json')
        self.original_bytes = (self.d / 'operations' / self.bid / (self.original_receipt['attempt'] + '.json')).read_bytes()
        self.original_log = (self.d / 'log.jsonl').read_bytes()

    def gather(self, after, n, **kwargs):
        picked, _ = prepared.fake_select([{'id': i} for i in range(after + 1, after + n + 1)], {}, n, {})
        return {'picked': picked, 'stats': {}, 'last': after + n, 'lookahead': [], 'seen': n}

    def post(self, method, url, body, token):
        if body.get('dry_run'):
            return 200, {'status': 'dry_run', 'new_entities': body['entities']}
        return 200, {'status': 'ok', 'episode_uuid': 'uuid-' + body['source_obs_id']}

    def fail_upload(self, method, url, body, token):
        self.sent.append(body['source_obs_id'])
        if len(self.sent) == 3:
            raise URLError('timed out')
        return self.post(method, url, body, token)

    def transport_trace(self, response_failure=False):
        path = Path(sysconfig.get_path('stdlib')) / 'http/client.py'
        source = path.read_text().splitlines()
        chunk = next(i for i, line in enumerate(source, 1) if line.strip() == 'self.send(chunk)')
        send = next(i for i, line in enumerate(source, 1) if line.strip() == 'self.sock.sendall(data)')
        return ('Traceback (most recent call last):\n'
                f'  File "{path}", line {chunk}, in {"getresponse" if response_failure else "_send_output"}\n'
                '    self.send(chunk)\n'
                f'  File "{path}", line {send}, in send\n'
                '    self.sock.sendall(data)\nTimeoutError: timed out\n'
                'During handling of the above exception, another exception occurred:\n'
                'urllib.error.URLError: <urlopen error timed out>')

    def worker(self, model, slot, *, kind=None, submit=False, capacity=False):
        root = ec.supervisor_dir(self.d, model, slot)
        root.mkdir(parents=True, exist_ok=True)
        session = f'original-{model}-{slot}'
        supervision.bind_slot_session(self.d, model, slot, session)
        path = root / 'original.events.jsonl'
        events = [{'type': 'thread.started', 'thread_id': session}]
        if submit:
            command = '/bin/zsh -lc \'"${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract" campaign submit --id ' + self.c['id'] + ' --batch ' + self.bid + "'"
            events.extend([{'type': 'item.started', 'item': {'type': 'command_execution', 'id': 'submit1', 'command': command}},
                           {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'submit1',
                            'command': command, 'exit_code': 1, 'aggregated_output': self.transport_trace()}}])
        if capacity:
            error = 'Selected model is at capacity. Please try a different model.'
            events.extend([{'type': 'error', 'message': error}, {'type': 'turn.failed', 'error': {'message': error}}])
        else:
            events.extend([{'type': 'item.completed', 'item': {'type': 'agent_message',
                'text': json.dumps({'recovery_kind': kind or 'none'})}}, {'type': 'turn.completed'}])
        path.write_text(''.join(json.dumps(e) + '\n' for e in events))
        receipts.write(root / 'worker.json', {'session': session, 'events': str(path)})
        with receipts.attempt(root, kind='supervisor') as update:
            update(exit_code=2, phase='coordinator_required')
        with receipts.attempt(root / 'native', kind='native_cli') as update:
            update(exit_code=1 if capacity else 0)
        return root, path

    def readback(self, method, url, body, token):
        return 200, {'items': [{**k, 'status': 'ok' if n < 2 else None,
                               'episode_uuid': 'uuid-' + k['source_obs_id'] if n < 2 else None}
                              for n, k in enumerate(body['items'])]}

    def snapshot(self, keys, anchor):
        return {'version': 'partial-upload-remote-v1', 'keys': keys, 'read_only': True,
            'observation_bodies_returned': False, 'anchor_request_id': 'request-2',
            'executions': [{**k, 'state': 'succeeded', 'finished_at': '2026-10-26T00:00:00Z'} for k in keys[:2]],
            'anchors': [{**keys[1], 'extractor_model': 'm', 'extractor_tool': 'codex',
                         'extractor_session': self.c['id'], 'run_id': self.c['models']['m']['run_id']}],
            'post_anchor_sources': [{**keys[1], 'request_id': 'request-2'}],
            'unparsed_post_anchor_headers': 0,
            'health': {'status': 'ok', 'active_extractions': 0, 'draining': False}}

    def reconcile(self, **kwargs):
        return continuation.reconcile_upload(self.c['id'], self.bid, url='http://invalid', token=None,
            post=kwargs.get('post', self.readback), snapshot=kwargs.get('snapshot', self.snapshot))

    def test_public_partial_reconcile_worker_resume_and_submit_only_remaining(self):
        with self.assertRaises(receipts.Uncertain):
            dispatch.resume(self.c['id'], reason='unsafe')
        self.assertEqual(self.reconcile()['remaining'], 1)
        with self.assertRaisesRegex(ValueError, 'terminal'):
            dispatch.resume(self.c['id'], reason='native not yet reconciled')
        proof = continuation.reconcile_worker(self.c['id'], 'm', 2, reason='human authorized continuation')
        self.assertEqual(proof['kind'], 'partial_upload_reconciled')
        self.assertEqual(dispatch.resume(self.c['id'], reason='safe public continuation')['dispatch']['state'], 'running')
        c = ec.load(self.d)
        self.assertEqual(supervision.completed(c, self.d, 'm'), 2)
        sent, gates = [], []
        def post(*args):
            sent.append(args[2]['source_obs_id'])
            return self.post(*args)
        def gate(kind):
            gates.append(kind)
            return contextlib.nullcontext()
        results = []
        def spawn(args, **kwargs):
            self.assertEqual(args[args.index('resume') + 1], 'original-m-2')
            native = Path(args[args.index('--receipt-dir') + 1])
            with receipts.attempt(native, kind='native_cli') as update:
                results.append(ec.cmd_submit(self.c['id'], self.bid, dry_run=False,
                    url='http://invalid', token=None, post=post, gate=gate))
                from tools.campaign_migration import drain
                drain(self.c['id'], 'synthetic coordinator finishes this in-flight batch')
                for event in [{'type': 'thread.started', 'thread_id': 'original-m-2'}, {'type': 'turn.completed'}]:
                    kwargs['stdout'].write((json.dumps(event) + '\n').encode())
                kwargs['stdout'].flush()
                update(exit_code=0)
            return mock.Mock(poll=lambda: 0)
        run = supervision.run(self.c['id'], 'm', slot=2, poll=.01, sleep=lambda _: None,
                              spawn=spawn, preflight=lambda *a: {'ok': True})
        self.assertEqual(run['state'], 'drained')
        self.assertEqual(sent, [self.batch['items'][2]['source_obs_id']])
        self.assertEqual(gates, ['submit'])
        self.assertEqual(results[0]['by_status'], {'ok': 3})
        self.assertTrue((self.d / 'log.jsonl').read_bytes().startswith(self.original_log))
        self.assertEqual(len((self.d / 'log.jsonl').read_text().splitlines()), 3)
        self.assertEqual(supervision.completed(ec.load(self.d), self.d, 'm'), 3)
        original = self.d / 'operations' / self.bid / (self.original_receipt['attempt'] + '.json')
        self.assertEqual(original.read_bytes(), self.original_bytes)
        self.assertEqual(ec.load(self.d)['quota'], 2000)

    def test_readback_requires_all_keys_exact_uuids_and_absent_remaining(self):
        original = (self.d / 'operations' / self.bid / 'latest.json').read_bytes()
        def missing(*args):
            code, r = self.readback(*args)
            r['items'].pop()
            return code, r
        def existing(*args):
            code, r = self.readback(*args)
            r['items'][-1]['status'] = 'pending'
            return code, r
        def wrong_uuid(*args):
            code, r = self.readback(*args)
            r['items'][0]['episode_uuid'] = 'unrelated'
            return code, r
        for post in [missing, existing, wrong_uuid]:
            with self.subTest(post=post.__name__), self.assertRaises(ValueError):
                self.reconcile(post=post)
            self.assertEqual((self.d / 'operations' / self.bid / 'latest.json').read_bytes(), original)

    def test_remote_requires_no_pending_accepted_or_foreign_requests(self):
        def mutate(field):
            def snapshot(*args):
                value = self.snapshot(*args)
                field(value)
                return value
            return snapshot
        changes = [lambda v: v['executions'][0].update(state='running'),
                   lambda v: v['executions'].append({**v['keys'][-1], 'state': 'succeeded', 'finished_at': 't'}),
                   lambda v: v['health'].update(active_extractions=1),
                   lambda v: v['anchors'][0].update(run_id='another-campaign'),
                   lambda v: v['post_anchor_sources'].append(v['keys'][-1]),
                   lambda v: v.update(unparsed_post_anchor_headers=1),
                   lambda v: v.update(anchors=[])]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.reconcile(snapshot=mutate(change))

    def test_response_timeout_and_changed_input_cannot_authorize_replay(self):
        root = ec.supervisor_dir(self.d, 'm', 2)
        path = Path(receipts.read(root / 'worker.json')['events'])
        old = path.read_text()
        rows = [json.loads(line) for line in old.splitlines()]
        for row in rows:
            if row.get('type') == 'item.completed' and row.get('item', {}).get('type') == 'command_execution':
                row['item']['aggregated_output'] = self.transport_trace(True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        with self.assertRaisesRegex(ValueError, 'incomplete JSON'):
            self.reconcile()
        path.write_text(old)
        results = copy.deepcopy(self.results)
        results['items'][-1]['entities'][0]['summary'] += ' altered'
        receipts.write(Path(self.out['results_file']), results)
        with self.assertRaisesRegex(ValueError, 'failed upload'):
            self.reconcile()

    def test_live_native_and_altered_continuation_proof_stay_blocked(self):
        root = ec.supervisor_dir(self.d, 'm', 2)
        with receipts.attempt(root / 'native', kind='native_cli'):
            with self.assertRaisesRegex(ValueError, 'terminal'):
                self.reconcile()
        self.reconcile()
        continuation.reconcile_worker(self.c['id'], 'm', 2, reason='explicit')
        ref = receipts.read(root / 'continuation-active.json')
        p = Path(ref['path'])
        p.write_text(p.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'terminal'):
            dispatch.resume(self.c['id'], reason='tampered')

    def capacity(self):
        out = ec.cmd_claim(self.c['id'], 'n', slot=1, url='http://invalid', token=None, check=lambda r: {})
        c = ec.load(self.d)
        b = ec.find_batch(c, out['batch'])
        batch = receipts.read(Path(out['batch_file']))
        root, events = self.worker('n', 1, capacity=True)
        directory = self.d / 'operations' / b['id']
        with self.assertRaises(SystemExit), receipts.attempt(directory, kind='submit',
                identity={'campaign': c['id'], 'batch': b['id'], **ec.batch_identity(c, b)}) as update:
            update(phase='waiting_nas', may_have_written=False,
                source_keys=[{k: row[k] for k in ('source_description', 'source_obs_id')} for row in batch['items']])
            raise SystemExit(1)
        ec.cmd_reconcile_operation(c['id'], b['id'], url='http://invalid', token=None,
                                  check=lambda rows, *args: {row['source_obs_id']: None for row in rows})
        return root, events

    def test_capacity_requires_exact_transient_error_and_fresh_same_model_probe(self):
        # Finish the partial worker's receipt checks first; n remains independently gated.
        self.reconcile()
        continuation.reconcile_worker(self.c['id'], 'm', 2, reason='explicit')
        c = ec.load(self.d)
        c['dispatch']['state'] = 'running'
        ec.save(self.d, c)
        root, events = self.capacity()
        c = ec.load(self.d)
        c['dispatch']['state'] = 'draining'
        ec.save(self.d, c)
        with mock.patch.object(ec, 'model_readiness', side_effect=ValueError('stale actual probe')):
            with self.assertRaisesRegex(ValueError, 'stale'):
                continuation.reconcile_worker(self.c['id'], 'n', 1, reason='explicit', model_preflight='probe.json')
        with mock.patch.object(ec, 'model_readiness', return_value={'n': {'receipt_sha256': 'synthetic'}}) as probe:
            recovery = continuation.reconcile_worker(self.c['id'], 'n', 1, reason='explicit', model_preflight='probe.json')
            self.assertEqual(recovery['kind'], 'capacity_recovered')
            probe.assert_called_once_with(['n'], 'codex', ['probe.json'], effort='medium', proxy_mode='auto')
            self.assertEqual(dispatch.resume(self.c['id'], reason='verified capacity restored')['dispatch']['state'], 'running')
        original = events.read_text()
        events.write_text(original.replace('Selected model is at capacity.', 'Permission denied.'))
        with self.assertRaisesRegex(ValueError, 'exact transient'):
            continuation.reconcile_worker(self.c['id'], 'n', 1, reason='must not bypass', model_preflight='probe.json')
        events.write_text(original)
        self.assertEqual(receipts.read(root / 'native/latest.json')['exit_code'], 1)
        spawns = []
        def continue_same_session(args, **kwargs):
            self.assertEqual(args[args.index('resume') + 1], 'original-n-1')
            native = Path(args[args.index('--receipt-dir') + 1])
            with receipts.attempt(native, kind='native_cli') as update:
                spawns.append(args)
                from tools.campaign_migration import drain
                drain(self.c['id'], 'synthetic coordinator freezes after capacity recovery')
                for event in [{'type': 'thread.started', 'thread_id': 'original-n-1'}, {'type': 'turn.completed'}]:
                    kwargs['stdout'].write((json.dumps(event) + '\n').encode())
                kwargs['stdout'].flush()
                update(exit_code=0)
            return mock.Mock(poll=lambda: 0)
        run = supervision.run(self.c['id'], 'n', slot=1, poll=.01, sleep=lambda _: None,
                              spawn=continue_same_session, preflight=lambda *a: {'ok': True})
        self.assertEqual(run['state'], 'drained')
        self.assertEqual(len(spawns), 1)

    def test_stable_public_expression_does_not_evaluate_arbitrary_shell(self):
        command = '\"${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract\" campaign submit --id a --batch b'
        self.assertEqual(command_scope(command)['batch'], 'b')
        with self.assertRaises(ValueError):
            command_scope(command + '; touch /tmp/not-executed')
        self.assertIsNone(command_scope(command.replace('${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}', '$(other)')))


if __name__ == '__main__':
    unittest.main()
