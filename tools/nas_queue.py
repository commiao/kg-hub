"""Process-leased FIFO reservations; never replaces the legacy heavy lock.

The short queue lock only protects metadata. Each ticket's flock, rather than a
PID or its age, determines whether the owner still exists. Progress counts are
advanced only after a real HTTP response, never by a timer heartbeat.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid


@contextlib.contextmanager
def guard(root):
    with (root / 'queue.lock').open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def update(stream, value):
    stream.seek(0)
    stream.truncate()
    json.dump(value, stream)
    stream.flush()
    os.fsync(stream.fileno())


def live_tickets(root, own=None):
    active = []
    for path in sorted(root.glob('*.ticket')):
        if path == own:
            active.append(path)
            continue
        with path.open('r+') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active.append(path)
            else:
                # Only a provably released lease can be retired. An old live
                # request must not lose its place, even after the stall budget.
                path.unlink()
    return active


class Reservation:
    def __init__(self, root, path, stream, value, clock):
        self.root, self.path, self.stream = root, path, stream
        self.value, self.clock = value, clock

    def position(self):
        with guard(self.root):
            tickets = live_tickets(self.root, self.path)
            index = tickets.index(self.path)
            head = json.loads(tickets[0].read_text())
            return index, (tickets[0].name, head['responses'])

    def response(self):
        with guard(self.root):
            self.value['responses'] += 1
            self.value['last_response_at'] = self.clock()
            update(self.stream, self.value)


@contextlib.contextmanager
def reserve(directory, kind, clock):
    root = Path(directory).expanduser() / 'nas-heavy.queue'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with guard(root):
        tickets = live_tickets(root)
        counter = root / 'sequence'
        # Read corruption fails closed. Never silently restart sequence numbers
        # and let a new arrival overtake an existing ticket.
        previous = int(counter.read_text()) if counter.exists() else 0
        largest = max((int(p.name.split('-')[0]) for p in tickets), default=0)
        sequence = max(previous, largest) + 1
        fd, temporary = tempfile.mkstemp(prefix='.sequence-', dir=root)
        try:
            with os.fdopen(fd, 'w') as out:
                out.write(str(sequence))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, counter)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        path = root / f'{sequence:020d}-{uuid.uuid4().hex}.ticket'
        stream = path.open('x+')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            value = {'kind': kind, 'queued_at': clock(), 'responses': 0}
            update(stream, value)
        except BaseException:
            stream.close()
            path.unlink(missing_ok=True)
            raise
    ticket = Reservation(root, path, stream, value, clock)
    try:
        yield ticket
    finally:
        with guard(root):
            path.unlink(missing_ok=True)
            stream.close()
