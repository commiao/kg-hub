#!/usr/bin/env python3
"""每晚低峰清理 model-attempts.sqlite3（T-0213），由 journal_prune 容器常驻调度。

为什么是容器：DSM 没有用户 crontab；2026-10-11/12 第一次清理靠宿主上 nohup 的
一次性脚本（~/journal-stats/prune-night.sh），不在任何仓库里，也不会每晚再跑。

每晚北京时间 KG_HUB_PRUNE_AT（默认 23:05）运行一次：

    python -m utils.journal_prune --apply --retention-days 7 --max-seconds 3600

保留 7 天是用户 2026-10-11 批准的口径。journal_prune 自己分批提交（每批一个事务、
批间停顿），从最旧的开始删；任何时候停下都安全，下一晚从剩下最旧的接着删。

看护（沿用第一次清理的规则，并修掉当时「停不下来」的缺陷）：
- 开始前：数据目录里有 CANCEL 文件就跳过本晚。
- 运行中：每 KG_HUB_PRUNE_IO_INTERVAL 秒读一次 /proc/syno_loadavg 的 1 分钟 IO 负载
  （与宿主 `uptime` 里 [IO: …] 同源），连续 4 次高于 KG_HUB_PRUNE_IO_LIMIT（默认 4）
  就停；出现 CANCEL 文件也停；容器收到 SIGTERM（发布、重建）同样先停子进程再退出。
- 停的方式：SIGTERM，宽限期后 SIGKILL，并等待确认子进程确实退出——第一次清理的
  看护用了镜像里不存在的 pkill 又吞掉了报错，「停止」之后清理照常跑完。

手工 VACUUM / 换文件之前先放 CANCEL 文件或停掉本容器：替换文件期间不能有清理在写。
状态写在 <数据目录>/status.json，日志进 docker logs。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))   # 中国无夏令时，固定偏移即可，不依赖 tzdata
log = logging.getLogger("kg_hub.nightly_prune")


def next_run(now: datetime, at: str = "23:05") -> datetime:
    """下一个北京时间 ``at``（严格晚于 now），返回 UTC。"""
    hour, minute = (int(x) for x in at.split(":"))
    local = now.astimezone(BEIJING)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def io_load(path: str | Path = "/proc/syno_loadavg") -> float | None:
    """Synology 的 1 分钟 IO 负载；读不到返回 None（不当作高负载）。"""
    try:
        return float(Path(path).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


class _Stop:
    """SIGTERM 到达时置位；运行中的清理据此先停子进程再退出。"""

    def __init__(self) -> None:
        self.requested = False

    def __call__(self, signum, frame) -> None:  # noqa: ARG002
        self.requested = True


def _stop_child(child: subprocess.Popen, grace: float) -> int:
    child.terminate()
    try:
        return child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        log.warning("[prune] child ignored SIGTERM for %.0fs; SIGKILL", grace)
        child.kill()
        return child.wait()


def run_once(cmd: list[str], *, control: Path, io_limit: float, stop: _Stop,
             streak_limit: int = 4, interval: float = 30.0, grace: float = 30.0,
             poll: float = 1.0, read_io=io_load) -> dict:
    """跑一轮清理并看护。返回 {"result": done|failed|cancelled|io_high|terminated, "rc": …}。"""
    started = time.monotonic()
    child = subprocess.Popen(cmd)
    log.info("[prune] start pid=%s: %s", child.pid, " ".join(cmd))
    streak = 0
    next_sample = time.monotonic() + interval
    while True:
        try:
            rc = child.wait(timeout=poll)
        except subprocess.TimeoutExpired:
            rc = None
        if rc is not None:
            result = "done" if rc == 0 else "failed"
            break
        reason = None
        if stop.requested:
            reason = "terminated"
        elif (control / "CANCEL").exists():
            reason = "cancelled"
        elif time.monotonic() >= next_sample:
            next_sample = time.monotonic() + interval
            load = read_io()
            streak = streak + 1 if load is not None and load > io_limit else 0
            log.info("[prune] running, IO load %s (high streak %d)",
                     "?" if load is None else f"{load:.2f}", streak)
            if streak >= streak_limit:
                reason = "io_high"
        if reason:
            log.warning("[prune] stopping (%s)", reason)
            rc = _stop_child(child, grace)
            result = reason
            break
    report = {"result": result, "rc": rc, "seconds": round(time.monotonic() - started, 1)}
    log.info("[prune] %s", json.dumps(report, sort_keys=True))
    return report


def write_status(control: Path, **fields) -> None:
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = control / "status.json.tmp"
    tmp.write_text(json.dumps(fields, ensure_ascii=False, sort_keys=True), "utf-8")
    os.replace(tmp, control / "status.json")


def command(retention_days: str, max_seconds: str) -> list[str]:
    return [sys.executable, "-m", "utils.journal_prune", "--apply",
            "--retention-days", retention_days, "--max-seconds", max_seconds]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    control = Path(os.environ.get("KG_HUB_PRUNE_DIR", "/journal-prune"))
    at = os.environ.get("KG_HUB_PRUNE_AT", "23:05")
    io_limit = float(os.environ.get("KG_HUB_PRUNE_IO_LIMIT", "4"))
    interval = float(os.environ.get("KG_HUB_PRUNE_IO_INTERVAL", "30"))
    cmd = command(os.environ.get("KG_HUB_PRUNE_RETENTION_DAYS", "7"),
                  os.environ.get("KG_HUB_PRUNE_MAX_SECONDS", "3600"))
    stop = _Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last = None
    while not stop.requested:
        due = next_run(datetime.now(timezone.utc), at)
        write_status(control, state="waiting", next_run=due.isoformat(), last=last)
        log.info("[prune] next run %s (Beijing %s)", due.isoformat(),
                 due.astimezone(BEIJING).strftime("%F %H:%M"))
        while not stop.requested and datetime.now(timezone.utc) < due:
            time.sleep(min(20.0, max(0.1, (due - datetime.now(timezone.utc)).total_seconds())))
        if stop.requested:
            break
        if (control / "CANCEL").exists():
            last = {"result": "skipped", "reason": "CANCEL file",
                    "at": datetime.now(timezone.utc).isoformat()}
            log.warning("[prune] skipped tonight: CANCEL file present")
            continue
        write_status(control, state="running", next_run=due.isoformat(), last=last)
        last = {**run_once(cmd, control=control, io_limit=io_limit, stop=stop,
                           interval=interval),
                "at": datetime.now(timezone.utc).isoformat()}
    write_status(control, state="stopped", last=last)
    return 0


if __name__ == "__main__":
    sys.exit(main())
