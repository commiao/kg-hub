"""tools/capsule_watch.py — track canonical capsule usage recovery post ranking-fix.

After the 2026-06-18 ranking change (relevance + global scope + exploration slot,
see capsule-usage-audit-2026-06-18.md), four canonical capsules were starved at
usage==0. This watcher fetches live usage_ranking from the kg-hub server, diffs
against the saved baseline, and reports whether those starved capsules have begun
accumulating usage — the real proof the fix "keeps working", not just "went live".

Read-only against the graph (HTTP). Writes a dated report + a last-state file for
change detection, and (optional) pushes a compact summary to Feishu.

Usage:
  python -m tools.capsule_watch                          # stdout + report file
  python -m tools.capsule_watch --feishu                 # also push summary to Feishu (kg-hub)
  python -m tools.capsule_watch --feishu --only-on-change # push only when usage moved (for cron)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

REPORTS = Path.home() / ".kg-hub" / "reports"
STATE = Path.home() / ".kg-hub" / "state" / "capsule-watch.last.json"
BASELINE = REPORTS / "capsule-baseline-2026-06-19.json"
FEISHU_SEND = Path.home() / ".claude" / "skills" / "feishu-notify" / "scripts" / "send.py"


def fetch_usage() -> dict[str, int]:
    base = (os.environ.get("KG_HUB_URL") or "http://127.0.0.1:8080").rstrip("/")
    tok = os.environ.get("KG_HUB_API_TOKEN") or ""
    req = urllib.request.Request(
        f"{base}/api/usage_ranking?top_n=50",
        headers={"Authorization": f"Bearer {tok}"} if tok else {},
    )
    last = None  # escalating timeouts ride out NAS cold-start / transient slowness
    for i, t in enumerate((15, 25, 35)):
        try:
            with urllib.request.urlopen(req, timeout=t) as r:
                d = json.loads(r.read())
            uc = {x["name"]: int(x["usage_count"]) for x in d.get("top_canonical", [])}
            for x in d.get("demote", []):
                uc.setdefault(x["name"], 0)
            return uc
        except Exception as exc:
            last = exc
            if i < 2:
                time.sleep(2.0)
    raise last


def short(n: str) -> str:
    return n.replace("kg-hub-canonical-", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feishu", action="store_true", help="push summary to Feishu (kg-hub webhook)")
    ap.add_argument("--only-on-change", action="store_true",
                    help="with --feishu: push only if usage changed since last run")
    args = ap.parse_args()

    base = json.loads(BASELINE.read_text())["usage"] if BASELINE.exists() else {}
    try:
        cur = fetch_usage()
    except Exception as exc:
        # Transient (NAS/tailscale blip) — a monitor missing one run is not an error;
        # exit 0 so launchd/ops doesn't flag the agent "errored" for a network hiccup.
        print(f"[capsule_watch] fetch failed (transient, skipping this run): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 0

    names = sorted(set(base) | set(cur), key=lambda n: -cur.get(n, 0))
    watch = [n for n in base if base.get(n, 0) == 0]           # starved at baseline
    recovered = [n for n in watch if cur.get(n, 0) > 0]
    still = [n for n in watch if cur.get(n, 0) == 0]

    L = [
        f"# kg-hub 胶囊观察 — {datetime.now(tz=timezone.utc).date()}",
        "基线 2026-06-19 → 现在（排序修复 2026-06-18 上线后）。🎯 = 基线被饿死的观察目标",
        "",
        "| 胶囊 | 基线 | 现在 | Δ |",
        "|---|---|---|---|",
    ]
    for n in names:
        b, c = base.get(n, 0), cur.get(n, 0)
        d = c - b
        flag = " 🎯" if n in watch else ""
        L.append(f"| {short(n)}{flag} | {b} | {c} | {'+' if d >= 0 else ''}{d} |")
    L.append("")
    if recovered:
        L.append(f"✅ 已恢复曝光（0→>0）：{', '.join(short(x) for x in recovered)}")
    if still:
        L.append(f"⏳ 仍为 0：{', '.join(short(x) for x in still)}")
    md = "\n".join(L)
    print(md)

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"capsule-watch-{datetime.now().strftime('%Y-%m-%d')}.md").write_text(md)

    changed = True
    if STATE.exists():
        try:
            changed = json.loads(STATE.read_text()).get("usage") != cur
        except Exception:
            changed = True
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(
        {"usage": cur, "at": datetime.now(tz=timezone.utc).isoformat()}, ensure_ascii=False))

    if args.feishu and (changed or not args.only_on_change):
        summary = (
            f"📊 kg-hub 胶囊排序观察 {datetime.now().strftime('%m-%d')}\n"
            f"被饿死胶囊恢复曝光 {len(recovered)}/{len(watch)}"
            + (f"：{', '.join(short(x) for x in recovered)}" if recovered else "")
            + (f"\n仍为 0：{', '.join(short(x) for x in still)}" if still else "")
        )
        try:
            subprocess.run(
                [sys.executable, str(FEISHU_SEND), summary,
                 "--webhook", "kg-hub", "--title", "kg-hub 胶囊排序观察"],
                check=False, timeout=20,
            )
        except Exception as exc:
            print(f"[feishu] skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
    elif args.feishu and args.only_on_change and not changed:
        print("[feishu] no change since last run; push skipped", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
