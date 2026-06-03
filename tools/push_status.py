"""
tools/push_status.py — anti-black-box health check for the kg-hub PUSH hook.

The PUSH hook (kg_push_hook.py) injects canonical context silently at every
SessionStart across Claude Code / Cursor / Codex. "Silent" is the danger:
if FalkorDB dies, a tool upgrades its hook schema, or a runtime stops echoing
the chip, the injection can fail with NO visible signal — the user only knows
something broke when they happen to notice the chip is gone.

This command answers, on demand: **"Is PUSH still firing in each tool, when
did it last fire, and what did it reference?"** It reads data/.push_hook.log
(written by kg_push_hook.py on every invocation) and reports per-tool health.

Read-only. No DB connection, no graph mutation — just log analysis, so it
works even when FalkorDB is down (and will tell you that's why PUSH is failing).

Usage:
  python -m tools.push_status              # human-readable per-tool health
  python -m tools.push_status --window 24h # only consider last 24h of log
  python -m tools.push_status --json       # machine-readable
  python -m tools.push_status --tail 20    # also show last 20 raw events
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / ".push_hook.log"

# Freshness thresholds (since last *successful* fire) for the health verdict.
FRESH_HOURS = 48      # 🟢 fired within 2 days
STALE_HOURS = 168     # 🟡 within a week; 🔴 beyond

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
FMT_RE = re.compile(r"fmt=(\S+)")
KW_RE = re.compile(r"kw=(\S+)")
NAMES_RE = re.compile(r"names=\[([^\]]*)\]")
PICKED_RE = re.compile(r"picked=(\d+)")
ELAPSED_RE = re.compile(r"elapsed=([\d.]+)s")


def parse_window(spec: str | None) -> timedelta | None:
    if not spec or spec == "all":
        return None
    m = re.fullmatch(r"(\d+)([dhm])", spec.strip().lower())
    if not m:
        raise SystemExit(f"--window must be Nd/Nh/Nm or 'all', got {spec!r}")
    n, unit = int(m.group(1)), m.group(2)
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def parse_log(window: timedelta | None) -> list[dict]:
    """Parse log into structured events. Each event: {ts, kind, fmt, raw, ...}.

    kind ∈ {start, ok, no_match, empty, error, usage, other}
    """
    if not LOG_PATH.exists():
        return []
    cutoff = (datetime.now(tz=timezone.utc) - window) if window else None
    events = []
    for line in LOG_PATH.read_text().splitlines():
        m = TS_RE.match(line)
        if not m:
            continue
        ts_str, body = m.group(1), m.group(2)
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if cutoff and ts < cutoff:
            continue

        ev = {"ts": ts, "ts_str": ts_str, "raw": body}
        fmt_m = FMT_RE.search(body)
        ev["fmt"] = fmt_m.group(1) if fmt_m else None

        if body.startswith("START"):
            ev["kind"] = "start"
        elif body.startswith("OK"):
            ev["kind"] = "ok"
            ev["kw"] = (KW_RE.search(body) or [None, None])[1] if KW_RE.search(body) else None
            nm = NAMES_RE.search(body)
            ev["names"] = [s for s in nm.group(1).split(",") if s] if nm else []
            pm = PICKED_RE.search(body)
            ev["picked"] = int(pm.group(1)) if pm else None
            em = ELAPSED_RE.search(body)
            ev["elapsed"] = float(em.group(1)) if em else None
        elif body.startswith("no match"):
            ev["kind"] = "no_match"
        elif body.startswith("picked but"):
            ev["kind"] = "empty"
        elif body.startswith("bumped usage"):
            ev["kind"] = "usage"
        elif "failed" in body or "ERR" in body or "Timeout" in body:
            ev["kind"] = "error"
        else:
            ev["kind"] = "other"
        events.append(ev)
    return events


def attribute_fmt(events: list[dict]) -> None:
    """Some OK/outcome lines lack fmt= (logged before the fmt-logging change).
    Carry the fmt forward from the most recent START line so they're grouped
    correctly. Mutates events in place."""
    last_fmt = "claude"  # pre-fmt-logging era was claude-only
    for ev in events:
        if ev["kind"] == "start" and ev["fmt"]:
            last_fmt = ev["fmt"]
        if not ev.get("fmt"):
            ev["fmt"] = last_fmt


def summarize_by_tool(events: list[dict]) -> dict:
    by_tool: dict[str, dict] = defaultdict(lambda: {
        "starts": 0, "ok": 0, "no_match": 0, "empty": 0, "error": 0,
        "last_ok_ts": None, "last_ok_names": [], "last_ok_kw": None,
        "last_any_ts": None, "last_error_raw": None,
    })
    for ev in events:
        fmt = ev.get("fmt") or "claude"
        t = by_tool[fmt]
        t["last_any_ts"] = ev["ts"]
        if ev["kind"] == "start":
            t["starts"] += 1
        elif ev["kind"] == "ok":
            t["ok"] += 1
            t["last_ok_ts"] = ev["ts"]
            t["last_ok_names"] = ev.get("names", [])
            t["last_ok_kw"] = ev.get("kw")
        elif ev["kind"] == "no_match":
            t["no_match"] += 1
        elif ev["kind"] == "empty":
            t["empty"] += 1
        elif ev["kind"] == "error":
            t["error"] += 1
            t["last_error_raw"] = ev["raw"]
    return dict(by_tool)


def health_verdict(tool: dict, now: datetime) -> tuple[str, str]:
    """Return (emoji, reason)."""
    if tool["error"] > 0 and tool["ok"] == 0:
        return "🔴", f"only errors, never succeeded ({tool['error']} errors)"
    last_ok = tool["last_ok_ts"]
    if last_ok is None:
        if tool["no_match"] > 0:
            return "🟡", "fires but never matches (no canonical/obs for these cwds)"
        return "⚪", "no successful fire on record"
    age = now - last_ok
    if age <= timedelta(hours=FRESH_HOURS):
        return "🟢", f"last success {fmt_age(age)} ago"
    if age <= timedelta(hours=STALE_HOURS):
        return "🟡", f"last success {fmt_age(age)} ago (getting stale)"
    return "🔴", f"last success {fmt_age(age)} ago (stale — may be broken)"


def fmt_age(td: timedelta) -> str:
    s = int(td.total_seconds())
    if s < 3600:
        return f"{s//60}m"
    if s < 86400:
        return f"{s//3600}h"
    return f"{s//86400}d"


def render(by_tool: dict, events: list[dict], window_spec: str, tail: int) -> str:
    now = datetime.now(tz=timezone.utc)
    L: list[str] = []
    p = L.append

    p(f"# kg-hub PUSH Status — {now.isoformat()}")
    p("")
    p(f"Window: {window_spec or 'all'} · Log: `data/.push_hook.log` · "
      f"{sum(t['starts'] for t in by_tool.values())} total fires across "
      f"{len(by_tool)} tool(s)")
    p("")

    if not by_tool:
        p("⚠️ **No PUSH events in window.** Either the hook has never fired, "
          "or the window is too narrow. If you expect activity, check that "
          "the SessionStart hooks are still registered (~/.claude/settings.json, "
          ".cursor/hooks.json).")
        return "\n".join(L)

    p("## Per-tool health")
    p("")
    p("| Tool | Health | Fires | OK | NoMatch | Err | Last referenced |")
    p("|---|---|---|---|---|---|---|")
    for fmt in sorted(by_tool):
        t = by_tool[fmt]
        emoji, reason = health_verdict(t, now)
        names = t["last_ok_names"]
        ref = ", ".join(
            (n[len("kg-hub-canonical-"):] if n.startswith("kg-hub-canonical-")
             else "obs-" + n[len("claude-mem-obs-"):] if n.startswith("claude-mem-obs-")
             else n)
            for n in names[:3]
        ) or "—"
        p(f"| {fmt} | {emoji} | {t['starts']} | {t['ok']} | {t['no_match']} | "
          f"{t['error']} | {ref} |")
    p("")

    p("## Verdict detail")
    p("")
    for fmt in sorted(by_tool):
        t = by_tool[fmt]
        emoji, reason = health_verdict(t, now)
        p(f"- **{fmt}** {emoji} — {reason}")
        if t["last_ok_ts"]:
            p(f"  - last success: {t['last_ok_ts'].isoformat()} "
              f"(kw={t['last_ok_kw']}, referenced {len(t['last_ok_names'])} episodes)")
        if t["last_error_raw"]:
            p(f"  - last error: `{t['last_error_raw'][:100]}`")
    p("")

    # Black-box guard interpretation
    p("## What to do")
    p("")
    any_red = any(health_verdict(t, now)[0] == "🔴" for t in by_tool.values())
    any_stale = any(health_verdict(t, now)[0] == "🟡" for t in by_tool.values())
    if any_red:
        p("🔴 **A tool has stopped or is erroring.** Likely causes:")
        p("- FalkorDB down → `docker ps | grep falkordb`; restart if needed")
        p("- Hook deregistered → check that tool's hooks config still calls kg_push_hook.py")
        p("- Tool upgraded and changed its hook payload/schema")
    elif any_stale:
        p("🟡 **A tool hasn't fired recently.** This is normal if you simply "
          "haven't opened sessions in that tool. Only investigate if you HAVE "
          "been using it and still see no fires.")
    else:
        p("🟢 **All tools healthy.** PUSH is firing and referencing content "
          "across every registered runtime.")
    p("")

    if tail > 0:
        p(f"## Last {tail} raw events")
        p("")
        p("```")
        for ev in events[-tail:]:
            p(f"{ev['ts_str']}  {ev['raw']}")
        p("```")

    return "\n".join(L)


def to_json(by_tool: dict) -> dict:
    now = datetime.now(tz=timezone.utc)
    out = {}
    for fmt, t in by_tool.items():
        emoji, reason = health_verdict(t, now)
        out[fmt] = {
            "health": emoji, "reason": reason,
            "fires": t["starts"], "ok": t["ok"],
            "no_match": t["no_match"], "error": t["error"],
            "last_ok": t["last_ok_ts"].isoformat() if t["last_ok_ts"] else None,
            "last_referenced": t["last_ok_names"],
            "last_kw": t["last_ok_kw"],
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default=None, help="Nd/Nh/Nm or 'all' (default: all)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tail", type=int, default=0, help="show last N raw events")
    args = ap.parse_args()

    window = parse_window(args.window)
    events = parse_log(window)
    attribute_fmt(events)
    by_tool = summarize_by_tool(events)

    if args.json:
        print(json.dumps(to_json(by_tool), ensure_ascii=False, indent=2))
        return 0

    print(render(by_tool, events, args.window, args.tail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
