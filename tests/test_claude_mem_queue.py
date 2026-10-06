"""Queue telemetry must not turn unavailable/restarted workers into digestion."""
import json
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.claude_mem_queue import collect, reconciliation_count
from flow_dashboard import claude_mem_trends, _HTML

NOW=1790863200.0

class QueueTests(unittest.TestCase):
    def test_live_missing_persistence_and_no_duplicate_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            def http(url,**kw):
                if ':37701/' in url:return None,'offline'
                return ({'pid':42} if url.endswith('health') else {'queueDepth':20}),None
            a=collect(home,http,NOW)
            self.assertIsNone(a['current'][0]['depth'])
            self.assertEqual(a['current'][1]['depth'],20)
            b=collect(home,http,NOW+60)
            self.assertEqual(len(b['history']),2)
            self.assertTrue((home/'.kg-hub/state/claude-mem-queue.sqlite3').exists())

    def test_incremental_log_import_does_not_replace_live_or_read_partial_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);logs=home/'.claude-mem/logs';logs.mkdir(parents=True)
            stamp=datetime.fromtimestamp(NOW-3600).strftime('%Y-%m-%d %H:%M:%S.000')
            line=f'[{stamp}] [INFO ] [WORKER] Broadcasting processing status {{queueDepth=99}}\n'
            log=logs/('claude-mem-'+datetime.fromtimestamp(NOW,timezone.utc).strftime('%Y-%m-%d')+'.log')
            log.write_text(line[:-1])
            http=lambda url,**kw: ({'pid':4} if url.endswith('health') else {'queueDepth':0},None)
            a=collect(home,http,NOW)
            self.assertEqual(len(a['history']),2)
            with log.open('a') as f:f.write('\n')
            b=collect(home,http,NOW+10)
            self.assertEqual(len(b['history']),3)
            self.assertEqual(sum(p['depth']==99 for p in b['history']),1)
            c=collect(home,http,NOW+20)
            self.assertEqual(len(c['history']),3)

    def test_rates_require_contiguous_live_same_process(self):
        def point(at,depth,pid='1',source='live'):
            return dict(worker='current',at=at,depth=depth,pid=pid,source=source)
        hist=[point(NOW-14400,100),point(NOW-10800,90),point(NOW-7200,1,pid='2'),
              point(NOW-3600,None,pid='2'),point(NOW,0,pid='2')]
        data=claude_mem_trends([dict(host='mac',claude_mem_queue=dict(sampled_at=NOW,history=hist))],datetime.fromtimestamp(NOW,timezone.utc))[0]
        self.assertEqual([r['current_rate'] for r in data['rows']],[None,10,None,None,None])
        self.assertTrue(all(r['total'] is None for r in data['rows']))
        self.assertFalse(data['stale'])

    def test_missing_and_negative_rates_and_staleness(self):
        hist=[dict(worker='legacy',at=NOW-3600,depth=0,pid='1',source='live'),
              dict(worker='legacy',at=NOW,depth=10,pid='1',source='live')]
        row=claude_mem_trends([dict(_snapshot_stale=True,claude_mem_queue=dict(sampled_at=NOW,history=hist))],datetime.fromtimestamp(NOW,timezone.utc))[0]
        self.assertEqual(row['rows'][-1]['legacy_rate'],-10)
        self.assertTrue(row['stale'])
        self.assertNotIn('claude-mem · 压缩队列积压',_HTML)
        self.assertIn('kg-hub · 入图积压消化',_HTML)

