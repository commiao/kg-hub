"""Queue transport only: no model selection, rate policy, or paid-call retries."""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import time

log = logging.getLogger('kg_hub.gateway_queue')


class QueueOutcomeError(RuntimeError):
    def __init__(self, job):
        self.job = job
        super().__init__('model queue '+job['state']+': '+str((job.get('error') or {}).get('code', 'unknown')))


class QueueRetryWithNewKey(QueueOutcomeError):
    """A settled failure with no model result: retry later under a new key.

    The gateway's unified result mapping (credvault #68) says so with
    ``disposition=retry, same_key=false``: a definite provider 429/5xx answer
    (cached as this key's terminal result), a job that waited past the queue's
    expiry, or one kg-hub cancelled before it ran. Nothing here needs review.
    ``status_code``/``body`` mirror an SDK error so the ingest classifier puts
    429 under rate limiting and the rest under upstream errors.
    """

    def __init__(self, job):
        super().__init__(job)
        error = job.get('error') or {}
        status = error.get('http_status')
        self.status_code = status if isinstance(status, int) and status >= 400 else 503
        code = PROVIDER_RATE_LIMITED if self.status_code == 429 else error.get('code')
        self.body = {'error': {'code': code, 'message': error.get('message', '')}}


class QueueProviderRefused(QueueRetryWithNewKey):
    """The provider answered 429: no result and nothing billed for this job.

    2026-10-09 15:40 the shared token plan ran out of quota. Each refused job
    came back as failed/provider_error/429; kg-hub only saw "the provider call
    started" and held 464 tasks for review.
    """


class QueueCancelled(QueueRetryWithNewKey):
    """kg-hub gave up waiting and the gateway withdrew the job before it ran."""


# The gateway's own code for "provider is rate limiting, not sent" (credvault
# #67). It holds such a job as queued and records the refusal in job.error.
PROVIDER_RATE_LIMITED = 'provider_rate_limited'
CANCELLED = 'cancelled_by_caller'


def provider_refused(job):
    """True when a failed job's only outcome is the provider's 429."""
    error = job.get('error') or {}
    return (job.get('state') == 'failed' and error.get('code') == 'provider_error'
            and error.get('http_status') == 429)


def retry_with_new_key(job):
    """A failed job the gateway marks retry/new key (provider_refused covers
    gateways that predate the field)."""
    error = job.get('error') or {}
    return job.get('state') == 'failed' and (
        (error.get('disposition') == 'retry' and error.get('same_key') is False)
        or provider_refused(job))


def held_by_gateway(job):
    """True while the gateway keeps this job queued on purpose: a provider rate
    limit, an open circuit, a quota or a local fault it waits out itself."""
    error = job.get('error') or {}
    return job.get('state') == 'queued' and (
        error.get('code') == PROVIDER_RATE_LIMITED or error.get('disposition') == 'paused')


held_for_rate_limit = held_by_gateway


# Process-wide view for the refinery: "the gateway is holding our jobs for a
# provider rate limit". Refreshed by every poll that sees the hold, cleared by
# any success; a view nobody refreshed for PROVIDER_WAIT_TTL is dropped.
PROVIDER_WAIT_TTL = 300
_provider_wait = {}


def _note_provider_wait(job):
    now = time.time()
    ready_at = job.get('ready_at')
    _provider_wait.update(seen_at=now, since=_provider_wait.get('since', now),
                          retry_at=ready_at if isinstance(ready_at, (int, float)) else None)


def provider_wait():
    """{'since', 'retry_at'} while the gateway holds kg-hub jobs, else None."""
    if not _provider_wait or time.time() - _provider_wait['seen_at'] > PROVIDER_WAIT_TTL:
        return None
    return {'since': _provider_wait['since'], 'retry_at': _provider_wait['retry_at']}


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
# About two minutes in total: long enough to ride out a gateway restart
# (2026-10-09 08:56: three attempts over ~7s did not).
SUBMIT_ATTEMPTS = 8
SUBMIT_BACKOFF_SECONDS = (2, 5, 10, 20, 30, 30, 30)


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


