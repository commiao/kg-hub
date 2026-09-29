"""Bounded, process-local commit telemetry for the flow dashboard.

Only timings and outcome flags are retained. A restart starts a new series;
the dashboard must not fill those gaps with zeroes.
"""
from __future__ import annotations

import threading
import time
from collections import deque

_samples: deque[dict] = deque(maxlen=4000)
_lock = threading.Lock()


def record(*, conflict: bool, lock_wait_s: float = 0, commit_s: float | None = None,
           validate_skipped: bool = False, prevalidated_conflict: bool = False,
           at: float | None = None) -> None:
    sample = {"at": time.time() if at is None else at,
              "conflict": conflict, "lock_wait_s": max(0, lock_wait_s),
              "commit_s": commit_s, "validate_skipped": validate_skipped,
              "prevalidated_conflict": prevalidated_conflict}
    with _lock:
        _samples.append(sample)


def recent(*, since: float) -> list[dict]:
    with _lock:
        return [row.copy() for row in _samples if row["at"] >= since]
