"""Work-conserving weighted consumers; each completion frees one slot immediately."""
from __future__ import annotations

import asyncio
from collections import Counter, deque
import time


_IDLE = object()


async def consume_fairly(refill, process, *, can_submit, concurrency=4,
                         backlog_weight=4, live_weight=1,
                         active_seconds=900, checkpoint=None, group_of=None):
    """Without ``checkpoint`` the pool stops taking work after ``active_seconds``
    and drains. With it, every ``active_seconds`` the pool runs ``checkpoint()``
    and keeps going: a deadline followed by a drain leaves slots idle behind the
    slowest in-flight item (2026-09-28: ~21% of slot time, one item ran 1539s).

    After a checkpoint, rows attempted earlier but no longer in flight may be
    offered again by ``refill`` — deferred/backed-off rows need that to retry.
    Idle workers wait for a completion or the next checkpoint instead of
    exiting while others are still busy; the pool ends once nothing is in
    flight and nothing is left to take, or ``can_submit()`` turns false.

    ``group_of(row)`` names rows that contend for the same graph entities
    (``None`` opts a row out).
    A row whose group already has work in flight is passed over while any
    other queued row is available; only when every candidate shares a busy
    group is one taken anyway, so exclusion never idles a slot
    (2026-09-29: 68% of read-set conflicts had a same-project commit land
    during their prepare).
    """
    if min(concurrency, backlog_weight, live_weight) < 1 or active_seconds <= 0:
        raise ValueError("positive scheduler bounds required")
    # Spread live slots through the schedule, not one giant backlog phase.
    schedule = []
    b = l = 0
    while b < backlog_weight or l < live_weight:
        if b < backlog_weight and (l >= live_weight or b * live_weight <= l * backlog_weight):
            schedule.append("backlog"); b += 1
        else:
            schedule.append("live"); l += 1
    queues = {kind: deque() for kind in ("backlog", "live")}
    attempted = set()
    inflight = set()
    busy_groups = Counter()
    taken = 0
    turn = 0
    stop = False
    deadline = time.monotonic() + active_seconds
    wake = asyncio.Event()

    def take():
        nonlocal turn, taken, deadline
        if stop or not can_submit():
            return None
        if time.monotonic() >= deadline:
            if checkpoint is None:
                return None
            checkpoint()
            deadline = time.monotonic() + active_seconds
            attempted.intersection_update(inflight)
            for queue in queues.values():
                queue.clear()
            wake.set()
            if stop or not can_submit():
                return None
        preferred = schedule[turn % len(schedule)]
        turn += 1
        order = (preferred, "live" if preferred == "backlog" else "backlog")
        contended = None
        for kind in order:
            if not queues[kind]:
                queues[kind].extend(refill(kind, attempted))
            passed_over = []
            chosen = None
            while queues[kind]:
                row = queues[kind].popleft()
                if row["id"] in attempted:
                    continue
                group = group_of(row) if group_of is not None else None
                if group is not None and busy_groups[group]:
                    passed_over.append(row)
                    continue
                chosen = row
                break
            queues[kind].extendleft(reversed(passed_over))
            if chosen is not None:
                attempted.add(chosen["id"])
                taken += 1
                return kind, chosen
            if contended is None and passed_over:
                contended = kind
        if contended is not None:
            row = queues[contended].popleft()
            attempted.add(row["id"])
            taken += 1
            return contended, row
        return _IDLE if checkpoint is not None and inflight else None

    async def worker():
        nonlocal stop
        try:
            while True:
                item = take()
                if item is None:
                    return
                if item is _IDLE:
                    wake.clear()
                    try:
                        await asyncio.wait_for(
                            wake.wait(), max(0.0, deadline - time.monotonic()) + 0.01)
                    except asyncio.TimeoutError:
                        pass
                    continue
                kind, row = item
                group = group_of(row) if group_of is not None else None
                inflight.add(row["id"])
                if group is not None:
                    busy_groups[group] += 1
                try:
                    await process(kind, row)
                finally:
                    inflight.discard(row["id"])
                    if group is not None:
                        busy_groups[group] -= 1
                    wake.set()
                # Locally filtered rows must not monopolize the event loop.
                await asyncio.sleep(0)
        except BaseException:
            stop = True
            raise

    tasks = [asyncio.create_task(worker()) for _ in range(concurrency)]
    drain = asyncio.gather(*tasks, return_exceptions=True)
    try:
        results = await asyncio.shield(drain)
    except BaseException:
        # Never leave paid in-flight work detached from the caller's drain.
        stop = True
        await asyncio.shield(drain)
        raise
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return taken


class ProgressLedger:
    """Convert repeated cumulative callbacks to nonnegative, once-only deltas."""
    def __init__(self):
        self.totals = {kind: {"ingested": 0, "rejected": 0, "deferred": 0,
                            "backoff_skipped": 0, "result_counts": {}, "filter_counts": {},
                            "deferred_counts": {}}
                       for kind in ("backlog", "live")}
        self.partials = {}

    def update(self, kind, oid, stats):
        previous = self.partials.get((kind, oid), {})
        delta = {}
        for key, value in stats.items():
            if isinstance(value, dict):
                diff = {k: n - previous.get(key, {}).get(k, 0) for k, n in value.items()}
                if any(n < 0 for n in diff.values()):
                    raise ValueError("progress counter regressed")
                target = self.totals[kind].setdefault(key, {})
                for k, n in diff.items():
                    target[k] = target.get(k, 0) + n
                delta[key] = diff
            else:
                diff = value - previous.get(key, 0)
                if diff < 0:
                    raise ValueError("progress counter regressed")
                self.totals[kind][key] = self.totals[kind].get(key, 0) + diff
                delta[key] = diff
        self.partials[(kind, oid)] = {k: dict(v) if isinstance(v, dict) else v for k, v in stats.items()}
        return delta

    def run(self, kind, oid):
        """One processing attempt of ``oid``: an updater plus its release.

        The pool re-offers deferred rows after a checkpoint, so one cycle can
        process the same observation twice. Keyed by observation alone, the
        second attempt's counters start from zero and look like a regression
        (2026-10-08: a 409 retried and then ingested raised "progress counter
        regressed", which stopped the whole pool until every slot drained).
        The pool never runs one observation twice at once, so releasing the
        partial when an attempt ends is enough.
        """
        def update(stats):
            return self.update(kind, oid, stats)

        def finish():
            self.partials.pop((kind, oid), None)

        return update, finish
