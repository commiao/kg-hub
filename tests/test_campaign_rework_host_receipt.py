"""Synthetic public fixtures; never execute transcript commands."""
import copy
import json
import shlex
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import campaign_rework_host_receipt as h


def fixture():
    scope = {'campaign_root':'/tmp/campaign', 'audit_relative':'audit/review',
             'batch':'model-001', 'source_ids':[101,102,103], 'session':'session-a',
             'turn':'turn-a', 'cwd':'/tmp/excluded-cwd',
             'native_started_at':'2026-01-01T00:00:00+00:00',
             'native_ended_at':'2026-01-01T00:00:10+00:00'}
    py = h.PY_TEMPLATE.replace('ROOT',repr(scope['campaign_root'])).replace(
        'AUDIT',repr(scope['audit_relative'])).replace('BATCH',repr('batches/model-001.json')).replace('IDS','(101, 102)')
    shell = h.SHELL_TEMPLATE.replace('IDS','101 102').replace('AUDIT','/tmp/campaign/audit/review')
    script = "python3 - <<'PY'\n" + py + '\nPY\n' + shell
    argv = ['/bin/zsh','-lc',script]
    native = [{'type':'thread.started','thread_id':'session-a'},
              {'type':'item.started','item':{'id':'failed-tool','type':'command_execution','command':'false'}},
              {'type':'item.completed','item':{'id':'failed-tool','type':'command_execution','command':'false','exit_code':1,'status':'failed'}},
              {'type':'item.started','item':{'id':'pending-tool','type':'command_execution','command':shlex.join(argv)}},
              {'type':'turn.failed','error':{'message':h.CAPACITY}}]
    origin={'turn_id':'turn-a'}
    call={'timestamp':'2026-01-01T00:00:01+00:00','type':'response_item',
          'payload':{'type':'custom_tool_call','name':'exec','call_id':'call-a',
          'internal_chat_message_metadata_passthrough':origin.copy(),
          'input':'const r=await tools.exec_command({cmd:'+json.dumps(script)+',"workdir":'+json.dumps(scope['cwd'])+',"yield_time_ms":1000,"max_output_tokens":500});text(r)\n'}}
    output={'timestamp':'2026-01-01T00:00:03+00:00','type':'response_item',
            'payload':{'type':'custom_tool_call_output','call_id':'call-a',
            'internal_chat_message_metadata_passthrough':origin.copy(),
            'output':[{'text':json.dumps({'session_id':123,'output':''})}]}}
    terminal={'timestamp':'2026-01-01T00:00:04+00:00','type':'event_msg',
              'payload':{'type':'task_complete','turn_id':'turn-a',
                         'error':{'message':h.CAPACITY,'codex_error_info':'server_overloaded'}}}
    completed={'timestamp':'2026-01-01T00:00:05+00:00','type':'event_msg',
               'payload':{'type':'item_completed','thread_id':'session-a','turn_id':'turn-a',
               'started_at_ms':int(h.milliseconds('2026-01-01T00:00:02+00:00')),
               'completed_at_ms':int(h.milliseconds('2026-01-01T00:00:05+00:00')),
               'item':{'type':'CommandExecution','id':'exec-12345678-1234-1234-1234-123456789abc',
                       'process_id':'123','command':argv,'cwd':'file:///tmp/excluded-cwd',
                       'source':'unified_exec_startup','status':'failed','exit_code':-1,
                       'aggregated_output':'','duration':{'secs':3,'nanos':0}}}}
    return {'scope':scope,'native_rows':native,'host_rows':[call,output,terminal,completed]}


def classify(f):
    return h.inspect(f['native_rows'],f['host_rows'],f['scope'])


def mutate_script(f, transform):
    argv=f['host_rows'][-1]['payload']['item']['command']
    original=argv[2];argv[2]=transform(original)
    f['host_rows'][0]['payload']['input']=f['host_rows'][0]['payload']['input'].replace(json.dumps(original),json.dumps(argv[2]))
    f['native_rows'][-2]['item']['command']=shlex.join(argv)


class HostReceiptTests(unittest.TestCase):
    def test_evidence_preserves_failure_and_does_not_authorize(self):
        f=fixture();before=copy.deepcopy(f);r=classify(f)
        self.assertEqual(f,before)
        self.assertEqual(r['host_exit_code'],-1)
        self.assertEqual(r['source_ids'],[101,102])
        self.assertEqual(r['other_failed_commands_not_waived'],['failed-tool'])
        for key in ('normal_os_exit_proven','preview_success_proven','all_recovery_gates_validated','restart_allowed'):
            self.assertIs(r[key],False)

    def test_untrusted_host_mutations(self):
        cases=[('thread_id','foreign'),('turn_id','foreign'),('started_at_ms',0),('completed_at_ms',0)]
        for key,value in cases:
            f=fixture();f['host_rows'][-1]['payload'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):classify(f)
        cases=[('process_id','456'),('cwd','file:///tmp/foreign'),('source','invented'),
               ('status','completed'),('exit_code',0),('exit_code',1),('exit_code',True),
               ('aggregated_output','unexpected'),('id','bad'),('duration',{'secs':99,'nanos':0})]
        for key,value in cases:
            f=fixture();f['host_rows'][-1]['payload']['item'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):classify(f)

    def test_pairing_and_temporal_failures(self):
        for index in range(4):
            for action in ('remove','duplicate'):
                f=fixture()
                if action=='remove':f['host_rows'].pop(index)
                else:f['host_rows'].append(copy.deepcopy(f['host_rows'][index]))
                with self.subTest(index=index,action=action),self.assertRaises(ValueError):classify(f)
        for index in (0,1):
            f=fixture();f['host_rows'][index]['payload']['internal_chat_message_metadata_passthrough']['turn_id']='foreign'
            with self.assertRaises(ValueError):classify(f)
        for timestamp in ('2026-01-01T00:00:11+00:00','2026-01-01T00:00:05'):
            f=fixture();f['host_rows'][-1]['timestamp']=timestamp
            with self.assertRaises(ValueError):classify(f)
        f=fixture();f['host_rows'][2]['payload']['error']['message']='NAS refused'
        with self.assertRaises(ValueError):classify(f)
        f=fixture();f['native_rows'].append({'type':'item.started'})
        with self.assertRaises(ValueError):classify(f)
        f=fixture();f['native_rows'].insert(-1,{'type':'item.started','item':{'id':'extra','type':'command_execution','command':'unknown'}})
        with self.assertRaises(ValueError):classify(f)

    def test_consistent_command_mutation_still_rejected(self):
        transforms=[lambda s:s.replace(' preview --file ',' apply --file '),
                    lambda s:s+'; touch /tmp/unexpected',
                    lambda s:s.replace('import json,pathlib','import json,pathlib,subprocess'),
                    lambda s:s.replace("f'{n}.selector.json'","f'../../{n}.selector.json'"),
                    lambda s:s.replace('model-001.json','other.json'),
                    lambda s:s.replace('102','999'),
                    lambda s:s.replace('102','101'),
                    lambda s:s.replace('(101,','(True,'),
                    lambda s:s.replace('tools/extraction_repair.py','tools/other.py'),
                    lambda s:s.replace('$n.live-preview.json','../../$n.live-preview.json')]
        for i,transform in enumerate(transforms):
            f=fixture();mutate_script(f,transform)
            with self.subTest(case=i),self.assertRaises(ValueError):classify(f)
        f=fixture();f['scope']['audit_relative']='audit/../../elsewhere'
        with self.assertRaises(ValueError):classify(f)

if __name__=='__main__':unittest.main()
