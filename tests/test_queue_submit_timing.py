"""Client timing must not turn an accepted or ambiguous submit into a retry."""
import asyncio
import hashlib
import types
import unittest
from unittest.mock import patch

import httpx
from utils import gateway_queue as queue


class SubmitTimingTests(unittest.IsolatedAsyncioTestCase):
    async def submit(self, client, key='private-request-key', business='kg_hub.entity_extract'):
        return await queue._timed_submit(client, 'http://example.invalid',
            {'Authorization': 'Bearer private-token'},
            {'body': 'private-prompt'}, business, key, 1)

    async def test_elapsed_covers_receiving_the_response_without_logging_secrets(self):
        ticks = iter([10.0, 10.250])
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(202, json={'secret': 'private-answer'}))) as client:
            with patch.object(queue, 'time', types.SimpleNamespace(monotonic=lambda: next(ticks))), \
                    self.assertLogs(queue.log, level='INFO') as logs:
                response = await self.submit(client)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(logs.output), 1)
        line = logs.output[0]
        self.assertIn('elapsed_ms=250.000', line)
        self.assertIn('http_status=202', line)
        self.assertIn('key_hash=' + hashlib.sha256(b'private-request-key').hexdigest()[:16], line)
        for secret in ['private-request-key', 'private-token', 'private-prompt',
                       'private-answer', 'example.invalid']:
            self.assertNotIn(secret, line)

    async def test_logging_failure_cannot_change_an_accepted_submit(self):
        calls = []
        def handle(req):
            calls.append(req.url.path)
            return httpx.Response(202)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.object(queue.log, 'info', side_effect=RuntimeError('disk full')):
                response = await self.submit(client)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(calls, ['/v1/queue/submit'])

    async def test_timeout_remains_the_same_exception_even_if_logging_fails(self):
        error = httpx.ReadTimeout('private-provider-details')
        def handle(req):
            raise error
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.object(queue.log, 'info', side_effect=RuntimeError('disk full')):
                with self.assertRaises(httpx.ReadTimeout) as caught:
                    await self.submit(client)
        self.assertIs(caught.exception, error)

    async def test_cancelled_submit_is_not_retried_or_converted_to_transport_failure(self):
        started = asyncio.Event()
        calls = []
        async def handle(req):
            calls.append(req.url.path)
            started.set()
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertLogs(queue.log, level='INFO') as logs:
                task = asyncio.create_task(self.submit(client))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(calls, ['/v1/queue/submit'])
        self.assertIn('outcome=cancelled', logs.output[0])

    async def test_ambiguous_submit_recovery_is_timed_once_and_does_not_resubmit(self):
        calls = []
        def handle(req):
            calls.append(req.url.path)
            if req.url.path.endswith('/submit'):
                raise httpx.ReadTimeout('private-provider-details', request=req)
            return httpx.Response(200, json={'version': 1, 'job': {
                'request_key': 'private-request-key', 'business_key': 'kg_hub.entity_extract',
                'state': 'succeeded'}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertLogs(queue.log, level='INFO') as logs:
                response = await queue._submit(client, 'http://example.invalid', {},
                    {}, 'kg_hub.entity_extract', 'private-request-key', (0,))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ['/v1/queue/submit', '/v1/queue/status'])
        lines = [line for line in logs.output if '[queue:submit_timing]' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('outcome=timeout', lines[0])
        self.assertNotIn('private-provider-details', lines[0])

    async def test_untrusted_business_label_cannot_inject_a_log_line(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(400))) as client:
            with self.assertLogs(queue.log, level='INFO') as logs:
                await self.submit(client, business='key\nprivate-prompt')
        self.assertEqual(len(logs.output), 1)
        self.assertIn('business_key=invalid', logs.output[0])
        self.assertNotIn('private-prompt', logs.output[0])


if __name__ == '__main__':
    unittest.main()
