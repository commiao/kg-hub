#!/usr/bin/env python3
"""
sync_openclaw.py — Phase 2 full-snapshot sync from OpenClaw VPS into kg-hub.

Pipeline (one shot per run):
  1. ssh + tar over Tailscale: stream notes/ + memory/ from /home/admin/clawd/
     on VPS, untar locally into data/openclaw-live/.
  2. invoke ingesters.openclaw_capsule against that dir.
     - the ingester's existing sha256 watermark at data/.ingested.json skips
       capsules unchanged since the last run, so re-pull is cheap on the
       LLM-extraction side.

Why tar | ssh and not rsync:
  * VPS doesn't have rsync; we'd need apt install on VPS to use it.
  * Whole working set is ~8.5 MB (439 .md files). Network cost of full pull
    is trivial over Tailscale; the watermark gates the expensive LLM step.

Constraints honored:
  * VPS read-only — we only `tar -c` (read), never write back (openclaw decision 1).
  * Nested historical *.tar.gz on VPS are pulled as-is and NOT re-extracted —
    they're archived data, not the active capsule working set.
  * No launchd auto-install — this is a CLI tool. Wire up cron/launchd only
    after user approval (kg-hub ROADMAP rule).
  * Watermark only resets on --fresh — bare runs preserve incremental state.

Usage:
    python sync_openclaw.py                # pull + ingest (incremental via watermark)
    python sync_openclaw.py --dry-run      # list what would be pulled; no fetch, no ingest
    python sync_openclaw.py --no-ingest    # pull only, skip ingest
    python sync_openclaw.py --fresh        # wipe FalkorDB graph + watermark, full re-ingest

Note on Phase 1 → Phase 2 watermark continuity: the ingester keys files by
their path *relative to the snapshot dir*. Phase 1 snapshot dir was
`data/openclaw-snapshot-2026-05-14/`; Phase 2 dir is `data/openclaw-live/`.
Files that exist at the same relative path in both dirs (e.g.
`notes/capsules/foo.md`) with the same sha256 will be skipped — most of
the 31 Phase-1 capsules carry over. Only the 5 capsules that landed at
weird recursive-untar paths in Phase 1 will re-ingest at clean paths.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path


KG_HUB_DIR = Path(__file__).resolve().parent
LIVE_DIR = KG_HUB_DIR / "data" / "openclaw-live"

VPS_HOST = "admin@oc-vps-aliyun-us"  # actual Tailscale MagicDNS name (openclaw README shorthand "oc-vps" doesn't resolve)
VPS_BASE = "/home/admin/clawd"                # tar -C base on VPS
# Subdirs to pull. Discovered 2026-05-15: capsule .md files live in 4 top-level
# dirs (notes/, plans/, reports/, capsules/). memory/ has none currently but
# Phase 1 archive extraction landed historical ones under memory/archive/, so
# include it to preserve that hierarchy for any future archive-unpack flow.
VPS_DIRS = ["notes", "memory", "plans", "reports", "capsules"]

SSH_CONNECT_TIMEOUT = 10                       # seconds; bail fast if VPS unreachable


def pull_via_tar_ssh(dry_run: bool, verbose: bool) -> None:
    """
    Stream the working set from VPS using tar over ssh.

    Equivalent shell:
        ssh -o ConnectTimeout=10 admin@oc-vps-aliyun-us \\
            'cd /home/admin/clawd && tar -czf - notes memory' \\
          | tar -xzf - -C data/openclaw-live

    --dry-run uses `tar -tzf -` (list mode) so we see what would land without
    actually writing to disk. Nested *.tar.gz on the VPS are pulled as
    archive bytes — we do NOT recursively extract them locally (they're
    historical archives, not the active capsule working set).
    """
    LIVE_DIR.mkdir(parents=True, exist_ok=True)

    remote_dirs = " ".join(VPS_DIRS)
    remote_cmd = f"cd {VPS_BASE} && tar -czf - {remote_dirs}"

    ssh_cmd = [
        "ssh",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o", "BatchMode=yes",                # fail fast if creds missing
        VPS_HOST,
        remote_cmd,
    ]

    # Local consumer: extract or list
    if dry_run:
        local_cmd = ["tar", "-tzf", "-"]      # list contents to stdout
    else:
        local_cmd = ["tar", "-xzf", "-", "-C", str(LIVE_DIR)]
        if verbose:
            local_cmd.insert(1, "-v")

    print(f"[ssh ] {' '.join(ssh_cmd)}")
    print(f"[tar ] {' '.join(local_cmd)}")

    # Pipe: ssh | tar
    ssh_proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE)
    tar_proc = subprocess.Popen(local_cmd, stdin=ssh_proc.stdout)
    if ssh_proc.stdout is not None:
        ssh_proc.stdout.close()  # let SIGPIPE propagate to ssh if tar dies
    tar_rc = tar_proc.wait()
    ssh_rc = ssh_proc.wait()

    if ssh_rc != 0:
        raise SystemExit(f"ssh+tar pull failed: ssh rc={ssh_rc}")
    if tar_rc != 0:
        raise SystemExit(f"ssh+tar pull failed: local tar rc={tar_rc}")

    # Count what landed (skip during dry-run since nothing was extracted).
    if not dry_run:
        md_count = sum(1 for _ in LIVE_DIR.rglob("*.md"))
        size_mb = sum(p.stat().st_size for p in LIVE_DIR.rglob("*") if p.is_file()) / (1024 * 1024)
        print(f"[done] {md_count} .md files in {LIVE_DIR} ({size_mb:.1f} MB total)")


async def run_ingester(fresh: bool) -> int:
    """Invoke ingesters.openclaw_capsule via subprocess so its argparse/asyncio
    runtime stays isolated from this script's. Returns its exit code."""
    venv_py = KG_HUB_DIR / "spike-graphiti" / ".venv" / "bin" / "python"
    if not venv_py.is_file():
        raise SystemExit(f"venv python not found at {venv_py}")

    cmd = [
        str(venv_py),
        "-m",
        "ingesters.openclaw_capsule",
        "--snapshot",
        str(LIVE_DIR),
    ]
    if fresh:
        cmd.append("--fresh")
    print(f"[ingest] {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(KG_HUB_DIR),
    )
    await proc.wait()
    return proc.returncode or 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="rsync --dry-run; no ingest")
    ap.add_argument("--no-ingest", action="store_true", help="rsync only, skip ingest step")
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="wipe FalkorDB graph + watermark; full re-ingest after rsync",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="verbose rsync output")
    args = ap.parse_args()

    pull_via_tar_ssh(dry_run=args.dry_run, verbose=args.verbose)

    if args.dry_run:
        print("[sync] --dry-run set; skipping ingest")
        return 0
    if args.no_ingest:
        print("[sync] --no-ingest set; pull complete, ingest skipped")
        return 0

    return await run_ingester(fresh=args.fresh)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
