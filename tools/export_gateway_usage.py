#!/usr/bin/env python3
"""把 model-gateway 的用量历史导成一份 JSON，供 kg-hub 面板渲染成本卡片。

为什么需要这一层，而不是让面板直接查库
--------------------------------------
唯一有历史的数据源是回滚见证库（`daily_counts` 按天累计、`attempts` 带 ISO
时间戳）。但网关把见证不可用当**致命**处理：`_validate_state_anchor` 失败即
`ConfigurationError` → 对外 503「网关本地配置不可用」。也就是说任何在请求路径上
碰这个库的读者，一旦短暂持锁就可能把整条链路打成 503（T-0046 里已经实测过它
有多敏感）。

所以这里**先复制文件、再读副本**：与活库零锁交互，代价是快照可能撕裂——对报表
可以接受，且读不出来就跳过本轮、不写坏文件。

网关自己的 `runtime/state/usage/*.json` 不能用：它只有**当天**计数和一个分钟
滑窗，没有历史，做不出月/日/时曲线。

输出契约（与面板约定）
----------------------
    {
      "generated_at": "...", "witness_deployment_id": "...",
      "daily":  [{"day": "2026-09-03", "business_key": "...", "count": 954}, ...],
      "hourly": [{"hour": "2026-09-03T08", "business_key": "...", "count": 12}, ...],
      "monthly":[{"month": "2026-09", "business_key": "...", "count": 1549}, ...],
      "totals": {"<business_key>": 1549, ...},
      "ceilings": {"<business_key>": {"daily_requests": 120000, "requests_per_minute": 60}},
      "window": {"hourly_hours": 72, "daily_days": 62}
    }

小时数据来自 `attempts`；它是单调追加的，解决 provider-status 未决标记不影响它。

副本自洽性
----------
网关每个付费请求都写见证库，`shutil.copy2` 有很大概率正好落在写事务中间，得到一份
读不出任何表的坏副本（2026-09-07 实测 5 次里 3 次）。所以这里「复制→自检」重试若干
次；必需的表读失败一律抛 `TornSnapshot`，由 `main()` 保留上一份快照——
**绝不写出一份"结构合法但全空"的快照**，那会让面板说「今日无调用」这种假话。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_WITNESS = Path("/volume1/docker/model-gateway-witness/rollback-witness.sqlite3")
DEFAULT_OUT = Path("/volume2/4T/kg-hub-data/gateway-usage/usage.json")
HOURLY_HOURS = 72
DAILY_DAYS = 62


def _copy_for_read(source: Path, into: Path, attempt: int = 0) -> Path:
    """复制活库后再读。见模块 docstring：绝不在活库上持锁。"""
    target = into / f"witness-{attempt}.sqlite3"
    shutil.copy2(source, target)
    # WAL/日志旁文件若存在也一并带上，否则副本可能读不出最近写入。
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target.with_name(target.name + suffix))
    return target


class TornSnapshot(RuntimeError):
    """副本落在活库的一次写事务中间，读不出完整数据。"""


def _rows(database: sqlite3.Connection, sql: str, *,
          required: bool = False) -> list[tuple]:
    """required=True 的表读失败必须抛错，**绝不能静默返回空**。

    2026-09-07 实测:连续 5 次导出有 3 次全表读空——网关每个付费请求都写见证库,
    写事务期间主库文件本身不自洽,`shutil.copy2` 有很大概率正好复制到事务中间,
    于是每条查询都抛 DatabaseError。此前这里把错误吞掉返回 [],结果写出一份
    "结构合法但全空"的快照,拓扑面板据此显示「今日无 kg-hub 调用」——
    一个监控通路说了假话,正是这套系统反复栽的那个跟头。
    """
    try:
        return list(database.execute(sql))
    except sqlite3.Error as exc:
        if required:
            raise TornSnapshot(f"见证库副本不可读: {exc}") from exc
        return []


def _open_consistent_copy(witness: Path, staging: Path, *,
                          attempts: int = 6, delay_s: float = 0.7):
    """反复「复制→自检」直到拿到一份自洽副本；始终不碰活库的锁。

    自检只读两张恒非空的小表(witness_meta / cost_policy_ceiling)——足以判断副本是否
    完整，又不像 PRAGMA quick_check 那样扫全库。单次成功率实测约 40%，6 次≈99%；
    全部失败就抛错，由 main() 保留上一份快照(宁可旧，不可错)。
    """
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay_s)
        try:
            copy = _copy_for_read(witness, staging, attempt)
            database = sqlite3.connect(f"file:{copy}?mode=ro", uri=True, timeout=10)
        except (OSError, sqlite3.Error) as exc:
            last = exc
            continue
        try:
            meta = _rows(database, "SELECT deployment_id FROM witness_meta LIMIT 1",
                         required=True)
            ceiling = _rows(
                database,
                "SELECT business_key,daily_requests,requests_per_minute "
                "FROM cost_policy_ceiling", required=True)
            if not meta or not ceiling:
                raise TornSnapshot("见证库副本缺 witness_meta / cost_policy_ceiling")
        except (TornSnapshot, sqlite3.Error) as exc:
            database.close()
            last = exc
            continue
        return database, meta, ceiling
    raise TornSnapshot(f"{attempts} 次复制均未取到自洽副本: {last}")


def collect(witness: Path) -> dict:
    now = datetime.now(tz=timezone.utc)
    with tempfile.TemporaryDirectory() as staging:
        database, deployment, ceiling_raw = _open_consistent_copy(
            witness, Path(staging))
        try:
            daily_raw = _rows(
                database,
                "SELECT day,business_key,attempt_count FROM daily_counts "
                "ORDER BY day, business_key", required=True)
            # 只取小时窗内的 attempts。这张表单调追加(约 2000 行/天),不设下界
            # 会让导出开销随历史线性增长——而本报表只画最近 HOURLY_HOURS 小时。
            # at 是 ISO8601,字典序与时序一致,可直接比较。
            hourly_floor_iso = (
                now - timedelta(hours=HOURLY_HOURS)).isoformat()
            attempts_raw = _rows(
                database,
                "SELECT at,business_key FROM attempts "
                f"WHERE at >= '{hourly_floor_iso}'", required=True)
        finally:
            database.close()

    daily_floor = (now - timedelta(days=DAILY_DAYS)).strftime("%Y-%m-%d")
    daily = [
        {"day": str(day), "business_key": str(key), "count": int(count)}
        for day, key, count in daily_raw
        if str(day) >= daily_floor and isinstance(count, int)
    ]

    monthly_acc: dict[tuple[str, str], int] = defaultdict(int)
    totals: dict[str, int] = defaultdict(int)
    for item in daily:
        monthly_acc[(item["day"][:7], item["business_key"])] += item["count"]
        totals[item["business_key"]] += item["count"]

    hourly_floor = now - timedelta(hours=HOURLY_HOURS)
    hourly_acc: dict[tuple[str, str], int] = defaultdict(int)
    for at, key in attempts_raw:
        try:
            moment = datetime.fromisoformat(str(at))
        except ValueError:
            continue          # 坏行不该让整份报表失败
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment < hourly_floor:
            continue
        hourly_acc[(moment.strftime("%Y-%m-%dT%H"), str(key))] += 1

    return {
        "generated_at": now.isoformat(),
        "witness_deployment_id": (str(deployment[0][0]) if deployment else None),
        "daily": daily,
        "monthly": [
            {"month": month, "business_key": key, "count": count}
            for (month, key), count in sorted(monthly_acc.items())
        ],
        "hourly": [
            {"hour": hour, "business_key": key, "count": count}
            for (hour, key), count in sorted(hourly_acc.items())
        ],
        "totals": dict(sorted(totals.items())),
        "ceilings": {
            str(key): {"daily_requests": int(daily), "requests_per_minute": int(rpm)}
            for key, daily, rpm in ceiling_raw
            if isinstance(daily, int) and isinstance(rpm, int)
        },
        "window": {"hourly_hours": HOURLY_HOURS, "daily_days": DAILY_DAYS},
    }


def write_atomic(payload: dict, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 面板随时可能在读；先写临时文件再 rename，读者永远看到完整 JSON。
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent, delete=False)
    try:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, destination)
    destination.chmod(0o644)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--witness", type=Path, default=DEFAULT_WITNESS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--print", action="store_true", help="同时打印摘要")
    args = parser.parse_args(argv)

    if not args.witness.exists():
        print(f"witness not found: {args.witness}", file=sys.stderr)
        return 2
    try:
        payload = collect(args.witness)
    except Exception as exc:  # noqa: BLE001
        # 读不出来就保留上一轮的 usage.json —— 报表宁可旧，不可错。
        print(f"collect failed, keeping previous snapshot: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    write_atomic(payload, args.out)
    if args.print:
        print(json.dumps({
            "generated_at": payload["generated_at"],
            "totals": payload["totals"],
            "daily_rows": len(payload["daily"]),
            "hourly_rows": len(payload["hourly"]),
            "monthly_rows": len(payload["monthly"]),
        }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
