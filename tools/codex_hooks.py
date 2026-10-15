"""Read Codex's native hook inventory; never reproduce its hash algorithm or trust hooks.

hooks/list resolves active config layers, enabled plugins and current hashes using
exactly the same runtime that executes the hooks. Inspection failures are explicit.
"""
from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path


class HookInspectionError(RuntimeError):
    pass


def find_codex() -> str:
    override = os.environ.get("KG_HUB_CODEX_BINARY")
    candidates = ([override] if override else []) + [
        "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
        "/Applications/Codex.app/Contents/Resources/codex",
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        shutil.which("codex"),
    ]
    for path in candidates:
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return str(path)
    raise HookInspectionError("Codex executable unavailable")


def read_hooks(cwd: str | Path, timeout: float = 20) -> dict:
    # A legacy IDE launcher can leave non-UTF8 environment values. Rust's
    # std::env::vars panics on these; exclude them only from this child process.
    env = {k: v for k, v in os.environ.items()
           if not any(0xD800 <= ord(c) <= 0xDFFF for c in k + v)}
    try:
        proc = subprocess.Popen(
            [find_codex(), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env)
    except OSError as exc:
        raise HookInspectionError(f"Codex could not start: {type(exc).__name__}") from exc
    deadline = time.monotonic() + timeout
    pending = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)

    def call(method: str, params: dict, request_id: int) -> dict:
        proc.stdin.write((json.dumps({"id": request_id, "method": method,
                                      "params": params}) + "\n").encode())
        proc.stdin.flush()
        while True:
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending[:] = rest
                msg = json.loads(line)
                if msg.get("id") == request_id:
                    if "error" in msg:
                        raise HookInspectionError(f"Codex {method} rejected")
                    return msg["result"]
            left = deadline - time.monotonic()
            if left <= 0 or not selector.select(left):
                raise HookInspectionError("Codex hooks/list timed out")
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                raise HookInspectionError("Codex closed before hooks/list completed")
            pending.extend(chunk)
            if len(pending) > 8 * 1024 * 1024:
                raise HookInspectionError("Codex hook response too large")

    try:
        call("initialize", {"clientInfo": {"name": "kg-hub-hook-probe", "version": "1"},
                            "capabilities": {"experimentalApi": True}}, 1)
        proc.stdin.write(b'{"method":"initialized"}\n')
        proc.stdin.flush()
        result = call("hooks/list", {"cwds": [str(Path(cwd).resolve())]}, 2)
        entries = result.get("data", [])
        if len(entries) != 1 or entries[0].get("cwd") != str(Path(cwd).resolve()):
            raise HookInspectionError("Codex returned no matching working directory")
        entry = entries[0]
        if not isinstance(entry.get("hooks"), list):
            raise HookInspectionError("Codex returned invalid hook inventory")
        if entry.get("errors"):
            raise HookInspectionError("Codex reported hook configuration errors")
        for hook in entry["hooks"]:
            for key in ("key", "currentHash", "trustStatus", "eventName", "sourcePath"):
                if not isinstance(hook.get(key), str) or not hook[key]:
                    raise HookInspectionError("Codex returned incomplete hook metadata")
            if not isinstance(hook.get("enabled"), bool):
                raise HookInspectionError("Codex returned no hook enablement state")
        return entry
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HookInspectionError(f"Codex inspection failed: {type(exc).__name__}") from exc
    finally:
        selector.close()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        proc.stdin.close()
        proc.stdout.close()
