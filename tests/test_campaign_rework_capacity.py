"""Capacity rework uses the original identity without clearing failed writes."""
import ast
import json
import shlex
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_campaign_rework_single import SingleReworkTests
from tools import campaign_rework_capacity as capacity, campaign_supervision as supervision
from utils import process_receipts as receipts


class CapacityTests(SingleReworkTests):
    def fail_capacity(self, tools=()):
        op = self.d / 'operations' / self.bid
        if not (op / 'latest.json').exists():
            with receipts.attempt(op, kind='submit') as update:
                update(exit_code=0, may_have_written=False)
        def spawn(args, **kwargs):
            rows = [{'type': 'thread.started', 'thread_id': self.sid}]
            for key, command, code, output in tools:
                item = {'id': key, 'type': 'command_execution', 'command': command}
                rows += [{'type': 'item.started', 'item': item}, {'type': 'item.completed',
                    'item': {**item, 'exit_code': code, 'status': 'completed' if code == 0 else 'failed', 'aggregated_output': output}}]
            rows += [{'type': 'error', 'message': supervision.CAPACITY_MESSAGE},
                     {'type': 'turn.failed', 'error': {'message': supervision.CAPACITY_MESSAGE}}]
            with receipts.attempt(self.worker / 'native', kind='native_cli') as update:
                kwargs['stdout'].write(('\n'.join(map(json.dumps, rows)) + '\n').encode())
                kwargs['stdout'].flush()
                update(exit_code=1, child_pid=12345)
            return mock.Mock(wait=lambda: 1)
        with mock.patch.object(self, 'spawn', spawn):
            result = self.run_rework()
        self.assertEqual(result['state'], 'blocked')
        return Path(result['evidence_dir'])

    def retry(self, **kwargs):
        with mock.patch.object(supervision, 'CAPACITY_WAIT_SECONDS', 0):
            return self.run_rework(retry_capacity=True, **kwargs)

    def test_explicit_capacity_retry_preserves_failed_attempt_and_scope(self):
        job = self.fail_capacity([('c', 'echo diagnostic', 0, 'diagnostic')])
        old = {p: p.read_bytes() for p in job.iterdir() if p.is_file()}
        failed_native = (self.worker / 'native/latest.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'clean idle'):
            self.run_rework()
        out = self.retry()
        self.assertEqual(out['state'], 'rework_turn_completed')
        self.assertEqual((Path(out['evidence_dir']) / 'previous-native.json').read_bytes(), failed_native)
        proof = receipts.read(Path(out['evidence_dir']) / 'capacity-retry.json')
        for ref in [*proof['evidence'].values(), proof['failed_native'], proof['failed_worker']]:
            self.assertEqual(capacity._ref(ref['path']), ref)
        for p, data in old.items():self.assertEqual(p.read_bytes(), data)
        self.assertEqual(receipts.read(self.worker / 'worker.json')['capacity_retries'], 1)

    def test_backoff_and_retry_budget(self):
        self.fail_capacity()
        with self.assertRaisesRegex(ValueError, '300-second'):
            self.run_rework(retry_capacity=True)
        w = receipts.read(self.worker / 'worker.json');w['capacity_retries'] = 3
        receipts.write(self.worker / 'worker.json', w)
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):self.retry()

    def test_reject_failed_remote_tool_and_real_approval(self):
        for code, output in [(1, 'remote write timeout'), (75, 'NAS unavailable'),
                             (1, 'This action was rejected due to unacceptable risk')]:
            with self.subTest(code=code, output=output):
                job = self.fail_capacity([('c', 'repair apply --file request.json', code, output)])
                with self.assertRaises(ValueError):self.retry()
                # Restore only the synthetic clean starting fixture between cases.
                receipts.write(self.worker / 'worker.json', receipts.read(job / 'previous-worker.json'))
                receipts.write(self.worker / 'native/latest.json', receipts.read(job / 'previous-native.json'))
        self.assertFalse(self.spawned)

    def test_pending_commands_other_provider_and_wrong_owner_rejected(self):
        job = self.fail_capacity()
        path = job / 'events.jsonl';original = path.read_text()
        candidates = [original.replace(self.sid, 'other'),
            original.replace(supervision.CAPACITY_MESSAGE, 'HTTP 429'),
            original.replace('{"type": "error"', '{"type":"item.started","item":{"id":"x","type":"command_execution","command":"repair apply"}}\n{"type": "error"')]
        for text in candidates:
            path.write_text(text)
            with self.assertRaises(ValueError):self.retry()
        path.write_text(original)
        with self.assertRaisesRegex(ValueError, 'combine'):self.retry(retry_startup=True)
        self.assertFalse(self.spawned)

    def test_changed_evidence_during_service_preflight_rejected(self):
        job = self.fail_capacity()
        def flight(*args):
            p=job/'scope.json';v=receipts.read(p);v['session']='other';receipts.write(p,v)
            return {'ok': True}
        with mock.patch.object(supervision, 'CAPACITY_WAIT_SECONDS', 0):
            from tools import campaign_rework
            with self.assertRaises(ValueError):
                campaign_rework.run(self.c['id'], 'm', 1, batches=[self.bid], assignment=self.assignment,
                    reason='review', retry_capacity=True, spawn=self.spawn, preflight=flight)
        self.assertFalse(self.spawned)

    def test_unknown_formal_operation_and_unverified_repair_are_rejected(self):
        self.fail_capacity()
        path=self.d/'operations'/self.bid/'latest.json';old=path.read_bytes()
        value=receipts.read(path);value['may_have_written']=True;receipts.write(path,value)
        with self.assertRaisesRegex(ValueError,'known terminal'):self.retry()
        path.write_bytes(old)
        audit=self.d/'private-rework'/self.bid;audit.mkdir(parents=True)
        from tools import extract_campaign as ec
        oid=ec.find_batch(ec.load(self.d),self.bid)['ids'][0]
        receipts.write(audit/f'{oid}-apply.json',{'http':200,'verified':True,'repair_id':'synthetic'})
        with self.assertRaisesRegex(ValueError,'matching applied'):self.retry()
        status={'http':200,'repair':{'state':'applied','repair_id':'synthetic','result':{'verified':True},
                                     'request':{'source_description':'synthetic','source_obs_id':'foreign'}}}
        receipts.write(audit/f'{oid}-status.json',status)
        with self.assertRaisesRegex(ValueError,'matching applied'):self.retry()
        status['repair']['request']['source_obs_id']=str(oid)
        receipts.write(audit/f'{oid}-status.json',status)
        self.assertEqual(self.retry()['state'],'rework_turn_completed')
        proof=receipts.read(Path(receipts.read(self.worker/'worker.json')['events']).parent/'capacity-retry.json')
        receipts.write(audit/f'{oid}-status.json',{'new_repair':'later'})
        for ref in proof['evidence'].values():self.assertEqual(capacity._ref(ref['path']),ref)


