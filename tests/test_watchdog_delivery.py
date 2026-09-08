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


if __name__ == '__main__':
    unittest.main()
