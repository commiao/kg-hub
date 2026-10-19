"""Read the refinery's durable work schedule on every admission decision.

Apply a reviewed JSON file with ``python -m refinery_window apply SOURCE TARGET``.
TARGET is the persistent refinery-state/refinery-window.json file. Atomic replace
lets the running refinery pick up the next decision without a container restart.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def parse(data: object) -> tuple[str, int | None, int | None]:
    if not isinstance(data, dict):
        raise ValueError("refinery window must be a JSON object")
    if data == {"mode": "all_day"}:
        return "all_day", None, None
    if set(data) != {"mode", "start_hour", "end_hour"} or data.get("mode") != "hours":
        raise ValueError("expected all_day or hours with start_hour and end_hour")
    start, end = data["start_hour"], data["end_hour"]
    if type(start) is not int or type(end) is not int or not (0 <= start < 24 and 0 <= end < 24) or start == end:
        raise ValueError("hours must be distinct integers from 0 to 23")
    return "hours", start, end


def load(path: Path) -> tuple[str, int | None, int | None]:
    return parse(json.loads(path.read_text(encoding="utf-8")))


def is_open(hour: int, path: Path, fallback_start: int, fallback_end: int) -> bool:
    if path.exists():
        mode, start, end = load(path)
        if mode == "all_day":
            return True
    else:
        start, end = fallback_start, fallback_end
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def parse_blackout(raw: str | None) -> tuple[int, int] | None:
    """"HH:MM-HH:MM" → (起始分钟, 结束分钟)，左闭右开；空串或 None 表示关闭。

    起始晚于结束即跨午夜(如 "23:50-00:10")。写错直接抛 ValueError:配错的空档
    若静默当成关闭，就又回到「半夜送出去的任务结果未知、要人工对账」。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        start_text, end_text = text.split("-")
        minutes = []
        for part in (start_text, end_text):
            hour_text, minute_text = part.strip().split(":")
            hour, minute = int(hour_text), int(minute_text)
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
            minutes.append(hour * 60 + minute)
    except ValueError:
        raise ValueError(f"blackout must look like HH:MM-HH:MM, got {raw!r}") from None
    if minutes[0] == minutes[1]:
        raise ValueError(f"blackout start and end must differ, got {raw!r}")
    return minutes[0], minutes[1]


def in_blackout(minute_of_day: int, span: tuple[int, int] | None) -> bool:
    if span is None:
        return False
    start, end = span
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def apply(source: Path, target: Path) -> None:
    data = source.read_bytes()
    parse(json.loads(data))
    if not target.parent.is_dir():
        raise ValueError("refinery state directory does not exist")
    fd, name = tempfile.mkstemp(prefix=".refinery-window-", dir=target.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["check", "apply"])
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    wanted = load(args.source)
    if args.operation == "apply":
        apply(args.source, args.target)
    actual = load(args.target)
    if actual != wanted:
        parser.error("active schedule differs from reviewed source")
    print(f"refinery schedule: {actual[0]} (active file {args.target})")
