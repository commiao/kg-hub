"""Separate queue-wide availability from the cooldown of a failed observation.

Only a read-only gateway readiness probe may release an infrastructure pause.
An expired timer alone never proves that it is safe to start more paid work.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

INFRA_FAILURES = frozenset({
    "upstream_error", "provider_circuit_open", "gateway_unavailable",
    "terminal_write_unavailable", "net",
})
PAUSING_FAILURES = INFRA_FAILURES | {"quota", "daily_quota", "rate_limited"}
PROBE_DELAYS = (5, 15, 30, 60, 300)
# Gateway readiness reads several durable ledgers (each may wait on SQLite).
# Production returned healthy after 15.9s; the former 5s proxy timeout kept
# refinery paused despite recovery. Leave room for those bounded local reads.
GATEWAY_READINESS_TIMEOUT = 60
READINESS_PROXY_TIMEOUT = GATEWAY_READINESS_TIMEOUT + 5


def record_failure(state: dict, reason: str, *, cycle: int, interval: int,
                   quota_delay: int, now: float | None = None) -> None:
    now = time.time() if now is None else now
    if reason == "daily_quota":
        tomorrow = datetime.fromtimestamp(now, timezone.utc).date() + timedelta(days=1)
        retry_at = datetime.combine(tomorrow, datetime.min.time(), timezone.utc).timestamp()
    else:
        # Pauses before this one in the current run of provider 429s.
        earlier = state.get("rate_streak", 0) - (state.get("reason") == "rate_limited")
        delay = (quota_delay if reason == "quota" else
                 rate_limit_delay(earlier) if reason == "rate_limited" else 5)
        retry_at = now + delay
    # An infrastructure refusal from a second in-flight task must not shorten
    # a daily quota wait. The readiness endpoint does not guarantee quota left.
    if retry_at >= state.get("retry_at", 0):
        if reason == "rate_limited" and state.get("reason") != "rate_limited":
            # One more pause in an unbroken run of provider 429s (siblings
            # refused in the same pause do not count again).
            state["rate_streak"] = state.get("rate_streak", 0) + 1
        state.update(reason=reason, retry_at=retry_at, probe_attempt=0,
                     until_cycle=cycle + max(1, math.ceil((retry_at - now) / interval)))
    state["hits"] = state.get("hits", 0) + 1


# Provider 429s can last minutes or hours (2026-10-08: 80 minutes; 10-09:
# 15:40 until the token plan reset at 22:00). Each resume sends real tasks,
# so back off like the gateway's own hold: 60s doubling to 15 minutes.
RATE_LIMIT_BASE_DELAY = 60
RATE_LIMIT_MAX_DELAY = 900


def rate_limit_delay(streak: int) -> int:
    return min(RATE_LIMIT_BASE_DELAY * 2 ** max(0, streak), RATE_LIMIT_MAX_DELAY)


def note_success(state: dict) -> None:
    """A model-backed success ends the run of provider 429s."""
    state.pop("rate_streak", None)


def probe_due(state: dict, now: float | None = None) -> bool:
    return bool(state.get("reason")) and (time.time() if now is None else now) >= state["retry_at"]


def probe_result(state: dict, ready: bool, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    if ready:
        hits, streak = state.get("hits", 0), state.get("rate_streak")
        state.clear()
        state["hits"] = hits
        # Readiness says nothing about the provider's quota; only a success does.
        if streak:
            state["rate_streak"] = streak
    else:
        attempt = min(state.get("probe_attempt", 0) + 1, len(PROBE_DELAYS) - 1)
        state.update(probe_attempt=attempt, retry_at=now + PROBE_DELAYS[attempt])


def status_fields(state: dict) -> dict:
    reason = state.get("reason")
    return {
        "quota_paused": reason in {"quota", "daily_quota"},
        "rate_limited": reason == "rate_limited",
        "upstream_error_paused": reason in INFRA_FAILURES,
        "recovery_reason": reason,
        "recovery_retry_at": state.get("retry_at"),
        "recovery_probe_attempt": state.get("probe_attempt", 0),
        "quota_hits": state.get("hits", 0),
        "rate_limit_hits": state.get("hits", 0),
    }


def cooling(entry, cycle: int, now: float | None = None) -> bool:
    if not entry:
        return False
    if len(entry) >= 3:
        return (time.time() if now is None else now) < entry[2]
    return cycle < entry[1]


# Server error keys expire after 24h; an older cooldown no longer predicts a 409.
BACKOFF_RETENTION_SECONDS = 86400


def dump_backoff(backoff: dict, *, cycle: int, interval: int,
                 now: float | None = None) -> dict:
    """Cycle-relative cooldowns → wall-clock deadlines that survive a restart.

    The cycle counter restarts at zero with the process. Before this, every
    restart (2026-10-08: five releases in two hours) retried every cooling
    observation at once — 100+ instant 409s — and reset the exponential
    backoff to its first step.
    """
    now = time.time() if now is None else now
    out = {}
    for oid, entry in backoff.items():
        until = entry[2] if len(entry) >= 3 else now + (entry[1] - cycle) * interval
        if until >= now - BACKOFF_RETENTION_SECONDS:
            out[str(oid)] = [int(entry[0]), round(until, 3)]
    return out


def load_backoff(data: dict, *, cycle: int, interval: int,
                 now: float | None = None) -> dict:
    """Wall-clock deadlines → cooldowns for a process whose first cycle is ``cycle + 1``.

    Expired entries keep their consecutive-409 count so the next 409 continues
    the exponential sequence instead of starting over.
    """
    now = time.time() if now is None else now
    out = {}
    for oid, (count, until) in data.items():
        if until < now - BACKOFF_RETENTION_SECONDS:
            continue
        remaining = until - now
        out[int(oid)] = [int(count),
                         cycle + 1 + math.ceil(remaining / interval) if remaining > 0 else 0]
    return out
