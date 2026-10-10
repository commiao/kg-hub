"""Trip the model breaker when unknown-outcome tasks pile up.

A task lands in review (needs_reconciliation) when a model call was sent but
its outcome is unknown: it may have been billed and produced nothing. One such
task is an operator's job. A burst means something upstream is broken:

- 2026-10-09 15:40: provider quota ran out, 464 tasks in an hour;
- 2026-10-10 11:00: a ~15 s idle cut on the provider side broke about half of
  the long non-streaming calls -- ~24 possibly-paid failures an hour for hours.

The gateway's circuit breaker never opened (one success resets it) and the
refinery only parks each task. This guard counts the server's own verdicts
and opens the same manual breaker an operator would, with the reason, so the
pipeline stops paying and a human decides when to resume.
"""
from __future__ import annotations

import os
import time
from collections import deque

THRESHOLD = int(os.environ.get("KG_HUB_HELD_SURGE_THRESHOLD", "5"))
WINDOW_SECONDS = float(os.environ.get("KG_HUB_HELD_SURGE_WINDOW_SEC", "900"))
AUTO_BY = "auto:held-surge"


class HeldSurgeGuard:
    def __init__(self, threshold: int = THRESHOLD, window: float = WINDOW_SECONDS,
                 clock=time.monotonic):
        self.threshold, self.window, self._clock = threshold, window, clock
        self._seen: deque[float] = deque()

    def note(self) -> int | None:
        """Record one unknown outcome; return the count when it reaches the threshold."""
        if self.threshold <= 0:
            return None
        now = self._clock()
        self._seen.append(now)
        while self._seen and now - self._seen[0] > self.window:
            self._seen.popleft()
        return len(self._seen) if len(self._seen) >= self.threshold else None


def trip_if_surging(guard: HeldSurgeGuard, breakers, key: str) -> dict | None:
    """Open ``key`` for a surge unless it is already open (never overwrite a
    human's reason). Returns what was written, or None."""
    count = guard.note()
    if count is None or breakers.is_tripped(key)[0]:
        return None
    minutes = round(guard.window / 60)
    reason = (f"{minutes} 分钟内 {count} 条任务结果未知进入待核验（可能已计费），已自动断开；"
              "查明原因后在拓扑页恢复")
    breakers.set_tripped(key, True, by=AUTO_BY, reason=reason)
    return {"count": count, "reason": reason}
