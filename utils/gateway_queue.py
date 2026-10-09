"""Queue transport only: no model selection, rate policy, or paid-call retries."""
from __future__ import annotations
import asyncio
import hashlib
import json
import time


class QueueOutcomeError(RuntimeError):
    def __init__(self, job):
        self.job = job
        super().__init__('model queue '+job['state']+': '+str((job.get('error') or {}).get('code', 'unknown')))


def request_body(kwargs):
    from anthropic import NOT_GIVEN
    body = {k: v for k, v in kwargs.items()
            if k not in {'extra_headers', 'extra_body', 'extra_query', 'timeout'} and v is not NOT_GIVEN}
    body.update(kwargs.get('extra_body') or {})
    if body.get('stream'):
        raise ValueError('durable queue requires a complete model response')
    return body


# A submit whose answer is lost may still have been enqueued and paid for. On
# 2026-10-09 six tasks failed this way while the gateway rebuilt an image: four
# accepted jobs ran with nobody collecting them, and the twelve paid steps
# before them were discarded with their tasks (a later retry gets a new key).
SUBMIT_ATTEMPTS = 3
SUBMIT_BACKOFF_SECONDS = (2, 5)


async def _queued_job(client, base_url, headers, business_key, key):
    """Read-only lookup; it does not wait on the queue's write lock.

    None means "not found" or "could not tell" -- both let the caller submit
    the identical content again, which the gateway's unique key keeps single.
    """
    import httpx
    try:
        response = await client.post(base_url+'/v1/queue/status', headers=headers,
            json={'business_key': business_key, 'idempotency_key': key})
    except (httpx.TimeoutException, httpx.TransportError):
        return None
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response


async def _submit(client, base_url, headers, payload, business_key, key, backoff):
    """Enqueue exactly once, recovering an ambiguous or refused submit.

    Timeout / broken connection: the job may exist, so look it up first and
    only then resubmit the byte-identical payload. 503: nothing was enqueued
    (busy or draining), resubmit after a pause. Anything else -- notably 409
    for different content under the same key -- is not retried.
    """
    import httpx
    last = None
    for attempt in range(SUBMIT_ATTEMPTS):
        try:
            response = await client.post(base_url+'/v1/queue/submit', headers=headers, json=payload)
            response.raise_for_status()
            return response
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            found = await _queued_job(client, base_url, headers, business_key, key)
            if found is not None:
                return found
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 503:
                raise
            last = exc
        if attempt + 1 < SUBMIT_ATTEMPTS:
            await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])
    raise last


def body_digest(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


async def execute_queued(base_url, token, key, body, *, task_ids=None,
                         scenario='unclassified', before_submit=None,
                         transport=None, wait_seconds=1800, poll_seconds=2,
                         submit_backoff=SUBMIT_BACKOFF_SECONDS):
    import httpx
    from anthropic.types import Message
    payload = {'request': body, 'scenario': scenario}
    if task_ids is not None:
        payload['task_ids'] = task_ids
    headers = {'Authorization':'Bearer '+token, 'Idempotency-Key':key}
    if before_submit:
        await before_submit(body_digest(body))
    # SDK/HTTP-library retries stay disabled. An ambiguous submit is recovered
    # explicitly: the gateway's durable unique key keeps the same content single.
    async with httpx.AsyncClient(transport=transport, timeout=15, follow_redirects=False) as client:
        response = await _submit(client, base_url, headers, payload, body['model'], key,
                                 submit_backoff)
        deadline = time.monotonic()+wait_seconds
        while True:
            value = response.json()
            if value.get('version') != 1 or not isinstance(value.get('job'), dict):
                raise RuntimeError('invalid model queue response')
            job = value['job']
            if job.get('request_key') != key or job.get('business_key') != body['model']:
                raise RuntimeError('model queue identity mismatch')
            if job['state'] == 'succeeded':
                return Message.model_validate(job['response'])
            if job['state'] in {'failed', 'reconciliation'}:
                raise QueueOutcomeError(job)
            if job['state'] not in {'queued', 'running'}:
                raise RuntimeError('invalid model queue state')
            if time.monotonic() >= deadline:
                raise TimeoutError('model queue remains pending; resume with the same request key')
            await asyncio.sleep(poll_seconds)
            response = await client.post(base_url+'/v1/queue/status', headers=headers,
                json={'business_key':body['model'], 'idempotency_key':key})
            response.raise_for_status()


async def recover_business_receipts(journal, verify_result, cursor=("", "")):
    """Recover the graph-commit/local-receipt crash gap using read-only evidence."""
    rows = await asyncio.to_thread(journal.missing_queue_receipt_tasks, cursor)
    for sd, sid in rows:
        try:
            reference = await verify_result(sd, sid)
            if reference:
                await asyncio.to_thread(journal.queue_business_receipts, sd, sid, reference)
        except asyncio.CancelledError:
            raise
        except Exception:
            import logging
            logging.getLogger('kg_hub.queue_receipts').exception('business result remains unverified')
    return tuple(rows[-1]) if rows else ("", "")


async def receipt_loop(journal_factory, base_url, token, *, interval=10, verify_result=None):
    """Retry only durable business acknowledgements, never a model invocation."""
    import logging
    import httpx
    log = logging.getLogger('kg_hub.queue_receipts')
    cursor = ('', '')
    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
        while True:
            try:
                journal = await asyncio.to_thread(journal_factory)
                if journal:
                    if verify_result is not None:
                        cursor = await recover_business_receipts(journal, verify_result, cursor)
                    for payload in await asyncio.to_thread(journal.pending_queue_receipts):
                        try:
                            response = await client.post(base_url+'/v1/queue/ack',
                                headers={'Authorization':'Bearer '+token}, json=payload)
                            response.raise_for_status()
                            job = response.json().get('job', {})
                            if (job.get('request_key') != payload['idempotency_key']
                                    or job.get('business_key') != payload['business_key']
                                    or job.get('business_receipt') != payload['receipt']):
                                raise RuntimeError('business acknowledgement identity mismatch')
                            await asyncio.to_thread(journal.acknowledge_queue_receipt, payload['idempotency_key'])
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.exception('business receipt remains queued: %s', payload['idempotency_key'])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('business receipt remains queued for acknowledgement')
            await asyncio.sleep(interval)
