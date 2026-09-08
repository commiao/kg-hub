#!/usr/bin/env python3
"""Run one sync/apply under a kernel lock and an independent wall-time limit.

The lock inode is never removed. Children inherit it so killing the supervisor
cannot unlock a still-running writer. Each run owns a separate process group.
"""
import argparse
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import time


def run(lock, seconds, command):
    if seconds <= 0:
        raise ValueError("deadline must be positive")
    Path(lock).parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("BUSY another sync/apply is running", flush=True)
            return 75
        proc = subprocess.Popen(command, start_new_session=True,
                                pass_fds=(handle.fileno(),))
        interrupted = []
        def stop(signum, frame):
            interrupted.append(signum)
        old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        deadline = time.monotonic() + seconds
        try:
            while proc.poll() is None and not interrupted:
                if time.monotonic() >= deadline:
                    print("TIMEOUT sync/apply exceeded deadline", flush=True)
                    return 124
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            return 128 + interrupted[0] if interrupted else proc.returncode
        finally:
            # Also reap descendants when the leader exits before its children.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(0.2)
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Darwin can report EPERM for a group containing only reaped/
                # inaccessible zombies after TERM. A live leader is not safe.
                if proc.poll() is None:
                    raise
            proc.wait()
            for s, handler in old.items():
                signal.signal(s, handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("lock")
    parser.add_argument("seconds", type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    raise SystemExit(run(args.lock, args.seconds, args.command))
