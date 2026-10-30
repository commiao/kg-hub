"""Bounded reads of immutable evidence; never executes a transcript or grants retry.

The caller must derive anchors and expected digests independently of submitted
content, and must perform separate live process/operation reconciliation.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from tools.campaign_rework_host_receipt import require


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique_json(data):
    def pairs(values):
        out = {}
        for key, value in values:
            require(key not in out, 'duplicate JSON key')
            out[key] = value
        return out
    def constant(value):
        raise ValueError('non-finite JSON number: ' + value)
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def read_regular(anchor, relative, expected_sha, limit=128 * 1024 * 1024):
    require(type(expected_sha) is str and re.fullmatch('[0-9a-f]{64}', expected_sha), 'missing pinned hash')
    data = capture_regular(anchor, relative, limit=limit)
    require(digest(data) == expected_sha, 'evidence hash changed')
    return data


def capture_regular(anchor, relative, limit=128 * 1024 * 1024):
    """Capture current authority-owned bytes, not proof of historical identity.

    This never accepts a caller's claimed digest. Callers must retain the
    resulting digest, recheck it under their process locks, and independently
    verify history before allowing any continuation.
    """
    require(type(relative) is str and relative and not relative.startswith('/'), 'absolute or empty reference')
    parts = relative.split('/')
    require(all(x not in ('', '.', '..') for x in parts), 'path traversal')
    require(type(limit) is int and 0 < limit <= 128 * 1024 * 1024, 'invalid evidence size limit')
    anchor = Path(anchor)
    require(anchor.is_absolute() and str(anchor.resolve(strict=True)) == str(anchor), 'anchor must already be canonical')
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = os.open('/', flags | os.O_DIRECTORY)
    try:
        # Walk the canonical anchor too: O_NOFOLLOW on only its final component
        # cannot stop a raced symlink in one of its ancestors.
        for part in (*anchor.parts[1:], *parts[:-1]):
            nxt = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd); fd = nxt
        leaf = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(leaf)
            require(stat.S_ISREG(before.st_mode) and 0 <= before.st_size <= limit, 'not a bounded regular file')
            chunks, size = [], 0
            while True:
                data = os.read(leaf, min(1024 * 1024, limit + 1 - size))
                if not data:
                    break
                chunks.append(data); size += len(data)
                require(size <= limit, 'evidence grew past limit')
            after = os.fstat(leaf)
            require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                    'evidence changed while reading')
            data = b''.join(chunks)
            return data
        finally:
            os.close(leaf)
    finally:
        os.close(fd)