async def _cancel(client, base_url, headers, business_key, key):
    """Withdraw a job that has not started. Returns the cancelled job, the
    string 'running' when it already started (409), or None when unknown."""
    import httpx
    try:
        response = await client.post(base_url+'/v1/queue/cancel', headers=headers,
            json={'business_key': business_key, 'idempotency_key': key})
    except (httpx.TimeoutException, httpx.TransportError):
        return None
    if response.status_code == 409:
        return 'running'
    if response.status_code != 200:
        return None
    job = (response.json() or {}).get('job')
    if isinstance(job, dict) and job.get('request_key') == key and job.get('state') == 'failed':
        return job
    return None


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
                log.warning("[queue:submit_recovered] key=%s attempt=%d via=status after=%s",
                            key[:16], attempt + 1, type(exc).__name__)
                return found
            log.warning("[queue:submit_retry] key=%s attempt=%d reason=%s",
                        key[:16], attempt + 1, type(exc).__name__)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 503:
                raise
            last = exc
            log.warning("[queue:submit_retry] key=%s attempt=%d reason=http_503",
                        key[:16], attempt + 1)
        if attempt + 1 < SUBMIT_ATTEMPTS:
            await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])
    raise last


def body_digest(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


async def execute_queued(base_url, token, key, body, *, task_ids=None,
                         scenario='unclassified', before_submit=None,
                         transport=None, wait_seconds=1800, poll_seconds=2,
                         submit_backoff=SUBMIT_BACKOFF_SECONDS, held_poll_seconds=30):
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
                _provider_wait.clear()
                return Message.model_validate(job['response'])
            if (job.get('error') or {}).get('code') == CANCELLED and job['state'] == 'failed':
                raise QueueCancelled(job)
            if provider_refused(job):
                raise QueueProviderRefused(job)
            if retry_with_new_key(job):
                raise QueueRetryWithNewKey(job)
            if job['state'] in {'failed', 'reconciliation'}:
                raise QueueOutcomeError(job)
            if job['state'] not in {'queued', 'running'}:
                raise RuntimeError('invalid model queue state')
            held = held_by_gateway(job)
            if held:
                # The job stays durable and will run once the gateway resumes;
                # giving up here would leave a paid call nobody collects. How
                # long a hold lasts is not ours to bound, so the hold (not
                # elapsed time) decides: wait it out, then restart the clock.
                _note_provider_wait(job)
                deadline = time.monotonic()+wait_seconds
            elif time.monotonic() >= deadline:
                # Giving up used to leave the job queued: it later ran, was
                # billed, and nobody collected it (2026-10-09 22:55, six jobs).
                cancelled = await _cancel(client, base_url, headers, body['model'], key)
                if isinstance(cancelled, dict):
                    raise QueueCancelled(cancelled)
                if cancelled == 'running':
                    log.warning("[queue:cancel_refused] key=%s reason=running; waiting for its result",
                                key[:16])
                    deadline = time.monotonic()+wait_seconds
                else:
                    raise TimeoutError('model queue remains pending; resume with the same request key')
            await asyncio.sleep(held_poll_seconds if held else poll_seconds)
            # The job is durable in the gateway and this read changes nothing:
            # a gateway restart while polling must not fail a paid task
            # (2026-10-09 08:56: two accepted jobs were orphaned this way).
            try:
                fresh = await client.post(base_url+'/v1/queue/status', headers=headers,
                    json={'business_key':body['model'], 'idempotency_key':key})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                log.warning("[queue:poll_retry] key=%s reason=%s", key[:16], type(exc).__name__)
                continue
            if fresh.status_code == 503:
                log.warning("[queue:poll_retry] key=%s reason=http_503", key[:16])
                continue
            fresh.raise_for_status()
            response = fresh


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


class ReceiptBackoff:
    """Per-receipt pacing for /v1/queue/ack, decided by the gateway's disposition.

    2026-10-10: four receipts whose gateway jobs sat in ``reconciliation`` were
    re-acknowledged every 10s for 13 hours (6424 tracebacks). The gateway had
    answered 409 with ``disposition=failed`` -- do not retry this request -- but
    the loop never read it. ``failed`` now parks the receipt for the life of the
    process (one retry after a restart); anything else backs off per receipt.
    """
    FIRST_DELAY = 10.0
    MAX_DELAY = 3600.0

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._next: dict[str, float] = {}
        self._delay: dict[str, float] = {}
        self.parked: set[str] = set()

    def due(self, key):
        return key not in self.parked and self.clock() >= self._next.get(key, 0.0)

    def failed_before(self, key):
        return key in self._delay or key in self.parked

    def defer(self, key, retry_after=None):
        delay = min(max(self._delay.get(key, 0.0)*2, self.FIRST_DELAY), self.MAX_DELAY)
        self._delay[key] = delay
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
            delay = max(delay, float(retry_after))
        self._next[key] = self.clock()+delay
        return delay

    def park(self, key):
        self.parked.add(key)

    def clear(self, key):
        self._next.pop(key, None)
        self._delay.pop(key, None)
        self.parked.discard(key)


def _ack_error(response):
    try:
        error = response.json().get('error')
    except ValueError:
        return {}
    return error if isinstance(error, dict) else {}


async def acknowledge_receipts(client, journal, base_url, token, backoff, log):
    """One pass over pending receipts that are due; returns keys acknowledged."""
    import httpx
    acknowledged = []
    for payload in await asyncio.to_thread(journal.pending_queue_receipts):
        key = payload['idempotency_key']
        if not backoff.due(key):
            continue
        try:
            response = await client.post(base_url+'/v1/queue/ack',
                headers={'Authorization':'Bearer '+token}, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning('[receipt:retry] key=%s reason=%s next_in=%.0fs',
                        key[:16], type(exc).__name__, backoff.defer(key))
            continue
        if response.status_code >= 400:
            error = _ack_error(response)
            if error.get('disposition') == 'failed':
                backoff.park(key)
                log.warning('[receipt:parked] key=%s http=%s code=%s message=%s; '
                            'not retried until restart', key[:16], response.status_code,
                            error.get('code'), error.get('message'))
            else:
                log.warning('[receipt:retry] key=%s http=%s disposition=%s code=%s next_in=%.0fs',
                            key[:16], response.status_code, error.get('disposition'),
                            error.get('code'), backoff.defer(key, error.get('retry_after_seconds')))
            continue
        try:
            job = response.json().get('job', {})
            if (job.get('request_key') != key
                    or job.get('business_key') != payload['business_key']
                    or job.get('business_receipt') != payload['receipt']):
                raise RuntimeError('business acknowledgement identity mismatch')
            await asyncio.to_thread(journal.acknowledge_queue_receipt, key)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Traceback once per receipt; repeats stay one line.
            if backoff.failed_before(key):
                log.warning('[receipt:retry] key=%s reason=unexpected next_in=%.0fs',
                            key[:16], backoff.defer(key))
            else:
                backoff.defer(key)
                log.exception('business receipt remains queued: %s', key)
            continue
        backoff.clear(key)
        acknowledged.append(key)
    return acknowledged


async def receipt_loop(journal_factory, base_url, token, *, interval=10, verify_result=None,
                       transport=None, backoff=None):
    """Retry only durable business acknowledgements, never a model invocation."""
    import logging
    import httpx
    log = logging.getLogger('kg_hub.queue_receipts')
    backoff = backoff or ReceiptBackoff()
    cursor = ('', '')
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, transport=transport) as client:
        while True:
            try:
                journal = await asyncio.to_thread(journal_factory)
                if journal:
                    if verify_result is not None:
                        cursor = await recover_business_receipts(journal, verify_result, cursor)
                    await acknowledge_receipts(client, journal, base_url, token, backoff, log)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('business receipt remains queued for acknowledgement')
            await asyncio.sleep(interval)
