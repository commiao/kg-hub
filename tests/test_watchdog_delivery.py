import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import watchdog as W


class Delivery(unittest.TestCase):
    def test_http_success_requires_business_success(self):
        for payload, expected in [({'code': 0}, True), ({'StatusCode': 0}, True),
                                  ({'code': 19024}, False), ({}, False), ({'code': False}, False)]:
            r = type('R', (), {'status_code': 200, 'json': lambda self: payload})()
            with patch.object(W, 'FEISHU_WEBHOOK', 'mock'), patch.object(W.httpx, 'post', return_value=r):
                self.assertEqual(W.send_feishu('test'), expected)

    def test_failed_delivery_survives_restart_then_acknowledges(self):
        state = {'anomalies': {}}
        with patch.object(W, 'save_state', side_effect=lambda s: state.update(s)), patch.object(W, 'emit_alert', return_value=False) as emit:
            W.deliver_alerts(state, {'capture_blocked': True}, {'capture_blocked': 'broken'}, ['mac:sync'])
            self.assertIn('capture_blocked', state['pending_alerts'])
            self.assertEqual(emit.call_count, 1)
        with patch.object(W, 'save_state', side_effect=lambda s: state.update(s)), patch.object(W, 'emit_alert', return_value=True) as emit:
            W.deliver_alerts(state, {'capture_blocked': True}, {}, ['mac:sync'])
            self.assertEqual(emit.call_count, 1)
            self.assertEqual(state['pending_alerts'], {})
            W.deliver_alerts(state, {'capture_blocked': True}, {}, ['mac:sync'])
            self.assertEqual(emit.call_count, 1)

    def test_new_blocker_not_changing_metrics_fires(self):
        state = {'anomalies': {'capture_blocked': True}, 'capture_blocker_ids': ['mac:worker']}
        with patch.object(W, 'save_state', side_effect=lambda s: state.update(s)), patch.object(W, 'emit_alert', return_value=True) as emit:
            W.deliver_alerts(state, {'capture_blocked': True}, {'capture_blocked': 'age 2'}, ['mac:worker'])
            self.assertEqual(emit.call_count, 0)
            W.deliver_alerts(state, {'capture_blocked': True}, {'capture_blocked': 'corrupt'}, ['mac:worker','mac:sync'])
            self.assertEqual(emit.call_count, 1)

    def test_recovery_supersedes_failed_fire(self):
        state = {'anomalies': {'capture_blocked': True}, 'pending_alerts': {'capture_blocked': {'severity': 'fire', 'message': 'old'}}}
        with patch.object(W, 'save_state'), patch.object(W, 'emit_alert', return_value=True) as emit:
            W.deliver_alerts(state, {'capture_blocked': False}, {}, [])
            emit.assert_called_once_with('clear', 'capture_blocked', 'resolved')


    # ── 阻塞点闪烁不算「新」(2026-10-12)───────────────────────────
    # 线上 10-09 capture_blocked 连报 10 次不解除；10-10 04:16 与 04:18 阻塞点
    # 完全相同也报了两次：claude-mem 一直在，refinery 中间消失一轮又回来。
    # 判新的基准必须是「本次故障期间已报过的」，而不是「上一轮的」。
    CM, RF = 'mac-office:claude-mem', 'mac-office:refinery'

    def _rounds(self, state, rounds):
        with patch.object(W, 'save_state', side_effect=lambda s: state.update(s)), \
             patch.object(W, 'emit_alert', return_value=True) as emit:
            for bad, ids in rounds:
                W.deliver_alerts(state, {'capture_blocked': bad},
                                 {'capture_blocked': 'x'}, ids)
        return emit

    def test_flickering_blocker_is_not_new(self):
        state = {'anomalies': {'capture_blocked': True},
                 'capture_blocker_ids': [self.CM, self.RF],
                 'capture_blocker_alerted': [self.CM, self.RF]}
        emit = self._rounds(state, [(True, [self.CM]), (True, [self.CM, self.RF])] * 2)
        self.assertEqual(emit.call_count, 0)

    def test_upgrade_without_alerted_key_starts_from_last_round(self):
        # 升级当轮：旧状态没有 capture_blocker_alerted，以上一轮集合起步，不多报
        state = {'anomalies': {'capture_blocked': True},
                 'capture_blocker_ids': [self.CM, self.RF]}
        emit = self._rounds(state, [(True, [self.CM]), (True, [self.CM, self.RF])])
        self.assertEqual(emit.call_count, 0)

    def test_genuinely_new_blocker_fires_exactly_once(self):
        state = {'anomalies': {'capture_blocked': True},
                 'capture_blocker_ids': [self.CM], 'capture_blocker_alerted': [self.CM]}
        emit = self._rounds(state, [(True, [self.CM, self.RF]), (True, [self.CM]),
                                    (True, [self.CM, self.RF])])
        self.assertEqual(emit.call_count, 1)
        self.assertEqual(emit.call_args_list[0].args[:2], ('fire', 'capture_blocked'))

    def test_clear_starts_a_new_incident(self):
        state = {'anomalies': {'capture_blocked': True},
                 'capture_blocker_ids': [self.CM, self.RF],
                 'capture_blocker_alerted': [self.CM, self.RF]}
        emit = self._rounds(state, [(False, [])])
        self.assertEqual(state['capture_blocker_alerted'], [])
        emit2 = self._rounds(state, [(True, [self.CM, self.RF])])
        self.assertEqual([c.args[0] for c in emit.call_args_list + emit2.call_args_list],
                         ['clear', 'fire'])

    def test_unknown_round_keeps_alerted(self):
        state = {'anomalies': {'capture_blocked': True},
                 'capture_blocker_ids': [self.CM, self.RF],
                 'capture_blocker_alerted': [self.CM, self.RF]}
        emit = self._rounds(state, [(True, None), (True, [self.CM, self.RF])])
        self.assertEqual(emit.call_count, 0)
        self.assertEqual(state['capture_blocker_alerted'], [self.CM, self.RF])

    def test_main_persists_alerted_across_rounds(self):
        """走完整 main()，每轮读到的就是上一轮最后一次写盘的内容。

        只测 deliver_alerts 不够：main() 末尾是整份重写状态，漏写
        capture_blocker_alerted 的话下一轮又退回「只和上一轮比」，而单测里
        save_state 打桩成原地 update，照样全绿。
        """
        state = {'anomalies': {}, 'last_run': '2026-10-12T00:00:00+00:00'}
        fired = []
        stats = {'pending': 0, 'errored_last_1h': 0, 'errored_total': 0,
                 'oldest_pending_age_seconds': None}
        for ids in [[self.CM, self.RF], [self.CM], [self.CM, self.RF],
                    [self.CM], [self.CM, self.RF]]:
            saves = []
            decision = W.CaptureDecision(['blocked'], [], (), tuple(ids))
            with patch.object(W, 'load_notify_config', return_value={}), \
                 patch.object(W, 'load_state', return_value=dict(state)), \
                 patch.object(W, 'save_state', side_effect=lambda s: saves.append(dict(s))), \
                 patch.object(W, 'check_disk_temp', return_value=(None, '')), \
                 patch.object(W, 'check_health', return_value=(True, 'ok')), \
                 patch.object(W, 'check_queue', return_value=(stats, 'ok')), \
                 patch.object(W, 'check_search_probe', return_value=('ok', 0.1, '0.1s')), \
                 patch.object(W, 'check_refinery_halt', return_value=None), \
                 patch.object(W, 'check_gateway_monitor',
                              return_value={name: False for name in W.GATEWAY_ALERTS}), \
                 patch.object(W, 'check_model_gateway_consumer_contract', return_value='ok'), \
                 patch.object(W, 'check_capture_chain', return_value=decision), \
                 patch.object(W, 'emit_alert',
                              side_effect=lambda sev, kind, msg: fired.append((sev, kind)) or True):
                W.main()
            state = saves[-1]   # 文件被每次 save 整体覆盖：下一轮读到的是最后一次
        self.assertEqual([f for f in fired if f[1] == 'capture_blocked'],
                         [('fire', 'capture_blocked')])


if __name__ == '__main__':
    unittest.main()
