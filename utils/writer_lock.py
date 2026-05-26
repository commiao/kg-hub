"""
Shared writer lock for kg-hub ingest jobs.

Why: graphiti's add_episode() does (1) LLM extraction → (2) entity dedup
read → (3) entity/edge write. Step 2→3 has a race window: two writers
processing different sources but mentioning the same entity (e.g. "Cron")
will both see "no existing match" and create separate Entity nodes,
splitting the hub. This lock serializes ALL local writers so that
read-then-write is logically atomic w.r.t. FalkorDB state.

Scope:
  * In-scope: any process on this Mac that calls graphiti.add_episode()
    (ingesters/openclaw_capsule.py, ingesters/claude_mem_obs.py, future
    manual or MCP-triggered writers).
  * Out-of-scope: MCP READ tools (kg_search etc.) — they don't write.
  * Out-of-scope: cross-machine writers (Phase 3 OpenClaw push) — those
    will need server-side idempotency keys, which file locks can't provide.

Behavior:
  Non-blocking by default. If the lock is busy, raises WriterLockBusy
  so the caller can log "skipping this round" and exit cleanly — important
  for launchd jobs (clean exit avoids being marked failed).

Crash safety: fcntl.flock is auto-released by the OS on process exit,
including SIGKILL from launchd. No stale lock cleanup needed.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

LOCK_DIR = Path.home() / ".kg-hub" / "locks"
LOCK_FILE = LOCK_DIR / "writer.lock"


class WriterLockBusy(Exception):
    """Another writer holds the lock; caller should skip this round."""


@contextmanager
def writer_lock(owner: str = "?", timeout_seconds: float = 0.0):
    """
    Acquire an exclusive flock on ~/.kg-hub/locks/writer.lock.

    Args:
        owner: short label written into the lock file for diagnostics.
        timeout_seconds: 0 = fail fast on contention (default); >0 = poll
            up to this many seconds before giving up.

    Raises:
        WriterLockBusy: another writer holds the lock and timeout expired.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = open(LOCK_FILE, "a+")
    acquired = False
    try:
        deadline = time.monotonic() + max(timeout_seconds, 0)
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WriterLockBusy(
                        f"writer.lock held by another process; {owner} skipping"
                    )
                time.sleep(0.5)

        # Stamp owner + pid + acquire time for `cat writer.lock` debugging.
        fd.seek(0)
        fd.truncate()
        fd.write(
            f"owner={owner} pid={os.getpid()} "
            f"acquired={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        fd.flush()
        yield fd
    finally:
        if acquired:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


@asynccontextmanager
async def async_writer_lock(owner: str = "?", timeout_seconds: float = 0.0):
    """
    Async-friendly version of `writer_lock`. Same fcntl flock semantics
    (cross-process exclusive), but waits via `asyncio.sleep` instead of
    `time.sleep` so it does NOT block the event loop while polling.

    CRITICAL: use this version inside asyncio coroutines (e.g. kg_hub_server's
    background tasks). The sync `writer_lock` will freeze the event loop and
    defeat async-by-default.

    Args/Raises: identical to `writer_lock`.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = open(LOCK_FILE, "a+")
    acquired = False
    try:
        deadline = time.monotonic() + max(timeout_seconds, 0)
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WriterLockBusy(
                        f"writer.lock held by another process; {owner} skipping"
                    )
                await asyncio.sleep(0.5)  # async-friendly: yields to event loop

        fd.seek(0)
        fd.truncate()
        fd.write(
            f"owner={owner} pid={os.getpid()} "
            f"acquired={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        fd.flush()
        yield fd
    finally:
        if acquired:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