class LocalFailureTests(unittest.TestCase):
    def test_syntax_error_has_no_executed_body(self):
        item={'command': '/bin/zsh -lc '+shlex.quote("python3 - <<'PY'\nx = '\nPY"), 'exit_code':1,
              'aggregated_output':'  File "<stdin>", line 1\n    x = \'\nSyntaxError: unterminated string literal\n'}
        self.assertEqual(capacity.local_failure(item, {}), 'python_body_did_not_compile')
        item['command']="python3 - <<'PY'\nraise RuntimeError()\nPY"
        with self.assertRaises(ValueError):capacity.local_failure(item,{})

    def test_exact_prewrite_assertion_and_mutations(self):
        audit='/private/tmp/synthetic-audit';ids=[1,2]
        cli=str(Path(capacity.__file__).with_name('extraction_repair.py'))
        text=capacity.PRECHECK_WRAPPER.replace('AUDIT_PATH',repr(audit)).replace('REPAIR_CLI',repr(cli)).replace('SOURCE_IDS',repr(ids))
        line=8  # The failed generator is before the first subprocess, not the prior length check.
        output=f'Traceback (most recent call last):\n  File "<stdin>", line {line}, in <module>\n  File "<stdin>", line {line}, in <genexpr>\nKeyError: \'props\'\n'
        scopes={audit:set(ids)}
        self.assertTrue(capacity.prewrite_failure(text,output,scopes))
        for changed, log in [(text,'1 apply 0\n'+output),
                (text.replace('print(n,label,r.returncode,flush=True)','pass'),output),
                (text.replace("'apply'","'delete'"),output),
                ('import os; os.system("touch /tmp/no")\n'+text,output)]:
            with self.subTest(changed=changed[:50]):
                self.assertFalse(capacity.prewrite_failure(changed,log,scopes))
        self.assertFalse(capacity.prewrite_failure(text,output,{audit:{1}}))


if __name__ == '__main__':unittest.main()
