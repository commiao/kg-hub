"""Attribute event-loop stalls to the call sites that caused them.

A heartbeat task ticks on the loop; a watcher thread samples the loop thread's
stack whenever the heartbeat is late. Samples are summed per call site and
logged as ``[loop:block]`` every report window. Sampling only happens while the
loop is stalled, so an idle loop (sitting in the selector) is never charged.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import sys
import threading
import time

log = logging.getLogger("kg_hub.loop_block")

TICK = 0.05
THRESHOLD = float(os.environ.get("KG_HUB_LOOP_BLOCK_THRESHOLD_SEC", "0.2"))
SAMPLE = 0.02
REPORT_SECONDS = float(os.environ.get("KG_HUB_LOOP_BLOCK_REPORT_SEC", "300"))
TOP = 12
CALLERS = 3
_LIBRARY_MARKERS = ("site-packages", "dist-packages", f"{os.sep}lib{os.sep}python")


def _site(frame) -> str:
    code = frame.f_code
    return f"{os.path.basename(code.co_filename)}:{frame.f_lineno}:{code.co_name}"


def stack_key(frame) -> str:
    """Innermost frame plus the innermost frames in our own code.

    A shared helper such as a SQLite ``_connect`` is useless on its own, so
    up to ``CALLERS`` of our frames are kept, innermost first.
    """
    leaf = _site(frame)
    own = []
    f = frame
    while f is not None and len(own) < CALLERS:
        if not any(m in f.f_code.co_filename for m in _LIBRARY_MARKERS):
            own.append(_site(f))
        f = f.f_back
    if not own:
        return leaf
    chain = " <- ".join(own)
    return chain if own[0] == leaf else f"{chain} -> {leaf}"


class LoopBlockProbe:
    def __init__(self, threshold: float = THRESHOLD, report_seconds: float = REPORT_SECONDS):
        self.threshold = threshold
        self.report_seconds = report_seconds
        self.sites = collections.Counter()
        self.stalled = 0.0
        self.stalls = 0
        self._last_tick = time.monotonic()
        self._loop_thread = None
        self._stop = threading.Event()
        self._task = None
        self._thread = None
        self._lock = threading.Lock()

    async def _heartbeat(self):
        while True:
            self._last_tick = time.monotonic()
            await asyncio.sleep(TICK)

    def _watch(self):
        in_stall = False
        window_started = time.monotonic()
        while not self._stop.wait(SAMPLE):
            now = time.monotonic()
            if now - self._last_tick > self.threshold:
                frame = sys._current_frames().get(self._loop_thread)
                if frame is not None:
                    with self._lock:
                        self.sites[stack_key(frame)] += SAMPLE
                        self.stalled += SAMPLE
                        if not in_stall:
                            self.stalls += 1
                in_stall = True
            else:
                in_stall = False
            if now - window_started >= self.report_seconds:
                self.report(now - window_started)
                window_started = now

    def snapshot(self):
        with self._lock:
            return self.stalled, self.stalls, self.sites.most_common(TOP)

    def report(self, window: float):
        with self._lock:
            stalled, stalls, top = self.stalled, self.stalls, self.sites.most_common(TOP)
            self.sites.clear()
            self.stalled, self.stalls = 0.0, 0
        if not stalls:
            return
        log.info("[loop:block] window=%.0fs stalled=%.1fs stalls=%d threshold=%.2fs top=%s",
                 window, stalled, stalls, self.threshold,
                 "; ".join(f"{seconds:.1f}s {site}" for site, seconds in top))

    def start(self):
        loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._last_tick = time.monotonic()
        self._task = loop.create_task(self._heartbeat())
        self._thread = threading.Thread(target=self._watch, name="loop-block-probe", daemon=True)
        self._thread.start()

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._thread is not None:
            self._thread.join(timeout=1)


_probe = None


def start_probe():
    global _probe
    if os.environ.get("KG_HUB_LOOP_BLOCK_PROBE", "1") == "0" or _probe is not None:
        return None
    _probe = LoopBlockProbe()
    _probe.start()
    return _probe


async def stop_probe():
    global _probe
    if _probe is not None:
        probe, _probe = _probe, None
        await probe.stop()
