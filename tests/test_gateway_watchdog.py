"""Synthetic health and mocked delivery only: never send a provider/webhook call."""
import asyncio
import copy
import importlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import dashboard_status as D
from tools import watchdog as W


def health():
    checks = {name: {'status': 'ok', 'issues': []} for name in D._MONITOR_CHECKS}
    checks['passive_provider'].update(business_keys={}, degraded_metrics={})
    return {'status': 'ok', 'external_calls': 0, 'checks': checks}


def project(body=None, status=200):
    return D.gateway_monitor_projection(body or health(), status, datetime.now(timezone.utc).isoformat())


class GatewayWatchdogTests(unittest.TestCase):
    def test_idle_and_approved_history_are_not_invalid_credentials(self):
        body = health()
        body['checks']['historical_outcomes'].update(idempotency_count=7, provider_marker_count=5,
            warnings=['operator_approved_unknown_outcome_no_replay'])
        sample = project(body)
        self.assertTrue(sample['source_ok'])
        self.assertFalse(any(sample[k] for k in W.GATEWAY_ALERTS))

    def test_old_misnamed_write_failure_is_unknown_not_disk_or_key_failure(self):
        body = health(); body['status'] = 'error'
        body['checks']['passive_provider'].update(status='error', issues=['provider_state_write_failed'],
            degraded_metrics={'provider_state_write_failures_total': 0,
                'provider_state_write_degraded_business_keys': ['claude_mem.observation'],
                'provider_state_degradation_marker_issue': False})
        sample = project(body, 503)
        self.assertTrue(sample['not_ready'] and sample['outcome_unresolved'])
        self.assertFalse(sample['persistence_failed'] or sample['authentication_failed'])

    def test_real_write_and_authentication_failures_are_distinct(self):
        body = health(); body['status'] = 'error'
        passive = body['checks']['passive_provider']
        passive.update(status='error', issues=['provider_state_write_failed'],
                       degraded_metrics={'provider_state_write_failures_total': 1,
                                         'provider_state_write_failed_business_keys': ['business']},
                       business_keys={'business': {'status': 'authentication_failed', 'at': '2026-09-08T00:00:00Z'}})
        sample = project(body, 503)
        self.assertTrue(sample['persistence_failed'] and sample['authentication_failed'])
        self.assertFalse(sample['outcome_unresolved'])

    def test_legacy_cumulative_write_count_is_not_current_failure_proof(self):
        body = health(); body['checks']['passive_provider'].update(status='error',
            issues=['provider_state_write_failed'], degraded_metrics={
                'provider_state_write_failures_total': 3,
                'provider_state_write_degraded_business_keys': ['biz'],
                'provider_state_degradation_marker_issue': False})
        self.assertFalse(project(body, 503)['source_ok'])

    def test_new_unknown_issue_and_secret_text_not_forwarded(self):
        body = health(); body['status'] = 'error'
        body['checks']['passive_provider'].update(status='error', issues=['provider_outcome_unresolved', 'secret-do-not-emit'])
        body['secret'] = 'dummy-key-do-not-emit'
        sample = project(body, 503)
        self.assertTrue(sample['outcome_unresolved'])
        self.assertNotIn('do-not-emit', json.dumps(sample))

    def test_missing_checks_and_unknown_passive_shape_are_unknown(self):
        for body in (None, {}, {**health(), 'external_calls': False}):
            self.assertFalse(D.gateway_monitor_projection(body, 200, 'now')['source_ok'])
        body = health(); del body['checks']['credentials']
        self.assertFalse(project(body)['source_ok'])

    def test_unreadable_passive_ledger_cannot_clear_previous_auth_failure(self):
        body = health(); body['checks']['passive_provider']['issues'] = ['provider_state_unavailable']
        self.assertFalse(project(body, 503)['source_ok'])

    def test_malformed_success_or_metrics_cannot_clear_or_invent_failures(self):
        for entry in ({'status': 'success'}, {'status': 'success', 'at': 'invalid'},
                      {'status': 'success', 'at': '2026-09-08'},
                      {'status': 'success', 'at': '2026-09-08T00:00:00Z', 'extra': 'secret'}):
            body = health(); body['checks']['passive_provider']['business_keys'] = {'biz': entry}
            self.assertFalse(project(body)['source_ok'])
        for metrics in ({'provider_state_write_failed_business_keys': 'bad'},
                        {'provider_state_write_degraded_business_keys': [123]},
                        {'provider_state_degradation_marker_issue': 'false'}):
            body = health(); body['checks']['passive_provider']['degraded_metrics'] = metrics
            self.assertFalse(project(body)['source_ok'])
        body = health(); body['checks']['passive_provider']['business_keys'] = {'biz': {'status': 'invented'}}
        self.assertFalse(project(body)['source_ok'])

    def test_fixed_get_no_redirect_or_provider_request(self):
        response = type('Response', (), {'status_code': 200, 'json': lambda self: {'ok': True, 'gateway_monitor': project()}})()
        with patch.object(W, 'KG_HUB_URL', 'http://kg_hub_server:8080'), \
             patch.object(W.httpx, 'get', return_value=response) as get, \
             patch.object(W.httpx, 'post', side_effect=AssertionError('provider forbidden')):
            self.assertIsNotNone(W.check_gateway_monitor())
        self.assertEqual(get.call_args.args, ('http://kg_hub_server:8080/api/topology/latest',))
        self.assertFalse(get.call_args.kwargs['follow_redirects'])
        self.assertFalse(get.call_args.kwargs['trust_env'])

    def test_untrusted_urls_stale_and_failed_sources_never_clear(self):
        for url in ('https://public.example', 'http://evil.invalid:8080',
                    'http://token@127.0.0.1:8080', 'http://127.0.0.1:8080/messages'):
            with patch.object(W, 'KG_HUB_URL', url), patch.object(W.httpx, 'get') as get:
                self.assertIsNone(W.check_gateway_monitor()); get.assert_not_called()
        sample = project(); sample['checked_at'] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        response = type('Response', (), {'status_code': 200, 'json': lambda self: {'ok': True, 'gateway_monitor': sample}})()
        with patch.object(W, 'KG_HUB_URL', 'http://127.0.0.1:8080'), patch.object(W.httpx, 'get', return_value=response):
            self.assertIsNone(W.check_gateway_monitor())
        anomalies = {'capture_blocked': True}; details = {}
        W.apply_gateway_monitor(None, {'gateway_authentication_failed': True}, anomalies, details)
        self.assertTrue(anomalies['capture_blocked'] and anomalies['gateway_authentication_failed'])
        self.assertTrue(anomalies['gateway_monitor_unhealthy'])

    def test_delivery_pending_retry_and_recovery_use_existing_channel(self):
        state = {'anomalies': {}}; anomalies = {}; details = {}
        sample = {name: name == 'authentication_failed' for name in W.GATEWAY_ALERTS}
        W.apply_gateway_monitor(sample, {}, anomalies, details)
        with patch.object(W, 'save_state', side_effect=lambda value: state.update(copy.deepcopy(value))), \
             patch.object(W, 'emit_alert', return_value=False) as emit:
            W.deliver_alerts(state, anomalies, details, [])
            self.assertIn('gateway_authentication_failed', state['pending_alerts'])
            self.assertEqual(emit.call_count, 1)
        with patch.object(W, 'save_state', side_effect=lambda value: state.update(copy.deepcopy(value))), \
             patch.object(W, 'emit_alert', return_value=True) as emit:
            W.deliver_alerts(state, anomalies, details, [])
            self.assertEqual(state['pending_alerts'], {})
            W.deliver_alerts(state, anomalies, details, [])
            self.assertEqual(emit.call_count, 1)

    def test_topology_503_gateway_preserves_capture_http200_even_without_devices(self):
        import topology as T
        async def run():
            with patch.object(T, '_load_snapshots', new=AsyncMock(return_value=[])), \
                 patch.object(T, 'load_config', return_value={}), \
                 patch.object(T, 'gateway_health', new=AsyncMock(return_value={'monitor': project(health(), 503)})):
                response = await T.topology_latest(None)
            self.assertEqual(response.status_code, 200)
            payload = json.loads(response.body)
            self.assertTrue(payload['ok'])
            self.assertTrue(payload['gateway_monitor']['not_ready'])
            self.assertEqual(payload['snapshots'], [])
        asyncio.run(run())

    def test_gateway_exception_preserves_capture_endpoint(self):
        import topology as T
        async def run():
            with patch.object(T, '_load_snapshots', new=AsyncMock(return_value=[{'_host': 'fixture'}])), \
                 patch.object(T, 'load_config', return_value={}), \
                 patch.object(T, 'gateway_health', new=AsyncMock(side_effect=RuntimeError('dummy-secret'))):
                response = await T.topology_latest(None)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.body)['snapshots'], [{'_host': 'fixture'}])
            self.assertIsNone(json.loads(response.body)['gateway_monitor'])
            self.assertNotIn('dummy-secret', response.body.decode())
        asyncio.run(run())

    def test_actual_main_delivers_gateway_and_capture_independently(self):
        state = {'anomalies': {}, 'last_run': '2026-09-08T00:00:00Z'}
        sample = {key: key in {'not_ready', 'outcome_unresolved'} for key in W.GATEWAY_ALERTS}
        with patch.object(W, 'load_notify_config', return_value={}), \
             patch.object(W, 'load_state', return_value=state), \
             patch.object(W, 'save_state'), \
             patch.object(W, 'check_disk_temp', return_value=(None, '')), \
             patch.object(W, 'check_health', return_value=(True, 'ok')), \
             patch.object(W, 'check_queue', return_value=({}, 'ok')), \
             patch.object(W, 'check_search_probe', return_value=('ok', 0, '')), \
             patch.object(W, 'check_gateway_monitor', return_value=sample), \
             patch.object(W, 'check_capture_chain', return_value=W.CaptureDecision(['fixture'], [], (), ('fixture',))) as capture, \
             patch.object(W, 'emit_alert', return_value=True) as emit:
            self.assertEqual(W.main(), 0)
        capture.assert_called_once()
        kinds = {call.args[1] for call in emit.call_args_list}
        self.assertTrue({'gateway_not_ready', 'gateway_outcome_unresolved', 'capture_blocked'} <= kinds)
        self.assertNotIn('gateway_persistence_failed', kinds)
        self.assertNotIn('gateway_authentication_failed', kinds)

    def test_existing_capture_and_main_contracts_remain_valid(self):
        # Existing capture module uses plain pytest-style functions. Execute all
        # of those unmodified functions as one standard unittest compatibility
        # check, mocking only this new independent sampler (no network).
        module = importlib.import_module('tests.test_watchdog_capture')
        healthy = {name: False for name in W.GATEWAY_ALERTS}
        with patch.object(W, 'check_gateway_monitor', return_value=healthy):
            for name in sorted(vars(module)):
                if name.startswith('test_'):
                    with self.subTest(name=name): getattr(module, name)()


if __name__ == '__main__':
    unittest.main()
