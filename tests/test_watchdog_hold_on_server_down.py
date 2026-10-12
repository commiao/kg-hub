import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import watchdog as W

# 2026-10-12 线上形态：自动断路从 09:31Z 一直开着，两次发布重启 server 时
# model_breaker_auto / refinery_stalled 各发一对「resolved → 再 FIRE」。
UP = 'up'
DOWN = 'down'
TRIPPED = {'pending': 0, 'errored_last_1h': 0, 'errored_total': 0,
           'oldest_pending_age_seconds': None, 'needs_reconciliation_last_1h': 6,
           'model_breaker': {'tripped': True, 'by': 'auto:held_surge', 'reason': '5 条结果未知'}}
RESET = dict(TRIPPED, needs_reconciliation_last_1h=0, model_breaker={'tripped': False})
HALTED = {'halted': True, 'gates': ['人工断路'], 'window_open': True}
IDLE = {'halted': False, 'gates': [], 'window_open': False}
WATCHED = ('model_breaker_auto', 'held_growth', 'refinery_stalled', 'falkordb_slow')


def run_rounds(rounds):
    """rounds: [(server, stats, halt, probe)]；server=DOWN 时其余取数一律拿不到。"""
    state = {'anomalies': {}, 'last_run': '2026-10-12T00:00:00+00:00'}
    fired = []
    for server, stats, halt, probe in rounds:
        alive = server == UP
        saves = []
        with patch.object(W, 'load_notify_config', return_value={}), \
             patch.object(W, 'load_state', return_value=dict(state)), \
             patch.object(W, 'save_state', side_effect=lambda s: saves.append(dict(s))), \
             patch.object(W, 'check_disk_temp', return_value=(None, '')), \
             patch.object(W, 'check_health',
                          return_value=(True, 'ok') if alive else (False, 'ConnectError')), \
             patch.object(W, 'check_queue', return_value=(stats, 'ok')), \
             patch.object(W, 'check_search_probe', return_value=probe), \
             patch.object(W, 'check_refinery_halt', return_value=halt if alive else None), \
             patch.object(W, 'check_gateway_monitor',
                          return_value={name: False for name in W.GATEWAY_ALERTS}), \
             patch.object(W, 'check_model_gateway_consumer_contract', return_value='ok'), \
             patch.object(W, 'check_capture_chain', return_value=W.CaptureDecision(None, None)), \
             patch.object(W, 'emit_alert',
                          side_effect=lambda sev, kind, msg: fired.append((sev, kind, msg)) or True):
            W.main()
        state = saves[-1]
    return [(sev, kind, msg) for sev, kind, msg in fired if kind in WATCHED]


OK = ('ok', 2.4, '2.40s')
SLOW = ('slow', 10.6, '')


class HoldOnServerDown(unittest.TestCase):
    def kinds(self, fired):
        return sorted((sev, kind) for sev, kind, _ in fired)

    def test_restart_does_not_fake_clear(self):
        # 今晚的形态：正常 → 重启掉一轮 → 恢复，断路始终开着。只应有一开始那次 FIRE。
        fired = run_rounds([(UP, TRIPPED, HALTED, OK), (DOWN, None, None, OK),
                            (UP, TRIPPED, HALTED, OK)])
        self.assertEqual(self.kinds(fired), [('fire', 'held_growth'),
                                             ('fire', 'model_breaker_auto'),
                                             ('fire', 'refinery_stalled')])

    def test_slow_alert_survives_a_restart(self):
        fired = run_rounds([(UP, RESET, IDLE, SLOW), (UP, RESET, IDLE, SLOW),
                            (DOWN, None, None, OK), (UP, RESET, IDLE, SLOW)])
        self.assertEqual(self.kinds(fired), [('fire', 'falkordb_slow')])

    def test_long_outage_releases_with_an_honest_message(self):
        # 超过 CAPTURE_UNKNOWN_HOLD_ROUNDS 轮仍不可判：释放，但文案不能说「已恢复」。
        rounds = [(UP, TRIPPED, HALTED, OK)]
        rounds += [(DOWN, None, None, OK)] * (W.CAPTURE_UNKNOWN_HOLD_ROUNDS + 1)
        fired = run_rounds(rounds)
        clears = {kind: msg for sev, kind, msg in fired if sev == 'clear'}
        self.assertEqual(sorted(clears), ['held_growth', 'model_breaker_auto', 'refinery_stalled'])
        for kind, msg in clears.items():
            self.assertIn('不等于故障已修复', msg, kind)
            self.assertIn('server_down', msg, kind)
            self.assertNotIn('capture_probe_stale', msg, kind)

    def test_real_recovery_still_clears_at_once(self):
        fired = run_rounds([(UP, TRIPPED, HALTED, SLOW), (UP, TRIPPED, HALTED, SLOW),
                            (UP, RESET, IDLE, OK)])
        self.assertEqual(self.kinds(fired), [
            ('clear', 'falkordb_slow'), ('clear', 'held_growth'),
            ('clear', 'model_breaker_auto'), ('clear', 'refinery_stalled'),
            ('fire', 'falkordb_slow'), ('fire', 'held_growth'),
            ('fire', 'model_breaker_auto'), ('fire', 'refinery_stalled')])

    def test_unreadable_verdict_never_raises_an_alarm(self):
        # 沿用只保持已有的告警，绝不凭空报：一开始就掉线不能 FIRE 任何一条。
        fired = run_rounds([(DOWN, None, None, OK)] * 5)
        self.assertEqual(fired, [])


if __name__ == '__main__':
    unittest.main()
