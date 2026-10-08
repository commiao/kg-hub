#!/usr/bin/env python3
"""Release observations the refinery holds "for reconciliation" when nothing is left to reconcile.

The refinery moves an observation to ``held`` when the server answers
needs_reconciliation, and nothing ever moves it back. On 2026-10-08 it held
5194 observations. Each was matched against the durable model journal: none
had a call with an unknown outcome. 4563 had no successful call at all — every
failed call either never reached the gateway (``absent``: external_calls 0)
or was settled as failed by it (e.g. provider 429). No paid result to reuse,
nothing written to the graph: resubmitting costs what a new observation costs.

Three steps, run in this order:

  plan        read-only, inside kg-hub-server (journal + graph + /refinery-state)
  reset-keys  inside kg-hub-server; dry run unless --apply. Re-reads the journal
              per task and deletes the IngestedKey only while it is still
              needs_reconciliation with no worker attached.
  unhold      on the NAS host **with kg-hub-refinery stopped** (it keeps the
              watermark in memory and would overwrite the edit). Removes reset
              observations from ``held``; server-confirmed ones go to ``ingested``.

Observations with successful calls are left for the manual resume path, which
reuses the paid results; this tool never touches them.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# Settled with no result: never reached the gateway, or a definitive failure.
SETTLED_WITHOUT_RESULT = {"absent", "failed"}
RESET, RESUME, UNKNOWN, ALL_COMPLETED = "reset", "resume", "unknown_outcome", "all_completed"


def classify(phases: list[str]) -> str:
    """One observation's journal phases → what may safely happen to it."""
    if not phases:
        return UNKNOWN
    completed = sum(p == "completed" for p in phases)
    rest = {p for p in phases if p != "completed"}
    if not rest:
        return ALL_COMPLETED
    if not rest <= SETTLED_WITHOUT_RESULT:
        return UNKNOWN              # prepared / http_started / unknown: may have been paid
    return RESUME if completed else RESET


def journal_rows(db, oid: int) -> list[tuple]:
    # Range on the indexed source_description prefix; never a table scan.
    lo, hi = f"claude-mem obs id={oid} ", f"claude-mem obs id={oid}!"
    return db.execute(
        "SELECT phase, source_description, source_obs_id FROM model_attempts "
        "WHERE source_description >= ? AND source_description < ? ORDER BY rowid",
        (lo, hi)).fetchall()


def journal() -> sqlite3.Connection:
    path = Path(os.environ["KG_HUB_INGEST_BACKUP_PATH"]).with_name("model-attempts.sqlite3")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


async def key_status(driver, sd: str, sid: str):
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey {source_description: $sd, source_obs_id: $sid}) "
        "RETURN k.status AS status, k.worker_state AS worker_state", sd=sd, sid=sid)
    return dict(rows[0]) if rows else None


async def plan(watermark: Path, driver) -> list[dict]:
    held = sorted(json.loads(watermark.read_text()).get("held", []))
    db = journal()
    out = []
    for oid in held:
        rows = journal_rows(db, oid)
        keys = sorted({(r[1], r[2]) for r in rows})
        item = {"oid": oid, "category": classify([r[0] for r in rows]),
                "sd": keys[0][0] if len(keys) == 1 else None,
                "sid": keys[0][1] if len(keys) == 1 else None, "server": None}
        if len(keys) > 1:
            item["category"] = UNKNOWN      # more than one task identity: leave it to a human
        elif keys:
            status = await key_status(driver, *keys[0])
            item["server"] = status["status"] if status else "absent"
        out.append(item)
    return out


async def reset_keys(items: list[dict], driver, *, limit: int, apply: bool,
                     above: int = 0) -> list[int]:
    """Delete the server claim for up to ``limit`` RESET items with oid > ``above``.

    ``above`` set to the refinery boundary selects the live line only.
    """
    db = journal()
    done = []
    for item in items:
        if len(done) >= limit:
            break
        if item["oid"] <= above:
            continue
        if item["category"] != RESET or item["server"] != "needs_reconciliation":
            continue
        rows = journal_rows(db, item["oid"])
        if classify([r[0] for r in rows]) != RESET or {(r[1], r[2]) for r in rows} != {(item["sd"], item["sid"])}:
            continue                        # the journal moved since the plan: skip, never guess
        if not apply:
            done.append(item["oid"])
            continue
        rows, _, _ = await driver.execute_query(
            "MATCH (k:IngestedKey {source_description: $sd, source_obs_id: $sid}) "
            "WHERE k.status = 'needs_reconciliation' AND k.worker_state IS NULL "
            "DELETE k RETURN count(*) AS c", sd=item["sd"], sid=item["sid"])
        if rows and rows[0].get("c") == 1:
            done.append(item["oid"])
    return done


def unhold(watermark: Path, release: list[int], ingested: list[int]) -> dict:
    """Edit the refinery watermark; the refinery must be stopped."""
    wm = json.loads(watermark.read_text())
    held = set(wm.get("held", []))
    release, ingested = set(release) & held, set(ingested) & held
    wm["held"] = sorted(held - release - ingested)
    wm["ingested"] = sorted(set(wm.get("ingested", [])) | ingested)
    tmp = watermark.with_suffix(".release-held.tmp")
    tmp.write_text(json.dumps(wm))
    os.replace(tmp, watermark)
    return {"released": len(release), "marked_ingested": len(ingested), "held_left": len(wm["held"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--watermark", type=Path, default=Path("/refinery-state/watermark.json"))
    r = sub.add_parser("reset-keys"); r.add_argument("--plan", type=Path, required=True)
    r.add_argument("--limit", type=int, required=True); r.add_argument("--apply", action="store_true")
    r.add_argument("--above", type=int, default=0, help="only oids greater than this (refinery boundary = live line)")
    u = sub.add_parser("unhold"); u.add_argument("--watermark", type=Path, required=True)
    u.add_argument("--released", type=Path, required=True); u.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    if args.cmd == "unhold":
        items = json.loads(args.plan.read_text())
        confirmed = [i["oid"] for i in items if i["server"] == "ok"]
        print(json.dumps(unhold(args.watermark, json.loads(args.released.read_text()), confirmed)))
        return 0

    import asyncio
    sys.path.insert(0, "/app")
    from kg_hub_server import get_status_driver

    async def run():
        driver = get_status_driver()
        if args.cmd == "plan":
            return await plan(args.watermark, driver)
        return await reset_keys(json.loads(args.plan.read_text()), driver,
                                limit=args.limit, apply=args.apply, above=args.above)
    print(json.dumps(asyncio.run(run()), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
