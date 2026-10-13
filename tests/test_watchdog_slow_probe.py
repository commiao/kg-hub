import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import watchdog as W


class SlowProbeDebounce(unittest.TestCase):
    """falkordb_slow：连续 SLOW_CONSECUTIVE_ROUNDS 轮都慢才报，一轮正常即解除。"""

    def _apply(self, probe, prev_bad=False, prev_streak=None):
        prev_counters = {} if prev_streak is None else {'falkordb_slow_streak': prev_streak}
        new_anomalies = {'falkordb_slow': False, 'falkordb_unreachable': False}
        details, new_counters = {}, {}
        W.apply_search_probe(*probe, {'falkordb_slow': prev_bad}, new_anomalies,
                             details, prev_counters, new_counters)
        return new_anomalies, details, new_counters

    def test_single_slow_round_does_not_fire(self):
        anomalies, _, counters = self._apply(('slow', 10.6, ''))
        self.assertFalse(anomalies['falkordb_slow'])
        self.assertEqual(counters['falkordb_slow_streak'], 1)

    def test_second_consecutive_slow_round_fires(self):
        anomalies, details, counters = self._apply(('slow', 12.3, ''), prev_streak=1)
        self.assertTrue(anomalies['falkordb_slow'])
        self.assertEqual(counters['falkordb_slow_streak'], 2)
        self.assertIn('连续 2 轮', details['falkordb_slow'])
        self.assertIn('12.3s', details['falkordb_slow'])

    def test_firing_stays_on_while_slow(self):
        # 升级时正在报警、旧状态里没有计数：慢一轮也不能先假解除再重报。
        anomalies, _, _ = self._apply(('slow', 9.0, ''), prev_bad=True)
        self.assertTrue(anomalies['falkordb_slow'])

    def test_one_ok_round_clears_immediately(self):
        anomalies, _, counters = self._apply(('ok', 2.4, '2.40s'), prev_bad=True, prev_streak=5)
        self.assertFalse(anomalies['falkordb_slow'])
        self.assertEqual(counters['falkordb_slow_streak'], 0)

    def test_error_reports_unreachable_and_keeps_streak(self):
        # 探针报错 = 本轮快慢不可判：不触发、不清零，也不打断连续计数。
        anomalies, details, counters = self._apply(('error', 0.0, 'HTTP 500: x'), prev_streak=1)
        self.assertTrue(anomalies['falkordb_unreachable'])
        self.assertEqual(details['falkordb_unreachable'], 'HTTP 500: x')
        self.assertFalse(anomalies['falkordb_slow'])
        self.assertEqual(counters['falkordb_slow_streak'], 1)

    def test_unjudgeable_round_holds_a_firing_alert(self):
        for probe in (('error', 0.0, 'x'), ('skip', 0.0, 'server down')):
            anomalies, _, counters = self._apply(probe, prev_bad=True, prev_streak=3)
            self.assertTrue(anomalies['falkordb_slow'], probe)
            self.assertEqual(counters['falkordb_slow_unknown'], 1)

    def test_rounds_one_restores_single_round_behaviour(self):
        with patch.object(W, 'SLOW_CONSECUTIVE_ROUNDS', 1):
            anomalies, _, _ = self._apply(('slow', 8.5, ''))
        self.assertTrue(anomalies['falkordb_slow'])


class SlowProbeThroughMain(unittest.TestCase):
    """走完整 main()：计数必须真的跨轮落盘（main 末尾整份重写状态）。"""

    def _run(self, probes, cfg=None):
        state = {'anomalies': {}, 'last_run': '2026-10-12T00:00:00+00:00'}
        fired = []
        stats = {'pending': 0, 'errored_last_1h': 0, 'errored_total': 0,
                 'oldest_pending_age_seconds': None}
        for probe in probes:
            saves = []
            with patch.object(W, 'SLOW_CONSECUTIVE_ROUNDS', W.SLOW_CONSECUTIVE_ROUNDS), \
                 patch.object(W, 'load_notify_config', return_value=dict(cfg or {})), \
                 patch.object(W, 'load_state', return_value=dict(state)), \
                 patch.object(W, 'save_state', side_effect=lambda s: saves.append(dict(s))), \
                 patch.object(W, 'check_disk_temp', return_value=(None, '')), \
                 patch.object(W, 'check_health', return_value=(True, 'ok')), \
                 patch.object(W, 'check_queue', return_value=(stats, 'ok')), \
                 patch.object(W, 'check_search_probe', return_value=probe), \
                 patch.object(W, 'check_refinery_halt', return_value=None), \
                 patch.object(W, 'check_gateway_monitor',
                              return_value={name: False for name in W.GATEWAY_ALERTS}), \
                 patch.object(W, 'check_model_gateway_consumer_contract', return_value='ok'), \
                 patch.object(W, 'check_capture_chain',
                              return_value=W.CaptureDecision(None, None)), \
                 patch.object(W, 'emit_alert',
                              side_effect=lambda sev, kind, msg, **_: fired.append((sev, kind)) or True):
                W.main()
            state = saves[-1]
        return [f for f in fired if f[1] == 'falkordb_slow']

    SLOW = ('slow', 10.6, '')
    OK = ('ok', 2.4, '2.40s')

    def test_flicker_never_fires(self):
        # 线上 10-04/05/09 的形态：慢 1 轮就自愈，反复出现。
        self.assertEqual(self._run([self.SLOW, self.OK] * 4), [])

    def test_sustained_slow_fires_once_then_clears_once(self):
        self.assertEqual(self._run([self.SLOW, self.SLOW, self.SLOW, self.SLOW, self.OK]),
                         [('fire', 'falkordb_slow'), ('clear', 'falkordb_slow')])

    def test_hot_config_rounds(self):
        self.assertEqual(self._run([self.SLOW, self.SLOW], cfg={'falkordb_slow_rounds': 3}), [])
        self.assertEqual(self._run([self.SLOW], cfg={'falkordb_slow_rounds': 1}),
                         [('fire', 'falkordb_slow')])


if __name__ == '__main__':
    unittest.main()
