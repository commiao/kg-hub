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
        delay = quota_delay if reason == "quota" else (60 if reason == "rate_limited" else 5)
        retry_at = now + delay
    # An infrastructure refusal from a second in-flight task must not shorten
    # a daily quota wait. The readiness endpoint does not guarantee quota left.
    if retry_at >= state.get("retry_at", 0):
        state.update(reason=reason, retry_at=retry_at, probe_attempt=0,
                     until_cycle=cycle + max(1, math.ceil((retry_at - now) / interval)))
    state["hits"] = state.get("hits", 0) + 1


def probe_due(state: dict, now: float | None = None) -> bool:
    return bool(state.get("reason")) and (time.time() if now is None else now) >= state["retry_at"]


def probe_result(state: dict, ready: bool, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    if ready:
        hits = state.get("hits", 0)
        state.clear()
        state["hits"] = hits
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
