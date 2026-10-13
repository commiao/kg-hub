import copy
import re
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import watchdog as W

EDGE = re.compile(r'^\[(FIRE|CLEAR)\] ')   # 统计「真边沿」时用的写法


class AlertRetryLog(unittest.TestCase):
    """alerts.log 只把状态变化记成 [FIRE]/[CLEAR]；重发与未送达另行打标。"""

    def deliver(self, state, anomalies, delivered, details=None):
        lines = []
        with patch.object(W, 'FEISHU_WEBHOOK', 'https://example.invalid/hook'), \
             patch.object(W, 'write_alert_log', side_effect=lines.append), \
             patch.object(W, 'send_feishu', return_value=delivered), \
             patch.object(W, 'save_state',
                          side_effect=lambda s: state.update(copy.deepcopy(s))), \
             patch('builtins.print'):
            # 深拷贝：真实 save_state 在调用那一刻落盘，不能靠共享同一个 dict 「看见」后来的修改。
            W.deliver_alerts(state, anomalies, details or {'held_growth': '5 条进待核验'}, None)
        return lines

    def test_failed_send_then_retry_is_one_edge(self):
        # 10-13 02:00 的形态：首发时飞书不可达，下一轮补发成功。
        state = {'anomalies': {}}
        first = self.deliver(state, {'held_growth': True}, delivered=False)
        self.assertEqual(first[0], '[FIRE] held_growth: 5 条进待核验')
        self.assertTrue(first[1].startswith('[PENDING] held_growth: 飞书未送达（第 1 次）'))
        self.assertEqual(state['pending_alerts']['held_growth']['attempts'], 1)
        second = self.deliver(state, {'held_growth': True}, delivered=True)
        self.assertEqual(second, ['[FIRE·重发#2] held_growth: 5 条进待核验'])
        self.assertEqual(state['pending_alerts'], {})
        self.assertEqual(len([l for l in first + second if EDGE.match(l)]), 1)

    def test_long_outage_keeps_counting(self):
        # 09-15 那种连续投递失败：只有第一行是边沿，重发次数递增。
        state = {'anomalies': {}}
        lines = []
        for _ in range(4):
            lines += self.deliver(state, {'held_growth': True}, delivered=False)
        tags = [l.split(']')[0] + ']' for l in lines if not l.startswith('[PENDING]')]
        self.assertEqual(tags, ['[FIRE]', '[FIRE·重发#2]', '[FIRE·重发#3]', '[FIRE·重发#4]'])
        self.assertEqual(len([l for l in lines if l.startswith('[PENDING]')]), 4)

    def test_pre_upgrade_pending_entry_is_a_retry(self):
        # 升级前留在队列里的条目没有 attempts，但它一定已经发过一次。
        state = {'anomalies': {'held_growth': True},
                 'pending_alerts': {'held_growth': {'severity': 'fire', 'message': 'old'}}}
        lines = self.deliver(state, {'held_growth': True}, delivered=True)
        self.assertEqual(lines, ['[FIRE·重发#2] held_growth: old'])

    def test_superseded_pending_starts_a_fresh_edge(self):
        # FIRE 还没送达就已恢复：新的 CLEAR 是一个新边沿，不是重发。
        state = {'anomalies': {}}
        self.deliver(state, {'held_growth': True}, delivered=False)
        lines = self.deliver(state, {'held_growth': False}, delivered=True,
                             details={'held_growth:clear': '已回落'})
        self.assertEqual(lines, ['[CLEAR] held_growth: 已回落'])

    def test_retry_count_survives_main_rewrite(self):
        # main() 末尾整份重写状态；attempts 若没随 pending 落盘，下一轮又会记成首发。
        state = {'anomalies': {}, 'last_run': '2026-10-13T00:00:00+00:00'}
        stats = {'pending': 0, 'errored_last_1h': 0, 'errored_total': 0,
                 'oldest_pending_age_seconds': None, 'needs_reconciliation_last_1h': 6}
        lines = []
        for delivered in (False, True):
            saves = []
            with patch.object(W, 'FEISHU_WEBHOOK', 'https://example.invalid/hook'), \
                 patch.object(W, 'write_alert_log', side_effect=lines.append), \
                 patch.object(W, 'send_feishu', return_value=delivered), \
                 patch.object(W, 'load_notify_config', return_value={}), \
                 patch.object(W, 'load_state', return_value=dict(state)), \
                 patch.object(W, 'save_state', side_effect=lambda s: saves.append(dict(s))), \
                 patch.object(W, 'check_disk_temp', return_value=(None, '')), \
                 patch.object(W, 'check_health', return_value=(True, 'ok')), \
                 patch.object(W, 'check_queue', return_value=(stats, 'ok')), \
                 patch.object(W, 'check_search_probe', return_value=('ok', 2.4, '2.40s')), \
                 patch.object(W, 'check_refinery_halt', return_value={'halted': False}), \
                 patch.object(W, 'check_gateway_monitor',
                              return_value={name: False for name in W.GATEWAY_ALERTS}), \
                 patch.object(W, 'check_model_gateway_consumer_contract', return_value='ok'), \
                 patch.object(W, 'check_capture_chain', return_value=W.CaptureDecision(None, None)), \
                 patch('builtins.print'):
                W.main()
            state = saves[-1]
        held = [l for l in lines if 'held_growth' in l]
        self.assertEqual([l.split(']')[0] + ']' for l in held],
                         ['[FIRE]', '[PENDING]', '[FIRE·重发#2]'])
        self.assertEqual(state['pending_alerts'], {})


if __name__ == '__main__':
    unittest.main()
