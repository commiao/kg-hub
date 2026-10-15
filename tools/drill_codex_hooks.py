#!/usr/bin/env python3
"""Run a repeatable Codex hook alert drill against the deployed NAS watchdog.

Default: isolated dry-run (no Feishu messages). --live: exactly one synthetic
fault and one recovery notification, both marked 演练. Never edits real hooks,
submits topology, touches capture databases, or invokes watchdog.main().
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

RECEIPT_PREFIX = "HOOK_DRILL_RECEIPT "
DEFAULT_HOST = "commiao@100.123.208.32"
DOCKER = "/var/packages/ContainerManager/target/usr/bin/docker"


def ssh_command(host, container, drill_id, live):
    # Host is a destination, never an ssh option or an injected remote command.
    if not re.fullmatch(r"[A-Za-z0-9_@.:-]+", host) or host.startswith("-"):
        raise ValueError("invalid SSH host")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
        raise ValueError("invalid container name")
    remote = ["sudo", "-n", DOCKER, "exec", "-i", container,
              "python", "-u", "-", "--drill-id", drill_id]
    if live:
        remote.append("--live")
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if os.environ.get("KG_HUB_SSH_ALLOW_PROXYJUMP") != "1":
        command += ["-o", "ProxyJump=none"]
    return command + [host, shlex.join(remote)]


def redact(text):
    # Remote diagnostics must not expose webhook URLs or transport tokens.
    return re.sub(r"https?://[^\s\"'<>]+", "[URL redacted]", text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="发送两条标注演练的飞书通知；默认不发送")
    parser.add_argument("--ssh-host", default=os.environ.get("KG_HUB_NAS_SSH", DEFAULT_HOST))
    parser.add_argument("--container", default="kg-hub-watchdog")
    parser.add_argument("--output-dir", type=Path,
                        default=Path.home() / ".kg-hub/state/drills",
                        help="本地回执父目录；每次创建独立子目录，不复用旧状态")
    args = parser.parse_args(argv)
    drill_id = "HOOK-DRILL-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12]
    try:
        command = ssh_command(args.ssh_host, args.container, drill_id, args.live)
    except ValueError as exc:
        parser.error(str(exc))
    output = args.output_dir.expanduser() / drill_id
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    output.chmod(0o700)
    log_path, receipt_path = output / "run.log", output / "receipt.json"
    proof = {"schema_version": 1, "drill_id": drill_id, "ok": False,
             "mode": "live" if args.live else "dry-run", "stage": "transport",
             "delivery_status": "unknown", "automatic_retry": False}
    log = ""
    try:
        source = Path(__file__).with_name("codex_hook_drill_runner.py").read_text(encoding="utf-8")
        proof["runner_sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        result = subprocess.run(command, input=source, text=True, encoding="utf-8",
                                errors="replace", capture_output=True, timeout=120, check=False)
        log = result.stdout + result.stderr
        receipts = [json.loads(line[len(RECEIPT_PREFIX):]) for line in result.stdout.splitlines()
                    if line.startswith(RECEIPT_PREFIX)]
        if (len(receipts) != 1 or receipts[0].get("drill_id") != drill_id
                or receipts[0].get("mode") != proof["mode"]):
            raise RuntimeError("missing or mismatched remote receipt")
        proof = dict(receipts[0], runner_sha256=proof["runner_sha256"])
        if result.returncode != 0:
            proof.update(ok=False, transport_exit_code=result.returncode)
    except subprocess.TimeoutExpired as exc:
        log = "".join(part.decode("utf-8", errors="replace") if isinstance(part, bytes)
                      else (part or "") for part in (exc.stdout, exc.stderr))
        proof["error_type"] = "TimeoutExpired"
    except Exception as exc:
        proof["error_type"] = type(exc).__name__
    finally:
        log_path.write_text(redact(log), encoding="utf-8")
        receipt_path.write_text(redact(json.dumps(proof, ensure_ascii=False, indent=2)) + "\n",
                                encoding="utf-8")
        log_path.chmod(0o600)
        receipt_path.chmod(0o600)
    status = "通过" if proof["ok"] else "失败（勿自动重跑，先核对回执）"
    print(f"{proof['mode']} {status}；回执：{receipt_path}")
    if proof["ok"] and not args.live:
        print("仅验证隔离状态机和去重，未发送飞书；真实通道验证需显式 --live。")
    return 0 if proof["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