class HeldQueueTests(unittest.TestCase):
    def test_current_states_not_steps_or_historical_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'worker.db'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE observer_tasks (id TEXT PRIMARY KEY, state TEXT)')
                db.executemany('INSERT INTO observer_tasks VALUES (?,?)',
                               [('a','reconciliation'),('b','reconciliation'),('c','succeeded'),('d','queued')])
                db.execute('CREATE TABLE legacy_queue_evidence (disposition TEXT)')
                db.execute("INSERT INTO legacy_queue_evidence VALUES ('needs_manual_reconciliation')")
            self.assertEqual(reconciliation_count(path),(2,None))
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE observer_tasks SET state='succeeded' WHERE state='reconciliation'")
            self.assertEqual(reconciliation_count(path),(0,None))
            self.assertIsNone(reconciliation_count(path.with_name('missing.db'))[0])
            self.assertFalse(path.with_name('missing.db').exists())

    def test_old_history_migration_leaves_missing_held_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);state=home/'.kg-hub/state/claude-mem-queue.sqlite3'
            state.parent.mkdir(parents=True)
            with closing(sqlite3.connect(state)) as db, db:
                db.execute('CREATE TABLE samples (worker TEXT,bucket INTEGER,at REAL,depth INTEGER,pid TEXT,source TEXT,PRIMARY KEY(worker,bucket))')
                db.execute('INSERT INTO samples VALUES (?,?,?,?,?,?)',('current',int(NOW-3600)//3600,NOW-3600,22,'42','live'))
            source=home/'.claude-mem-next/claude-mem.db';source.parent.mkdir()
            with closing(sqlite3.connect(source)) as db, db:
                db.execute('CREATE TABLE observer_tasks (state TEXT)')
                db.executemany('INSERT INTO observer_tasks VALUES (?)',[('reconciliation',),('running',)])
            http=lambda url,**kw: ({'pid':42} if url.endswith('health') else {'queueDepth':20},None)
            a=collect(home,http,NOW)
            self.assertIsNone(a['history'][0]['held'])
            self.assertEqual(a['current'][1]['held'],1)
            self.assertIsNone(a['current'][0]['held'])
            b=collect(home,http,NOW+60)
            self.assertEqual(len(b['history']),3)
            trend=claude_mem_trends([dict(claude_mem_queue=b)],datetime.fromtimestamp(NOW+60,timezone.utc))[0]
            self.assertIsNone(trend['rows'][0]['current_held'])
            self.assertEqual(trend['rows'][-1]['current_held'],1)

    def test_invalid_held_is_not_zero(self):
        history=[dict(worker='current',at=NOW-i*3600,depth=20,held=v) for i,v in enumerate([0,None,-1,True,'2'])]
        trend=claude_mem_trends([dict(claude_mem_queue=dict(sampled_at=NOW,history=history))],datetime.fromtimestamp(NOW,timezone.utc))[0]
        self.assertEqual([r['current_held'] for r in trend['rows']],[None,None,None,None,0])

class TrendRangeTests(unittest.TestCase):
    def build(self, history, **kw):
        return claude_mem_trends([dict(claude_mem_queue=dict(sampled_at=NOW,history=history), **kw)],datetime.fromtimestamp(NOW,timezone.utc))[0]

    def point(self,hours,depth,pid='1',source='live'):
        return dict(worker='current',at=NOW-hours*3600,depth=depth,pid=pid,source=source)

    def test_rate_axis_does_not_include_week_of_null_log_rates(self):
        h=[self.point(144,500,source='log'),self.point(3,100),self.point(2,90),self.point(1,None),self.point(0,80)]
        data=self.build(h)
        self.assertEqual(len(data['rows']),5)
        self.assertEqual(len(data['rate_rows']),3)
        self.assertEqual(data['rate_rows'][0]['current_rate'],10)
        self.assertIsNone(data['rate_rows'][1]['current_rate'])
        self.assertEqual(data['unchanged_hours']['current'],0)

    def test_flat_live_queue_is_visible_but_gaps_restart_and_empty_are_not_stalls(self):
        data=self.build([self.point(5,50),self.point(4,50),self.point(3,50),self.point(2,50),self.point(1,50),self.point(0,50)])
        self.assertEqual(data['unchanged_hours']['current'],5)
        self.assertEqual(self.build([self.point(5,50),self.point(0,50)])['unchanged_hours']['current'],0)
        self.assertEqual(self.build([self.point(1,50),self.point(0,50,pid='2')])['unchanged_hours']['current'],0)
        self.assertEqual(self.build([self.point(1,0),self.point(0,0)])['unchanged_hours']['current'],0)

    def test_no_valid_rate_means_empty_chart_not_zero_filled_week(self):
        data=self.build([self.point(120,90,source='log'),self.point(0,50)])
        self.assertEqual(data['rate_rows'],[])

if __name__=='__main__':unittest.main()
