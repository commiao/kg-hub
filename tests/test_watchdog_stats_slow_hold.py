import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import watchdog as W

# 2026-10-13 01:05–01:12 线上形态：/health 每轮 200，/api/queue_stats 每轮超时，
# 连续 4 轮越过旧的 3 轮上限 → recent_errors / extraction_failing 各一对「释放 → 再 FIRE」。
FAILING = {'pending': 0, 'errored_last_1h': 29, 'errored_total': 29,
           'oldest_pending_age_seconds': None}
TIMEOUT = (None, 'ReadTimeout: timed out')
WATCHED = ('recent_errors', 'extraction_failing')


def run_rounds(rounds):
    """rounds: [(alive, check_queue 返回值)]。"""
    state = {'anomalies': {}, 'last_run': '2026-10-13T00:00:00+00:00'}
    fired, out = [], io.StringIO()
    for alive, queue in rounds:
        saves = []
        with patch.object(W, 'load_notify_config', return_value={}), \
             patch.object(W, 'load_state', return_value=dict(state)), \
             patch.object(W, 'save_state', side_effect=lambda s: saves.append(dict(s))), \
             patch.object(W, 'check_disk_temp', return_value=(None, '')), \
             patch.object(W, 'check_health',
                          return_value=(True, 'ok') if alive else (False, 'ConnectError')), \
             patch.object(W, 'check_queue', return_value=queue), \
             patch.object(W, 'check_search_probe', return_value=('ok', 2.4, '2.40s')), \
             patch.object(W, 'check_refinery_halt', return_value={'halted': False}), \
             patch.object(W, 'check_gateway_monitor',
                          return_value={name: False for name in W.GATEWAY_ALERTS}), \
             patch.object(W, 'check_model_gateway_consumer_contract', return_value='ok'), \
             patch.object(W, 'check_capture_chain', return_value=W.CaptureDecision(None, None)), \
             patch.object(W, 'emit_alert',
                          side_effect=lambda sev, kind, msg: fired.append((sev, kind, msg)) or True), \
             redirect_stdout(out):
            W.main()
        state = saves[-1]
    return [f for f in fired if f[1] in WATCHED], out.getvalue()


class StatsSlowHold(unittest.TestCase):
    def kinds(self, fired):
        return sorted((sev, kind) for sev, kind, _ in fired)

    def test_load_spike_does_not_fake_release(self):
        # 线上那两段：卡 4 轮后恢复。只应有一开始那次 FIRE。
        fired, _ = run_rounds([(True, (FAILING, 'ok'))] + [(True, TIMEOUT)] * 4
                              + [(True, (FAILING, 'ok'))])
        self.assertEqual(self.kinds(fired), [('fire', 'extraction_failing'),
                                             ('fire', 'recent_errors')])

    def test_long_unreadable_releases_with_the_real_reason(self):
        rounds = [(True, (FAILING, 'ok'))] + [(True, TIMEOUT)] * (W.STATS_SLOW_HOLD_ROUNDS + 1)
        fired, _ = run_rounds(rounds)
        clears = {kind: msg for sev, kind, msg in fired if sev == 'clear'}
        self.assertEqual(sorted(clears), sorted(WATCHED))
        for kind, msg in clears.items():
            self.assertIn('不等于故障已修复', msg, kind)
            self.assertIn('server 存活', msg, kind)
            self.assertIn('ReadTimeout', msg, kind)

    def test_release_happens_exactly_after_the_slow_limit(self):
        base = [(True, (FAILING, 'ok'))]
        held, _ = run_rounds(base + [(True, TIMEOUT)] * W.STATS_SLOW_HOLD_ROUNDS)
        self.assertEqual([f for f in held if f[0] == 'clear'], [])
        released, _ = run_rounds(base + [(True, TIMEOUT)] * (W.STATS_SLOW_HOLD_ROUNDS + 1))
        self.assertEqual(len([f for f in released if f[0] == 'clear']), 2)

    def test_server_down_keeps_the_short_limit(self):
        # 宕机由 server_down 接手，仍按 CAPTURE_UNKNOWN_HOLD_ROUNDS 释放，文案指向 server_down。
        rounds = [(True, (FAILING, 'ok'))] + [(False, TIMEOUT)] * (W.CAPTURE_UNKNOWN_HOLD_ROUNDS + 1)
        fired, _ = run_rounds(rounds)
        clears = {kind: msg for sev, kind, msg in fired if sev == 'clear'}
        self.assertEqual(sorted(clears), sorted(WATCHED))
        for msg in clears.values():
            self.assertIn('server_down', msg)
            self.assertNotIn('server 存活', msg)

    def test_unreadable_reason_is_logged(self):
        _, out = run_rounds([(True, TIMEOUT)])
        self.assertIn('queue_stats unavailable: ReadTimeout', out)

    def test_check_queue_uses_the_wider_timeout(self):
        seen = {}

        def fake_get(url, **kw):
            seen['timeout'] = kw.get('timeout')
            raise W.httpx.ReadTimeout('timed out')
        with patch.object(W, 'KG_HUB_TOKEN', 't'), patch.object(W.httpx, 'get', side_effect=fake_get):
            stats, msg = W.check_queue()
        self.assertIsNone(stats)
        self.assertEqual(seen['timeout'], W.QUEUE_STATS_TIMEOUT)
        self.assertGreaterEqual(W.QUEUE_STATS_TIMEOUT, 30)


if __name__ == '__main__':
    unittest.main()
