"""Host-only administration of the existing hashed scoped-token registry.

Never run this inside the service's read-only registry mount. Tokens are emitted
only into a new 0600 file; terminal output contains public metadata only.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

CODE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_ROOT))

from utils import token_auth


@contextmanager
def _locked(registry: Path):
    registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(registry) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read(registry: Path) -> dict:
    if registry.is_symlink():
        raise ValueError("registry must be a regular file, not a symlink")
    if not registry.exists():
        return {"tokens": []}
    try:
        data = json.loads(registry.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("registry is unreadable or invalid; refusing to replace it") from exc
    if (not isinstance(data, dict) or not isinstance(data.get("tokens"), list)
            or any(not isinstance(entry, dict) for entry in data["tokens"])):
        raise ValueError("registry must contain a tokens list of objects")
    return data


def _atomic_write(registry: Path, data: dict):
    fd, tmp = tempfile.mkstemp(prefix="." + registry.name + ".", dir=registry.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, registry)
        directory = os.open(registry.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class RegistryRollbackUnconfirmed(RuntimeError):
    """Storage failure prevented confirming a failed issue was rolled back."""


def _restore_failed_issue(registry: Path, original: dict, existed: bool, attempted: dict):
    try:
        current = _read(registry)
        if current == original and registry.exists() == existed:
            return
        if current != attempted:
            raise RegistryRollbackUnconfirmed("registry changed outside issuer lock")
        if existed:
            try:
                _atomic_write(registry, original)
            except OSError:
                pass  # A post-replace directory-fsync failure may still restore bytes.
        else:
            registry.unlink()
        if _read(registry) != original or registry.exists() != existed:
            raise RegistryRollbackUnconfirmed("failed issue rollback could not be verified")
    except (OSError, ValueError) as exc:
        raise RegistryRollbackUnconfirmed("failed issue rollback could not be verified") from exc


def issue(registry: Path, *, name: str, scopes: list[str], token_out: Path) -> dict:
    if not name.strip() or len(name) > 200:
        raise ValueError("name must contain 1–200 characters")
    scopes = sorted(set(scopes))
    if not scopes or any(scope not in token_auth.SCOPE_ALLOWLIST for scope in scopes):
        raise ValueError("only registered scopes read/extract are supported")
    if token_out.resolve() in (registry.resolve(), Path(str(registry) + ".lock").resolve()):
        raise ValueError("token output must differ from registry and lock")
    with _locked(registry):
        existed = registry.exists()
        data = _read(registry)
        original = deepcopy(data)
        if any(entry.get("name") == name for entry in data["tokens"]):
            raise ValueError("device name already exists; use a fresh name for rotation")
        token_out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Exclusive creation prevents a credential rotation from destroying a
        # device's working credential, including when token_out is a symlink.
        token = secrets.token_urlsafe(32)
        created = {"name": name, "scopes": scopes, "created_at": datetime.now(timezone.utc).isoformat()}
        entry = {**created, "sha256": token_auth.digest_for(token)}
        fd = os.open(token_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(token + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            data["tokens"].append(entry)
            _atomic_write(registry, data)
        except BaseException:
            # A post-replace fsync error is still a failed issue: restore the
            # previous registry while holding the same lock, then verify it.
            try:
                token_out.unlink(missing_ok=True)
            except OSError as exc:
                raise RegistryRollbackUnconfirmed("failed credential cleanup could not be verified") from exc
            _restore_failed_issue(registry, original, existed, data)
            raise
        return {**created, "status": "issued", "token_file": str(token_out)}


def revoke(registry: Path, *, name: str) -> dict:
    with _locked(registry):
        data = _read(registry)
        matched = [entry for entry in data["tokens"] if entry.get("name") == name]
        if not matched:
            raise ValueError("device name not found")
        for entry in matched:
            entry.update(disabled=True, revoked_at=datetime.now(timezone.utc).isoformat())
        _atomic_write(registry, data)
    return {"status": "revoked", "name": name}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Host-only issue/revoke of existing scoped KG API credentials; never print tokens")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("issue", "list", "revoke"):
        sub = commands.add_parser(command)
        sub.add_argument("--registry", required=True, type=Path, help="host registry JSON (the service mount is read-only)")
        if command != "list":
            sub.add_argument("--name", required=True, help="unique device identity")
        if command == "issue":
            sub.add_argument("--scope", action="append", choices=sorted(token_auth.SCOPE_ALLOWLIST), required=True)
            sub.add_argument("--token-out", type=Path, required=True, help="new exclusive 0600 plaintext credential file")
    args = parser.parse_args(argv)
    try:
        if args.command == "issue":
            result = issue(args.registry, name=args.name, scopes=args.scope, token_out=args.token_out)
        elif args.command == "revoke":
            result = revoke(args.registry, name=args.name)
        else:
            with _locked(args.registry):
                data = _read(args.registry)
            result = {"tokens": [{key: entry[key] for key in ("name", "scopes", "disabled", "created_at", "revoked_at")
                                   if key in entry} for entry in data["tokens"]]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except RegistryRollbackUnconfirmed:
        print(json.dumps({"status": "error", "code": "registry_rollback_unconfirmed",
                          "message": "storage failure requires operator inspection; do not use this issue attempt"}))
        return 1
    except (OSError, ValueError):
        # Do not echo exception payloads or registry contents into service logs.
        print(json.dumps({"status": "error", "message": "credential operation refused; check registry, unique name, scope and exclusive output file"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
