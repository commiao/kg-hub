"""Release observations from ``held`` while the refinery keeps running.

The refinery keeps its watermark in memory and rewrites the whole file after
every settled observation, so an outside edit is overwritten within seconds.
Until now ``tools/release_held.py unhold`` therefore had to stop the refinery
(2026-10-09/10: five stops in one day). Instead, an operator drops a request
file into the state directory; the refinery applies it to its own in-memory
watermark at the start of a cycle and answers with a receipt.

Order is save watermark → write receipt → delete request. A crash in between
re-applies the same request, which is harmless: observations no longer held
are only reported as ``not_held``.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

REQUEST_NAME = "release-request.json"
_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def receipt_path(state_dir: Path, request_id: str) -> Path:
    return Path(state_dir) / f"release-request.{request_id}.done.json"


def _ids(value, field: str) -> list[int]:
    if not isinstance(value, list) or any(type(v) is not int for v in value):
        raise ValueError(f"{field} must be a list of observation ids")
    return value


def parse(document) -> dict:
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("unsupported release request")
    request_id = document.get("id")
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise ValueError("invalid release request id")
    return {"id": request_id,
            "release": _ids(document.get("release", []), "release"),
            "ingested": _ids(document.get("ingested", []), "ingested")}


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    os.replace(tmp, path)


def apply(state_dir: Path, wm: dict, save_watermark) -> dict | None:
    """Apply a pending request to ``wm`` in place; None when there is none."""
    path = Path(state_dir) / REQUEST_NAME
    if not path.exists():
        return None
    now = datetime.now(timezone.utc).isoformat()
    try:
        request = parse(json.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        # A request we cannot read must not block the next one forever.
        result = {"id": None, "error": str(exc), "applied_at": now}
        _write_atomic(Path(state_dir) / "release-request.rejected.json", result)
        path.unlink(missing_ok=True)
        return result
    held = wm["held"]
    release = {oid for oid in request["release"] if oid in held}
    ingested = {oid for oid in request["ingested"] if oid in held} - release
    asked = set(request["release"]) | set(request["ingested"])
    held.difference_update(release | ingested)
    wm["ingested"].update(ingested)
    save_watermark(wm)
    result = {"id": request["id"], "released": len(release),
              "marked_ingested": len(ingested),
              "not_held": sorted(asked - release - ingested),
              "held_left": len(held), "applied_at": now}
    _write_atomic(receipt_path(state_dir, request["id"]), result)
    path.unlink(missing_ok=True)
    return result
