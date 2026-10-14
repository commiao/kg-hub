#!/usr/bin/env python3
"""每周日北京时间 09:00 在 NAS 上跑一次 kg-hub 周报（T-0269），由 weekly_report 容器常驻调度。

为什么搬到 NAS：周报原是 Mac launchd 作业。2026-09-20 Mac 作业改跑发布产物后它找不到
解释器，9/27 起每周 FATAL；而 Mac 上本来就没有 FalkorDB 凭据，决策日志也只在 NAS 的
refinery 状态目录里——更早那几周它把 FalkorDB 地址回退成 127.0.0.1，以「NAS 不可达」
静默跳过。最后一份报告停在 2026-08-23，七周没人发现。数据和凭据都在 NAS，就在 NAS 跑。

调度规则：
- 每周一个时间点：周 KG_HUB_REPORT_WEEKDAY（0=周一 … 6=周日，默认 6）的 KG_HUB_REPORT_AT
  （默认 09:00）。时间点用那天的日期标识，记在 <报告目录>/.scheduler-status.json 的 last_slot。
- 启动或醒来时，最近一个已到点的时间点还没跑过、且过点不超过 KG_HUB_REPORT_CATCHUP_HOURS
  （默认 24）小时，就立即补跑——发布或重建恰好压在周日上午时不至于漏一周。过点更久的
  不补，等下一个：首次上线不会在工作日白天突然跑一轮全图统计。
- 每个时间点只跑一次，成败都记账，失败不重试（周报漏一次不是故障，下周照常）。
- 子进程超过 KG_HUB_REPORT_TIMEOUT 秒（默认 3600）就停；容器收到 SIGTERM（发布、重建）
  也先停子进程再退出。停法：SIGTERM，宽限期后 SIGKILL，并等它确实退出。
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

BEIJING = timezone(timedelta(hours=8))   # 中国无夏令时，固定偏移即可
REPORT_DIR = Path.home() / ".kg-hub" / "reports"   # 与各子报告的输出目录一致
STATUS_NAME = ".scheduler-status.json"
log = logging.getLogger("kg_hub.weekly_report_scheduler")


def latest_slot(now: datetime, at: str = "09:00", weekday: int = 6) -> datetime:
    """不晚于 now 的最近一个时间点（北京时间 weekday 的 at），返回 UTC。"""
    hour, minute = (int(x) for x in at.split(":"))
    local = now.astimezone(BEIJING)
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    slot -= timedelta(days=(local.weekday() - weekday) % 7)
    if slot > local:
        slot -= timedelta(days=7)
    return slot.astimezone(timezone.utc)


def slot_id(slot: datetime) -> str:
    return slot.astimezone(BEIJING).date().isoformat()


def due_slot(now: datetime, last_slot: str | None, *, at: str = "09:00",
             weekday: int = 6, catchup_hours: float = 24.0) -> str | None:
    """现在该跑的时间点标识；不该跑返回 None。"""
    slot = latest_slot(now, at, weekday)
    if slot_id(slot) == last_slot:
        return None
    if now - slot > timedelta(hours=catchup_hours):
        return None
    return slot_id(slot)


def read_status(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(path: Path, **fields) -> None:
    data = read_status(path)
    data.update(fields, updated_at=datetime.now(timezone.utc).isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tmp.replace(path)


class _Stop:
    def __init__(self) -> None:
        self.requested = False

    def __call__(self, *_):
        self.requested = True


def _stop_child(child: subprocess.Popen, grace: float) -> int:
    child.terminate()
    try:
        return child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        child.kill()
        return child.wait()


def run_once(cmd: list[str], *, timeout: float, stop: _Stop,
             poll: float = 1.0, grace: float = 30.0) -> tuple[int, str]:
    """跑一次周报，返回 (rc, result)；result 为 done / failed / timeout / stopped。"""
    started = time.monotonic()
    child = subprocess.Popen(cmd)
    while True:
        try:
            rc = child.wait(timeout=poll)
            return rc, ("done" if rc == 0 else "failed")
        except subprocess.TimeoutExpired:
            pass
        if stop.requested:
            return _stop_child(child, grace), "stopped"
        if time.monotonic() - started > timeout:
            return _stop_child(child, grace), "timeout"


def tick(now: datetime, status_path: Path, cmd: list[str], *, stop: _Stop,
         at: str, weekday: int, catchup_hours: float, timeout: float) -> str | None:
    """做一次「该不该跑」的判断，该跑就跑完并记账。返回本次跑的时间点标识。"""
    slot = due_slot(now, read_status(status_path).get("last_slot"),
                    at=at, weekday=weekday, catchup_hours=catchup_hours)
    if slot is None:
        return None
    log.info("[weekly] slot %s: start %s", slot, " ".join(cmd))
    t0 = time.monotonic()
    write_status(status_path, state="running", running_slot=slot)
    rc, result = run_once(cmd, timeout=timeout, stop=stop)
    seconds = round(time.monotonic() - t0, 1)
    last = {"slot": slot, "rc": rc, "result": result, "seconds": seconds,
            "at": datetime.now(timezone.utc).isoformat()}
    if result == "stopped":
        # 被发布打断不算跑过：重建后的新容器还在补跑窗口内，会接着跑这一个时间点。
        write_status(status_path, state="waiting", running_slot=None, last=last)
    else:
        write_status(status_path, state="waiting", running_slot=None, last=last, last_slot=slot)
    log.info("[weekly] %s", json.dumps(last, ensure_ascii=False))
    return slot


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    at = os.environ.get("KG_HUB_REPORT_AT", "09:00")
    weekday = int(os.environ.get("KG_HUB_REPORT_WEEKDAY", "6"))
    catchup = float(os.environ.get("KG_HUB_REPORT_CATCHUP_HOURS", "24"))
    timeout = float(os.environ.get("KG_HUB_REPORT_TIMEOUT", "3600"))
    status_path = REPORT_DIR / STATUS_NAME
    cmd = [sys.executable, "-m", "tools.weekly_report"]
    stop = _Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    announced = None
    while not stop.requested:
        now = datetime.now(timezone.utc)
        tick(now, status_path, cmd, stop=stop, at=at, weekday=weekday,
             catchup_hours=catchup, timeout=timeout)
        nxt = (latest_slot(now, at, weekday) + timedelta(days=7)).isoformat()
        if nxt != announced:
            # 只在变化时写：NAS 正在治理写盘（T-0234），不为一个不变的值每分钟落一次盘。
            write_status(status_path, state="waiting", next_run=nxt)
            log.info("[weekly] next run %s", nxt)
            announced = nxt
        # 每分钟醒一次：时间点到了立刻跑，也能及时响应 SIGTERM。
        for _ in range(60):
            if stop.requested:
                break
            time.sleep(1)
    log.info("[weekly] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
