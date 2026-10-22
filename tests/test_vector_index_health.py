"""边向量索引墓碑比例巡检（T-0347）：取数解析 + watchdog 判据。

FalkorDB 改边上任何属性都会给它换文档号、留下墓碑，maxDocId / numDocuments 随入图单调上涨，
只有重建才回落。watchdog 只读 /api/queue_stats 里 server 取好的 db.indexes() 元数据。
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import watchdog as W
from tools import vector_index_check as C
from utils.vector_index_health import edge_vector_index_stats

PROPS = ['uuid', 'group_id', 'fact', 'fact_embedding']


def row(props, status, docs, max_id, label='RELATES_TO'):
    """falkordb 客户端 result_set 的列序：label, properties, types, options, language,
    stopwords, entitytype, status, info。"""
    return [label, props, {}, {}, 'english', [], 'RELATIONSHIP', status,
            {'numDocuments': docs, 'maxDocId': max_id, 'gcPolicy': 0}]


def as_dict(r):
    keys = ['label', 'properties', 'types', 'options', 'language', 'stopwords', 'entitytype',
            'status', 'info']
    return dict(zip(keys, r))


class Parse(unittest.TestCase):
    def test_operational_ratio(self):
        # 线上 10-22 重建前的实数。
        got = edge_vector_index_stats([row(['uuid'], 'OPERATIONAL', 1000, 1000, 'Entity'),
                                       row(PROPS, 'OPERATIONAL', 174828, 395389)])
        self.assertEqual(got, {'status': 'operational', 'num_documents': 174828,
                               'max_doc_id': 395389, 'ratio': 2.262})

    def test_driver_records_are_dicts(self):
        # kg_hub_server 走 graphiti FalkorDriver.execute_query，记录是按列名的 dict。
        got = edge_vector_index_stats([as_dict(row(PROPS, 'OPERATIONAL', 100, 150))])
        self.assertEqual(got['ratio'], 1.5)

    def test_rebuild_in_progress_is_not_judged(self):
        # 本机实测：重建时同一标签两行，建设中的那行排前面、数是半成品（61583/61583 看着很干净）。
        got = edge_vector_index_stats([
            row(PROPS, '[Indexing] 61583/172188: UNDER CONSTRUCTION', 61583, 61583),
            row(['uuid', 'group_id', 'fact'], 'OPERATIONAL', 172188, 172360)])
        self.assertEqual(got['status'], 'building')
        self.assertIsNone(got['ratio'])

    def test_rebuild_reports_progress_of_the_new_index(self):
        # 行序不保证：不管旧索引排在前还是后，报的都是在建那份的进度。
        got = edge_vector_index_stats([
            row(['uuid', 'group_id', 'fact'], 'OPERATIONAL', 172188, 172360),
            row(PROPS, '[Indexing] 61583/172188: UNDER CONSTRUCTION', 61583, 61583)])
        self.assertEqual((got['status'], got['num_documents']), ('building', 61583))

    def test_two_rows_are_never_judged(self):
        # 切换瞬间新索引已 OPERATIONAL、旧的还没撤：两份并存时数不可信。
        got = edge_vector_index_stats([row(PROPS, 'OPERATIONAL', 172188, 172188),
                                       row(['uuid', 'fact'], 'OPERATIONAL', 172188, 395389)])
        self.assertEqual(got['status'], 'building')
        self.assertIsNone(got['ratio'])

    def test_single_row_under_construction_is_not_judged(self):
        got = edge_vector_index_stats([row(PROPS, '[Indexing] 1518/174828: UNDER CONSTRUCTION',
                                           1518, 1518)])
        self.assertEqual(got['status'], 'building')
        self.assertIsNone(got['ratio'])

    def test_no_vector_index(self):
        self.assertIsNone(edge_vector_index_stats([row(['uuid', 'fact'], 'OPERATIONAL', 10, 10)]))
        self.assertIsNone(edge_vector_index_stats([]))
        self.assertIsNone(edge_vector_index_stats(None))

    def test_empty_index_has_no_ratio(self):
        self.assertIsNone(edge_vector_index_stats([row(PROPS, 'OPERATIONAL', 0, 0)])['ratio'])

    def test_acceptance_tool_uses_the_same_parser(self):
        class G:
            def ro_query(self, q):
                return type('R', (), {'result_set': [
                    row(PROPS, '[Indexing] 5/9: UNDER CONSTRUCTION', 5, 5),
                    row(['uuid'], 'OPERATIONAL', 9, 30)]})()
        self.assertEqual(C.index_stats(G())['status'], 'building')

    def run_check(self, rows, argv):
        class G:
            def ro_query(self, q, params=None):
                if q != 'CALL db.indexes()':
                    raise AssertionError(f'不该跑查询: {q[:40]}')
                return type('R', (), {'result_set': rows})()
        import tools.reembed as R
        with patch.object(R, 'graph', return_value=G()), redirect_stdout(io.StringIO()) as out:
            return C.main(argv), out.getvalue()

    def test_stats_only_runs_no_query(self):
        rc, out = self.run_check([row(PROPS, 'OPERATIONAL', 100, 120)], ['--stats'])
        self.assertEqual(rc, 0)
        self.assertIn('1.2', out)

    def test_check_refuses_while_rebuilding(self):
        # 在建索引会从半成品里答查询，这时跑验收只会得到假的「不符」。
        building = [row(PROPS, '[Indexing] 5/9: UNDER CONSTRUCTION', 5, 5)]
        self.assertEqual(self.run_check(building, ['--stats'])[0], 2)
        rc, out = self.run_check(building, [])
        self.assertEqual(rc, 2)
        self.assertIn('不跑查询', out)


class QueueStats(unittest.TestCase):
    """/api/queue_stats 带出 edge_vector_index；取不到时为 None、不拖垮主统计。"""

    def call(self, indexes):
        import asyncio
        import json
        os.environ.setdefault('KG_HUB_API_TOKEN', 'unit-test-only-token')
        import kg_hub_server as S

        class D:
            async def execute_query(self, q, **kw):
                if q == 'CALL db.indexes()':
                    if indexes == 'stall':
                        import asyncio
                        await asyncio.sleep(30)
                    if isinstance(indexes, Exception):
                        raise indexes
                    return indexes, [], None
                return [], [], None
        with patch.object(S, 'get_status_driver', return_value=D()):
            return json.loads(asyncio.run(S.queue_stats(None)).body)

    def test_field_present(self):
        body = self.call([as_dict(row(PROPS, 'OPERATIONAL', 180689, 186550))])
        self.assertEqual(body['edge_vector_index']['ratio'], 1.032)
        self.assertEqual(body['status'], 'ok')

    def test_stalled_index_metadata_is_bounded(self):
        # 重建时 CREATE 执行的那几秒 db.indexes() 要等；统计照样按时返回，字段为 None。
        import time
        import kg_hub_server as S
        self.assertLessEqual(S.VECTOR_INDEX_STATS_TIMEOUT, 5)
        with patch.object(S, 'VECTOR_INDEX_STATS_TIMEOUT', 0.05):
            t = time.time()
            body = self.call('stall')
        self.assertLess(time.time() - t, 2)
        self.assertIsNone(body['edge_vector_index'])
        self.assertEqual(body['pending'], 0)

    def test_failure_is_none(self):
        body = self.call(RuntimeError('boom'))
        self.assertIsNone(body['edge_vector_index'])
        self.assertEqual(body['pending'], 0)


def vi(docs, max_id, status='operational'):
    ratio = round(max_id / docs, 3) if status == 'operational' else None
    return {'status': status, 'num_documents': docs, 'max_doc_id': max_id, 'ratio': ratio}


def stats_with(index):
    s = {'pending': 0, 'errored_last_1h': 0, 'errored_total': 0,
         'oldest_pending_age_seconds': None}
    if index is not ...:
        s['edge_vector_index'] = index
    return s


def run_rounds(rounds, cfg=None):
    """rounds: [(alive, check_queue 返回值)]；返回 vector_index_dirty 的告警。"""
    state = {'anomalies': {}, 'last_run': '2026-10-22T00:00:00+00:00'}
    fired, out = [], io.StringIO()
    for alive, queue in rounds:
        saves = []
        with patch.object(W, 'load_notify_config', return_value=dict(cfg or {})), \
             patch.object(W, 'load_state', return_value=dict(state)), \
             patch.object(W, 'save_state', side_effect=lambda s: saves.append(dict(s))), \
             patch.object(W, 'check_disk_temp', return_value=(None, '')), \
             patch.object(W, 'check_health',
                          return_value=(True, 'ok') if alive else (False, 'ConnectError')), \
             patch.object(W, 'check_queue', return_value=queue), \
             patch.object(W, 'check_search_probe', return_value=('ok', 0.2, '0.20s')), \
             patch.object(W, 'check_refinery_halt', return_value={'halted': False}), \
             patch.object(W, 'check_gateway_monitor',
                          return_value={name: False for name in W.GATEWAY_ALERTS}), \
             patch.object(W, 'check_model_gateway_consumer_contract', return_value='ok'), \
             patch.object(W, 'check_capture_chain', return_value=W.CaptureDecision(None, None)), \
             patch.object(W, 'emit_alert',
                          side_effect=lambda sev, kind, msg, **_: fired.append((sev, kind, msg)) or True), \
             redirect_stdout(out):
            W.main()
        state = saves[-1]
    return [(sev, msg) for sev, kind, msg in fired if kind == 'vector_index_dirty']


def ok(index):
    return (True, (stats_with(index), 'ok'))


GATE = W.VECTOR_INDEX_RATIO_DEFAULT
CLEAN = vi(174828, 174828)
DIRTY = vi(174828, int(174828 * GATE) + 1000)
BUILDING = vi(1518, 1518, 'building')


class Watchdog(unittest.TestCase):
    def test_default_gate_is_below_measured_damage(self):
        # 本机模拟：更新分散时最早在 1.50 有查询漏前 30 名；刚重建完是 1.0（线上 174828/174828）。
        self.assertGreater(GATE, 1.05)
        self.assertLess(GATE, 1.5)

    def test_clean_index_is_silent(self):
        self.assertEqual(run_rounds([ok(CLEAN), ok(vi(174828, int(174828 * GATE) - 1000))]), [])

    def test_crossing_the_gate_fires_once_with_the_fix(self):
        fired = run_rounds([ok(CLEAN), ok(DIRTY), ok(vi(174828, int(174828 * GATE) + 5000))])
        self.assertEqual([sev for sev, _ in fired], ['fire'])
        msg = fired[0][1]
        self.assertIn('maxDocId', msg)
        self.assertIn(str(GATE), msg)
        self.assertIn('tools.vector_index_check', msg)
        self.assertIn('Semantic search', msg)

    def test_mass_rewrite_ratio_fires(self):
        # 10-21 换模型后的实数，漏了全扫第 2、3 名。
        self.assertEqual([s for s, _ in run_rounds([ok(vi(174828, 395389))])], ['fire'])

    def test_rebuild_holds_then_clears(self):
        # 重建期间数是半成品：不能当成「已重建」；建好之后才 CLEAR。
        fired = run_rounds([ok(DIRTY), ok(BUILDING), ok(BUILDING), ok(CLEAN)])
        self.assertEqual([s for s, _ in fired], ['fire', 'clear'])
        self.assertIn('回落', fired[1][1])
        self.assertIn('174828', fired[1][1])

    def test_slow_rebuild_does_not_fake_release(self):
        # 重建比 server 宕机的 3 轮上限长（图再大一点就会）：server 活着、只是索引在建，
        # 按 STATS_SLOW_HOLD_ROUNDS 沿用，不能中途报一条「停止沿用」再 FIRE。
        rounds = [ok(DIRTY)] + [ok(BUILDING)] * (W.CAPTURE_UNKNOWN_HOLD_ROUNDS + 2) + [ok(CLEAN)]
        fired = run_rounds(rounds)
        self.assertEqual([s for s, _ in fired], ['fire', 'clear'])
        self.assertIn('回落', fired[1][1])

    def test_old_server_without_field_does_not_fire(self):
        self.assertEqual(run_rounds([ok(...), ok(None)]), [])

    def test_long_unknown_releases_without_claiming_a_fix(self):
        rounds = [ok(DIRTY)] + [ok(BUILDING)] * (W.STATS_SLOW_HOLD_ROUNDS + 1)
        fired = run_rounds(rounds)
        self.assertEqual([s for s, _ in fired], ['fire', 'clear'])
        self.assertIn('不等于故障已修复', fired[1][1])
        self.assertIn('索引正在重建', fired[1][1])

    def test_server_down_holds(self):
        fired = run_rounds([ok(DIRTY), (False, (None, 'ConnectError'))])
        self.assertEqual([s for s, _ in fired], ['fire'])

    def test_gate_from_notify_config(self):
        self.assertEqual([s for s, _ in run_rounds([ok(vi(100, 125))],
                                                   {'vector_index_tombstone_ratio': 1.2})],
                         ['fire'])
        self.assertEqual(run_rounds([ok(vi(100, 999))], {'vector_index_tombstone_ratio': 0}), [])


if __name__ == '__main__':
    unittest.main()
