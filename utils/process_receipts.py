"""Local process leases and immutable attempt receipts; no ps or process signals.

A held lease proves that its owner is still running. A released lease without a
terminal receipt is uncertain, never permission to replay a business operation.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid


class Busy(RuntimeError):
    pass


class Uncertain(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {"state": "invalid"}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"state": "unreadable"}


def status(directory: Path) -> dict:
    """Read a lease without creating files or requiring process-list access."""
    receipt = read(directory / "latest.json")
    try:
        with (directory / "lease.lock").open("r") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                live = True
            else:
                live = False
    except FileNotFoundError:
        live = False if not receipt else None
    except OSError:
        live = None
    state = ("running" if live is True else
             "uncertain" if live is None or receipt.get("state") in {"running", "invalid", "unreadable"}
             else receipt.get("state", "not_started"))
    return {**receipt, "state": state, "lease_held": live}


def reconcile(directory: Path, verify) -> dict:
    """Only explicit, complete read-back can clear a dead owner's uncertainty."""
    if not (directory / "latest.json").is_file():
        raise Uncertain("legacy operation has no lease receipt; host terminal evidence required")
    with (directory / "lease.lock").open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("original operation remains live; reconciliation must wait") from exc
        record = read(directory / "latest.json")
        if record.get("state") in {"invalid", "unreadable"}:
            raise Uncertain("invalid operation receipt")
        result = verify(record)
        proof = {"version": "operation-reconciliation-v1", "at": now(),
                 "attempt": record.get("attempt"), **result}
        write(directory / ("reconcile-" + uuid.uuid4().hex + ".json"), proof)
        if result.get("confirmed_not_written") is True:
            record.update(state="reconciled_not_written", requires_reconciliation=False,
                          may_have_written=False, reconciliation=proof)
            write(directory / "latest.json", record)
        return proof


@contextlib.contextmanager
def idle(directory: Path):
    """Serialize a local mutation with submission without replacing its receipt."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "lease.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("original operation remains live") from exc
        record = read(directory / "latest.json")
        if record.get("state") in {"running", "invalid", "unreadable"} or record.get("requires_reconciliation"):
            raise Uncertain("original terminal unverified; reconcile before local mutation")
        yield


@contextlib.contextmanager
def attempt(directory: Path, *, kind: str, identity: dict | None = None, allow_abandoned: bool = False):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "lease.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("original operation is live; read its receipt and wait") from exc
        previous = read(directory / "latest.json")
        if (previous.get("state") in {"invalid", "unreadable"}
                or previous.get("state") == "running" and not allow_abandoned
                or previous.get("requires_reconciliation")):
            raise Uncertain("original terminal receipt missing; reconcile before any replay")
        record = {"version": "process-receipt-v1", "attempt": uuid.uuid4().hex,
                  "kind": kind, "identity": identity or {}, "state": "running",
                  "phase": "starting", "pid": os.getpid(), "started_at": now()}

        def update(**fields):
            record.update(fields, updated_at=now())
            write(directory / (record["attempt"] + ".json"), record)
            write(directory / "latest.json", record)

        update()
        try:
            yield update
        except BaseException as exc:
            code = (exc.code if type(exc.code) is int else 0 if exc.code is None else 1) if isinstance(exc, SystemExit) else (
                130 if isinstance(exc, KeyboardInterrupt) else 1)
            update(state="failed", ended_at=now(), exit_code=code,
                   error_type=type(exc).__name__,
                   requires_reconciliation=bool(record.get("may_have_written")))
            raise
        else:
            update(state="completed", ended_at=now(), exit_code=record.get("exit_code", 0))
