import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('merge', Path(__file__).resolve().parents[1] / 'tools/merge_claude_mem_sources.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class MergeSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = self.root / 'old.db'
        self.new = self.root / 'new.db'
        self.out = self.root / 'aggregate.db'
        for path in (self.old, self.new):
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE sdk_sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, content_session_id TEXT NOT NULL, memory_session_id TEXT UNIQUE); CREATE TABLE observations(id INTEGER PRIMARY KEY AUTOINCREMENT, memory_session_id TEXT NOT NULL, text TEXT); INSERT INTO sdk_sessions VALUES(1,"session","memory"); INSERT INTO observations VALUES(1,"memory","first");')
        self.config = {'sources': [{'name': 'legacy', 'path': str(self.old)}, {'name': 'next', 'path': str(self.new)}]}
    def tearDown(self):
        self.tmp.cleanup()
    def rows(self, sql):
        with sqlite3.connect(self.out) as db:
            return db.execute(sql).fetchall()
    def test_overlapping_ids_and_late_legacy_append(self):
        self.assertEqual(m.merge(self.config,self.out,True), {'legacy':1,'next':1})
        with sqlite3.connect(self.old) as db:
            db.execute('INSERT INTO observations VALUES(2,"memory","late legacy")')
        m.merge(self.config,self.out)
        self.assertEqual(self.rows('SELECT id,text FROM observations ORDER BY id'), [(1,'first'),(2,'first'),(3,'late legacy')])
        self.assertEqual(self.rows('SELECT source,local_id,aggregate_id FROM source_observation_map ORDER BY aggregate_id'), [('legacy',1,1),('next',1,2),('legacy',2,3)])
        self.assertEqual(m.merge(self.config,self.out), {'legacy':0,'next':0})
        self.assertEqual(self.rows('SELECT count(distinct memory_session_id) FROM sdk_sessions'), [(2,)])
    def test_legacy_orphan_observations_are_preserved(self):
        with sqlite3.connect(self.old) as db:
            db.execute('INSERT INTO observations VALUES(2,"orphan","historic")')
        m.merge(self.config,self.out,True)
        self.assertEqual(self.rows('SELECT text FROM observations WHERE id=2'), [('historic',)])

    def test_cloned_history_baseline_is_not_reimported(self):
        self.config['sources'][1]['after_id']=1
        m.merge(self.config,self.out,True)
        self.assertEqual(self.rows('SELECT count(*) FROM observations'), [(1,)])
        with sqlite3.connect(self.new) as db:
            db.execute('INSERT INTO observations VALUES(2,"memory","fresh")')
        m.merge(self.config,self.out)
        self.assertEqual(self.rows('SELECT text FROM observations ORDER BY id'), [('first',),('fresh',)])
    def test_broken_second_source_rolls_back_first_source_and_cursor(self):
        m.merge(self.config,self.out,True)
        with sqlite3.connect(self.old) as db:
            db.execute('INSERT INTO observations VALUES(2,"memory","late")')
        with sqlite3.connect(self.new) as db:
            db.execute('INSERT INTO observations VALUES(2,"missing","bad")')
        with self.assertRaisesRegex(ValueError,'without session'):
            m.merge(self.config,self.out)
        self.assertEqual(self.rows('SELECT count(*) FROM observations'),[(2,)])
        self.assertEqual(self.rows('SELECT last_id FROM source_cursor ORDER BY name'),[(1,),(1,)])
    def test_missing_output_and_reinitialization_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'restore'):
            m.merge(self.config,self.out)
        m.merge(self.config,self.out,True)
        with self.assertRaises(FileExistsError):
            m.merge(self.config,self.out,True)
    def test_source_order_is_part_of_identity(self):
        m.merge(self.config,self.out,True)
        self.config['sources'].reverse()
        with self.assertRaisesRegex(ValueError,'identity changed'):
            m.merge(self.config,self.out)

    def test_source_rollback_and_configuration_change_fail_closed(self):
        m.merge(self.config,self.out,True)
        self.config['sources'][1]['after_id']=1
        with self.assertRaisesRegex(ValueError,'identity changed'):
            m.merge(self.config,self.out)
        self.config['sources'][1]['after_id']=0
        with sqlite3.connect(self.new) as db:
            db.execute('DELETE FROM observations')
        with self.assertRaisesRegex(ValueError,'rolled back'):
            m.merge(self.config,self.out)

    def test_appended_source_columns_are_not_copied(self):
        m.merge(self.config,self.out,True)
        with sqlite3.connect(self.new) as db:
            db.execute('ALTER TABLE sdk_sessions ADD COLUMN cwd TEXT')
            db.execute('ALTER TABLE observations ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1')
            db.execute('INSERT INTO sdk_sessions VALUES(2,"s2","memory2","/repo")')
            db.execute('INSERT INTO observations VALUES(2,"memory2","after upgrade",3)')
        self.assertEqual(m.merge(self.config,self.out), {'legacy':0,'next':1})
        self.assertEqual(self.rows('SELECT text FROM observations ORDER BY id'), [('first',),('first',),('after upgrade',)])
        self.assertEqual(self.rows("SELECT name FROM pragma_table_info('observations')"), [('id',),('memory_session_id',),('text',)])
        self.assertEqual(self.rows("SELECT name FROM pragma_table_info('sdk_sessions')"), [('id',),('content_session_id',),('memory_session_id',)])

    def test_missing_or_retyped_aggregate_column_fails_closed(self):
        m.merge(self.config,self.out,True)
        with sqlite3.connect(self.new) as db:
            db.executescript('ALTER TABLE observations DROP COLUMN text; ALTER TABLE observations ADD COLUMN text INTEGER;')
        with self.assertRaisesRegex(ValueError,'schema mismatch: observations'):
            m.merge(self.config,self.out)
        with sqlite3.connect(self.new) as db:
            db.execute('ALTER TABLE observations DROP COLUMN text')
        with self.assertRaisesRegex(ValueError,'schema mismatch: observations'):
            m.merge(self.config,self.out)

if __name__=='__main__': unittest.main()
