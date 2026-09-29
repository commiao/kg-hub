"""积压消化链路看板：工具 → claude-mem → refinery → kg-hub → 知识图谱。

    GET /dashboard/flow        页面：五类图 + 积压消化 + 卡点清单
    GET /dashboard/flow.json   同一份数据（排障、脚本对账用）

回答的问题只有一个：积压消化得慢，是卡在哪一段、被什么卡住。

数据全部来自已有的权威来源，这里不另立口径：
- refinery `status.json`：窗口/暂停/温度/断路、积压剩余、按小时的去向账（单位：观测条数）
- 探针拓扑快照：工具、hook、claude-mem worker、SQLite、Mac→NAS 同步
- 网关 `usage.json` + 零调用健康检查：今日调用量、有效日上限、熔断（单位：模型调用次数）
- 图内 IngestedKey / Episodic：pending 年龄、错误分类、领取→终态耗时、按日入图量
- 进程内 `utils.ingest_timing`：写锁排队与抽取耗时拆分

观测条数与模型调用次数是两个单位；调用倍数只拿两个持久计数（网关当日调用、图内当日新增）在同一 UTC 日相除。
为什么单独成文件：同 topology.py，kg_hub_server.py 常有多方并行改动。
"""
from __future__ import annotations

import json
import asyncio
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

STATE_RANK = {"green": 0, "grey": 1, "amber": 2, "red": 3}

# 服务端 pending 键的最长合法寿命：写锁等待上限 + 单次抽取上限（utils/ingest_budget）。
# 超过它还在 pending，就不是「在排队」而是卡死了。
PENDING_STUCK_S = 2400
LOCK_WAIT_SHARE_SLOW = 0.4
EXTRACT_P50_SLOW_S = 120
CALLS_PER_OBS_SLOW = 10
DEFERRED_SHARE_SLOW = 0.2
ERRORS_24H_SLOW = 10
ETA_DAYS_SLOW = 30
# 当日入图太少时比值只反映零点附近的噪声，不给数。
CALLS_RATIO_MIN_EPISODES = 10

STAGES = (
    ("tools", "工具", "Claude Code / Cursor / Codex 等 + hook 捕获动作"),
    ("claude_mem", "claude-mem", "worker 用 LLM 提炼 observation，写入 SQLite"),
    ("sync", "Mac→NAS 同步", "launchd 每 15 分钟把 db 同步到 NAS 副本"),
    ("refinery", "refinery", "质量过滤 + 积压/实时调度 + 逐条提交入图"),
    ("kghub", "kg-hub", "幂等领取 → 并发槽位 → 锁外多轮抽取 → 短写锁提交"),
    ("gateway", "模型网关", "credvault 额度/限流/熔断 → 百炼"),
    ("graph", "知识图谱", "FalkorDB：Episode / 实体 / 关系"),
)

SEVERITY_RANK = {"stop": 0, "slow": 1}
# 同为慢流时按对吞吐的影响排：原因在前，「清空要很久」是这些原因的结果，排最后。
SLOW_IMPACT_ORDER = ("写锁排队是主要耗时", "并发槽位排队是主要耗时", "单条抽取耗时高", "单条入图耗时高",
                     "每条观测模型调用次数高", "推迟/重试占比高", "每天真正干活的小时数少",
                     "近 24h 抽取失败偏多", "今日额度接近上限", "Mac→NAS 同步落后",
                     "按当前速度清空积压需要很久")


def _worst(states: list[str]) -> str:
    return max(states, key=lambda s: STATE_RANK.get(s, 1)) if states else "grey"


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))], 1)


def _pct(numerator: float, denominator: float) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


def _hour_key(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def _model_attempt_rows(since: datetime) -> list[tuple]:
    """Read the existing journal without its schema setup or write transaction."""
    backup = os.environ.get("KG_HUB_INGEST_BACKUP_PATH", "").strip()
    if not backup:
        raise FileNotFoundError("KG_HUB_INGEST_BACKUP_PATH 未配置")
    path = Path(backup).with_name("model-attempts.sqlite3")
    if not path.is_file():
        raise FileNotFoundError(str(path))
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as db:
        return db.execute(
            "SELECT http_started_at, updated_at, phase FROM model_attempts "
            "WHERE http_started_at >= ?", (since.isoformat(),)).fetchall()


def key_metric_trends(*, now: datetime, commits: list[dict],
                      attempts: list[tuple] | None,
                      outcomes: list[dict] | None) -> list[dict]:
    """Hourly UTC buckets. None means unavailable; zero means observed zero."""
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    buckets = []
    for i in range(24):
        hour = start + timedelta(hours=i)
        buckets.append({"hour": _hour_key(hour), "ingested": None, "errors": None,
                        "lock_wait_avg": None, "lock_wait_p90": None,
                        "commit_avg": None, "commit_p50": None,
                        "validate_skipped": None, "commit_attempts": None,
                        "conflicts": None, "prevalidated_conflicts": None,
                        "conflict_rate": None, "model_calls": None,
                        "call_duration_avg": None, "model_inflight_avg": None,
                        "calls_per_ingested": None})
    by_hour = {b["hour"]: b for b in buckets}
    if outcomes is not None:
        for b in buckets:
            b["ingested"] = b["errors"] = 0
        for row in outcomes:
            b = by_hour.get(str(row.get("hour")))
            if b and row.get("status") == "ok":
                b["ingested"] += int(row.get("count") or 0)
            elif b and row.get("status") in ("error", "failed", "needs_reconciliation"):
                b["errors"] += int(row.get("count") or 0)
    grouped: dict[str, list[dict]] = {}
    for row in commits:
        key = _hour_key(datetime.fromtimestamp(row["at"], tz=timezone.utc))
        if key in by_hour:
            grouped.setdefault(key, []).append(row)
    for key, rows in grouped.items():
        b = by_hour[key]
        successes = [r for r in rows if not r["conflict"]]
        waits = [r["lock_wait_s"] for r in successes]
        durations = [r["commit_s"] for r in successes if r["commit_s"] is not None]
        b["commit_attempts"] = len(rows)
        b["conflicts"] = len(rows) - len(successes)
        b["prevalidated_conflicts"] = sum(r["prevalidated_conflict"] for r in rows)
        b["conflict_rate"] = _pct(b["conflicts"], len(rows))
        b["validate_skipped"] = sum(r["validate_skipped"] for r in successes)
        if waits:
            b["lock_wait_avg"] = round(sum(waits) / len(waits), 1)
            b["lock_wait_p90"] = _percentile(waits, .9)
        if durations:
            b["commit_avg"] = round(sum(durations) / len(durations), 1)
            b["commit_p50"] = _percentile(durations, .5)
    if attempts is not None:
        for b in buckets:
            b["model_calls"] = 0
        duration_by_hour: dict[str, list[float]] = {}
        for started_raw, ended_raw, phase in attempts:
            started = _parse_ts(started_raw)
            ended = _parse_ts(ended_raw) if phase != "http_started" else None
            if not started or started > now:
                continue
            b = by_hour.get(_hour_key(started))
            if b:
                b["model_calls"] += 1
                if phase == "completed" and ended and ended >= started:
                    duration_by_hour.setdefault(b["hour"], []).append(
                        (ended-started).total_seconds())
            # Integrate call-seconds over each hour, including calls spanning boundaries.
            # An interrupted, unresolved call has no known end. Cap its
            # interval so a stale journal row cannot look in flight for days.
            if phase != "completed":
                ended = min(ended or now, started + timedelta(minutes=15))
            end = min(ended or now, now)
            if end < started:
                continue
            cursor = max(started, start)
            while cursor < end:
                hour_end = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                segment_end = min(end, hour_end)
                cell = by_hour.get(_hour_key(cursor))
                if cell:
                    cell["model_inflight_avg"] = (cell["model_inflight_avg"] or 0) + (segment_end-cursor).total_seconds()
                cursor = segment_end
        for b in buckets:
            values = duration_by_hour.get(b["hour"], [])
            if values:
                b["call_duration_avg"] = round(sum(values) / len(values), 1)
            elapsed = min(3600, max(0, (now - _parse_ts(b["hour"] + ":00:00+00:00")).total_seconds()))
            b["model_inflight_avg"] = round((b["model_inflight_avg"] or 0) / elapsed, 2) if elapsed else None
            if b["ingested"] and b["model_calls"] is not None:
                b["calls_per_ingested"] = round(b["model_calls"] / b["ingested"], 1)
    return buckets


# ---------- 各段状态 ----------

def probe_stages(snapshots: list[dict]) -> dict[str, dict]:
    """从探针快照里取前半段（工具 / claude-mem / 同步）的节点，按段合并成最坏状态。"""
    groups: dict[str, list[tuple[dict, dict]]] = {"tools": [], "claude_mem": [], "sync": []}
    for snap in snapshots or []:
        for node in snap.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            layer, node_id = node.get("layer"), node.get("id")
            if layer in ("device", "tool", "hook"):
                groups["tools"].append((snap, node))
            elif node_id in ("worker", "sqlite"):
                groups["claude_mem"].append((snap, node))
            elif node_id in ("sync", "nasdb"):
                groups["sync"].append((snap, node))
    out: dict[str, dict] = {}
    for stage, items in groups.items():
        if not items:
            out[stage] = {"state": "grey", "sub": "无探针快照", "detail": "",
                          "metrics": {}}
            continue
        states, lines, metrics = [], [], {}
        for snap, node in items:
            state = node.get("state") or "grey"
            note = ""
            if snap.get("_disconnected") or snap.get("_snapshot_stale"):
                state, note = "grey", "（快照过期或设备离线，不作实况）"
            states.append(state)
            lines.append(f"[{snap.get('_host', '?')}] {node.get('label', node.get('id'))}: "
                         f"{state} {str(node.get('detail') or '')[:160]}{note}")
            node_metrics = node.get("metrics") if isinstance(node.get("metrics"), dict) else {}
            if node.get("id") == "worker" and node_metrics.get("queue_depth") is not None:
                metrics["queue_depth"] = node_metrics["queue_depth"]
            if node.get("id") == "sync":
                for key in ("local_max_obs_id", "nas_max_obs_id", "lag_rows"):
                    if node_metrics.get(key) is not None:
                        metrics[key] = node_metrics[key]
        bad = sum(STATE_RANK.get(s, 1) >= 2 for s in states)
        if stage == "tools":
            sub = f"{len(items)} 个节点" + (f" · {bad} 个异常" if bad else " · 正常")
        elif stage == "claude_mem":
            depth = metrics.get("queue_depth")
            sub = f"内存队列 {depth}" if depth is not None else ("异常" if bad else "正常")
        else:
            lag = metrics.get("lag_rows")
            if lag is None and isinstance(metrics.get("local_max_obs_id"), int) \
                    and isinstance(metrics.get("nas_max_obs_id"), int):
                lag = metrics["local_max_obs_id"] - metrics["nas_max_obs_id"]
                metrics["lag_rows"] = lag
            if lag is None:
                sub = "异常" if bad else "正常"
            elif lag < 0:
                # NAS 副本汇总多台设备的观测，编号可以领先任何一台本机。
                sub = f"已同步 · NAS 汇总领先本机 {-lag} 条"
            else:
                sub = f"落差 {lag} 条"
        out[stage] = {"state": _worst(states), "sub": sub,
                      "detail": "\n".join(lines), "metrics": metrics}
    return out


def refinery_stage(status: dict, now: datetime) -> dict:
    from dashboard_status import refinery_activity
    activity = refinery_activity(status, now)
    metrics = {k: status.get(k) for k in (
        "backlog_remaining", "backlog_window_open", "per_cycle", "live_per_cycle",
        "backoff_pending", "recovery_reason", "recovery_retry_at", "disk_temp",
        "heartbeat_at", "ts", "boundary_id", "live_cursor")}
    metrics["watermark"] = status.get("watermark") or {}
    remaining = status.get("backlog_remaining")
    sub = activity["label"][:40]
    if isinstance(remaining, int):
        sub = f"积压 {remaining} · {sub}"
    detail = [activity["label"],
              f"心跳 {status.get('heartbeat_at')} · 最近处理 {status.get('ts')}",
              f"工作窗口 {'开' if status.get('backlog_window_open') else '关'}"
              f" · 每轮名额 积压 {status.get('per_cycle')} / 实时 {status.get('live_per_cycle')}"]
    if status.get("recovery_reason"):
        detail.append(f"恢复暂停：{status.get('recovery_reason')}，下次探测 {status.get('recovery_retry_at')}")
    thermal = status.get("thermal") if isinstance(status.get("thermal"), dict) else {}
    if thermal:
        detail.append(f"今日温度歇工 {thermal.get('holds', 0)} 次 / {thermal.get('minutes', 0)} 分钟")
    return {"state": activity["state"], "sub": sub, "detail": "\n".join(detail),
            "metrics": metrics}


def kghub_stage(keys: dict | None, timing: dict, active: int | None) -> dict:
    if keys is None:
        return {"state": "red", "sub": "图查询失败", "detail": "IngestedKey 读取失败，服务端或 FalkorDB 不可用",
                "metrics": {"timing": timing, "active_extractions": active}}
    oldest = keys.get("pending_oldest_s")
    errors = sum((keys.get("errors_24h") or {}).values())
    state = "green"
    if isinstance(oldest, (int, float)) and oldest > PENDING_STUCK_S:
        state = "red"
    elif ((timing.get("wait_share") or 0) >= LOCK_WAIT_SHARE_SLOW and timing.get("samples", 0) >= 5) \
            or errors >= ERRORS_24H_SLOW:
        state = "amber"
    sub = f"在飞 {active if active is not None else '?'} · pending {keys.get('pending', 0)}"
    detail = [f"IngestedKey 状态分布：{json.dumps(keys.get('by_status') or {}, ensure_ascii=False)}",
              f"最老 pending：{_human(oldest)}",
              f"近 24h 失败/待核对：{json.dumps(keys.get('errors_24h') or {}, ensure_ascii=False)}",
              f"领取→终态耗时 P50 {keys.get('duration_p50')}s · P90 {keys.get('duration_p90')}s"
              f"（近 24h {keys.get('duration_samples', 0)} 条成功）"]
    if timing.get("samples"):
        detail.append(f"{_queue_label(timing)} P50 {timing.get('wait_p50')}s / P90 {timing.get('wait_p90')}s；"
                      f"抽取 P50 {timing.get('extract_p50')}s / P90 {timing.get('extract_p90')}s；"
                      f"排队占比 {_share(timing.get('wait_share'))}（本进程 {timing['samples']} 条样本）")
    else:
        detail.append("排队/抽取耗时拆分：本进程启动后尚无样本")
    return {"state": state, "sub": sub, "detail": "\n".join(detail),
            "metrics": {"keys": keys, "timing": timing, "active_extractions": active}}


def graph_stage(graph_daily: list[dict] | None, now: datetime) -> dict:
    if graph_daily is None:
        return {"state": "grey", "sub": "入图量不可读", "detail": "", "metrics": {}}
    today = now.strftime("%Y-%m-%d")
    per_lane: dict[str, int] = {}
    for row in graph_daily:
        if row.get("bucket") == today:
            per_lane[row["lane"]] = per_lane.get(row["lane"], 0) + int(row.get("count") or 0)
    total = sum(per_lane.values())
    return {"state": "green", "sub": f"今日入图 {total}",
            "detail": "今日（UTC）入图 Episode：" + (
                "、".join(f"{k} {v}" for k, v in sorted(per_lane.items())) or "0"),
            "metrics": {"today": per_lane}}


# ---------- 积压消化 ----------

def _line(bucket: dict, name: str) -> dict:
    raw = bucket.get(name) if isinstance(bucket.get(name), dict) else {}
    return {k: int(raw.get(k) or 0) for k in ("ingested", "rejected", "deferred")}


def backlog_digest(status: dict, graph_daily: list[dict] | None, now: datetime) -> dict:
    budget = status.get("budget_today") if isinstance(status.get("budget_today"), dict) else {}
    hourly_raw = budget.get("hourly") if isinstance(budget.get("hourly"), dict) else {}
    hours = []
    for key in sorted(hourly_raw):
        start = _parse_ts(key + ":00:00+00:00")
        if start is None or now - start > timedelta(hours=48):
            continue
        bucket = hourly_raw[key] if isinstance(hourly_raw[key], dict) else {}
        hours.append({"hour": key, "backlog": _line(bucket, "backlog"),
                      "live": _line(bucket, "live")})
    # 空闲小时补零行：看板要让人看见「哪些小时没干活」；最早记录之前是未知，不补。
    if hours:
        seen = {h["hour"] for h in hours}
        cursor = _parse_ts(hours[0]["hour"] + ":00:00+00:00")
        zero = {"ingested": 0, "rejected": 0, "deferred": 0}
        while cursor <= now:
            key = cursor.strftime("%Y-%m-%dT%H")
            if key not in seen:
                hours.append({"hour": key, "backlog": dict(zero), "live": dict(zero)})
            cursor += timedelta(hours=1)
        hours.sort(key=lambda h: h["hour"])

    recent = [h for h in hours
              if now - _parse_ts(h["hour"] + ":00:00+00:00") < timedelta(hours=24)]
    b_ing = sum(h["backlog"]["ingested"] for h in recent)
    b_rej = sum(h["backlog"]["rejected"] for h in recent)
    b_dfr = sum(h["backlog"]["deferred"] for h in recent)
    l_ing = sum(h["live"]["ingested"] for h in recent)
    l_rej = sum(h["live"]["rejected"] for h in recent)
    active_hours = sum(1 for h in recent
                       if any(v for line in (h["backlog"], h["live"]) for v in line.values()))
    coverage_h = 0.0
    if recent:
        earliest = _parse_ts(recent[0]["hour"] + ":00:00+00:00")
        coverage_h = round(min(24.0, (now - earliest).total_seconds() / 3600), 1)
    terminal = b_ing + b_rej
    remaining = status.get("backlog_remaining")
    eta_days = None
    if isinstance(remaining, int) and terminal > 0 and coverage_h >= 20:
        eta_days = round(remaining / terminal, 1)

    daily = []
    graph_eta_days = None
    backlog_7d = None
    if graph_daily is not None:
        by_day: dict[str, dict[str, int]] = {}
        for row in graph_daily:
            by_day.setdefault(row["bucket"], {})
            by_day[row["bucket"]][row["lane"]] = int(row.get("count") or 0)
        daily = [{"day": d, **v} for d, v in sorted(by_day.items())]
        week = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]
        backlog_7d = round(sum(by_day.get(d, {}).get("积压线", 0) for d in week) / 7, 1)
        if isinstance(remaining, int) and backlog_7d:
            graph_eta_days = round(remaining / backlog_7d, 1)

    return {
        "remaining": remaining,
        "boundary_id": status.get("boundary_id"),
        "window_open": bool(status.get("backlog_window_open")),
        "hourly": hours,
        "daily": daily,
        "last24": {"backlog_ingested": b_ing, "backlog_rejected": b_rej,
                   "backlog_deferred": b_dfr, "backlog_terminal": terminal,
                   "live_ingested": l_ing, "live_rejected": l_rej,
                   "active_hours": active_hours, "coverage_h": coverage_h},
        "accept_rate": _pct(b_ing, terminal),
        "deferred_share": (round(b_dfr / (terminal + b_dfr), 3) if terminal + b_dfr else None),
        "rate_per_active_hour": round(terminal / active_hours, 1) if active_hours else None,
        "eta_days": eta_days,
        "backlog_ingested_per_day_7d": backlog_7d,
        "graph_eta_days": graph_eta_days,
        "result_counts": ((budget.get("lines") or {}).get("backlog") or {}).get("result_counts") or {},
    }


def calls_per_observation(gateway_node: dict | None, digest: dict, now: datetime) -> float | None:
    """同一 UTC 日：网关 kg-hub 调用次数 ÷ 图内当日新增 Episode。

    两端都是持久计数。refinery 的当日终态数在它进程内存里，重启即归零，
    拿它当分母会在每次重启后把倍数放大几百倍。被质量闸拒绝的观测不调模型，
    失败的抽取调了模型却没入图，所以这个比值就是「每成功入图一条的调用成本」。"""
    if not gateway_node:
        return None
    from topology import GATEWAY_PRIMARY_KEY
    keys = (gateway_node.get("metrics") or {}).get("keys") or {}
    calls = (keys.get(GATEWAY_PRIMARY_KEY) or {}).get("today")
    today = now.strftime("%Y-%m-%d")
    row = next((d for d in digest.get("daily") or [] if d.get("day") == today), None)
    episodes = sum(v for k, v in row.items() if k != "day") if row else 0
    if isinstance(calls, int) and episodes >= CALLS_RATIO_MIN_EPISODES:
        return round(calls / episodes, 1)
    return None


# ---------- 卡点判定 ----------

def find_bottlenecks(*, status: dict, stages: dict[str, dict], digest: dict,
                     gateway_node: dict | None, keys: dict | None, timing: dict,
                     calls_per_obs: float | None, now: datetime) -> list[dict]:
    """把各段证据翻译成「卡在哪、为什么、怎么办」，停流在前，慢流在后。"""
    from topology import refinery_halt
    found: list[dict] = []

    def add(stage, level, title, evidence, action, deliberate=False):
        found.append({"stage": stage, "level": level, "title": title,
                      "evidence": evidence, "action": action, "deliberate": deliberate})

    heartbeat = _parse_ts(status.get("heartbeat_at"))
    if heartbeat is None or (now - heartbeat).total_seconds() > 900:
        add("refinery", "stop", "refinery 无心跳",
            f"最后心跳 {status.get('heartbeat_at') or '无'}",
            "检查 kg-refinery 容器是否存活、refinery-state 卷是否挂载")
    if status.get("last_error"):
        add("refinery", "stop", "refinery 本轮异常", str(status["last_error"])[:200],
            "查看 kg-refinery 容器日志")
    halt = refinery_halt(status)
    if status.get("breaker_open"):
        add("refinery", "stop", "人工断路中",
            str(status.get("breaker_reason") or "未填原因"),
            "确认原因后在拓扑页合上 kg_hub.entity_extract 断路器", deliberate=True)
    if status.get("thermal_hold"):
        add("refinery", "stop", "盘温门控歇工",
            f"盘温 {status.get('disk_temp')}°C；今日已歇 "
            f"{(status.get('thermal') or {}).get('minutes', 0)} 分钟",
            "改善散热；门控阈值是保护整机的决定，不建议直接调高")
    if status.get("idle_outside_window"):
        add("refinery", "stop", "工作窗口外，积压暂停",
            "refinery 只在工作窗口内消化；窗口外只写心跳",
            "若要提速，评估扩大工作窗口（refinery_window 配置）", deliberate=True)
    reason = status.get("recovery_reason")
    if reason or status.get("quota_paused") or status.get("rate_limited") \
            or status.get("upstream_error_paused"):
        label = reason or ("配额耗尽" if status.get("quota_paused") else
                           "限流" if status.get("rate_limited") else "上游 5xx")
        add("gateway", "stop", f"网关/供应商暂停：{label}",
            f"下次探测 {status.get('recovery_retry_at') or '—'}；停工闸门 "
            + ("、".join(halt["gates"]) or "—"),
            "等待自动恢复；日额度耗尽需等 UTC 日切或调整共享额度")
    if gateway_node:
        if gateway_node.get("state") == "red" and not reason:
            add("gateway", "stop", "网关不可用或额度打满",
                str(gateway_node.get("sub") or ""),
                "查看 credvault 网关 /health/ready 与用量看板")
        ratio = ((gateway_node.get("metrics") or {}).get("keys") or {}) \
            .get("kg_hub.entity_extract", {}).get("ratio")
        if isinstance(ratio, (int, float)) and 0.8 <= ratio < 1:
            add("gateway", "slow", "今日额度接近上限", f"已用 {ratio * 100:.0f}%",
                "关注窗口尾部；额度不是消化速度的上限，调用倍数才是")
    for stage in ("tools", "claude_mem", "sync"):
        info = stages.get(stage) or {}
        if info.get("state") == "red":
            add(stage, "stop", f"{dict((s[0], s[1]) for s in STAGES)[stage]} 异常",
                str(info.get("detail") or info.get("sub"))[:240],
                "影响新数据进入链路，不直接影响历史积压消化；见采集链路拓扑")
        elif info.get("state") == "amber" and stage == "sync":
            add(stage, "slow", "Mac→NAS 同步落后", str(info.get("sub")),
                "确认 Mac 在线与 launchd 同步作业")

    if keys is not None:
        oldest = keys.get("pending_oldest_s")
        if isinstance(oldest, (int, float)) and oldest > PENDING_STUCK_S:
            add("kghub", "stop", "有抽取任务卡在 pending",
                f"最老 pending 已 {_human(oldest)}，超过合法上限 {_human(PENDING_STUCK_S)}",
                "检查 kg_hub_server 日志与写锁持有者；清理器会接管过期键")
        errors = keys.get("errors_24h") or {}
        total_errors = sum(errors.values())
        if total_errors >= ERRORS_24H_SLOW:
            top = "、".join(f"{k} {v}" for k, v in sorted(errors.items(), key=lambda kv: -kv[1])[:4])
            add("kghub", "slow", "近 24h 抽取失败偏多", f"{total_errors} 条：{top}",
                "按错误类型处理；失败的调用已计费，重试会再次计费")

    if timing.get("samples", 0) >= 5:
        share = timing.get("wait_share")
        if isinstance(share, (int, float)) and share >= LOCK_WAIT_SHARE_SLOW:
            if timing.get("parallel"):
                add("kghub", "slow", "并发槽位排队是主要耗时",
                    f"等槽位占 {_share(share)}；排队 P50 {timing.get('wait_p50')}s，"
                    f"抽取+提交 P50 {timing.get('extract_p50')}s",
                    "槽位一直满载，吞吐由单条耗时决定：先压单条调用次数与冲突重算；"
                    "加槽位前确认网关并发上限")
            else:
                add("kghub", "slow", "写锁排队是主要耗时",
                    f"排队占 {_share(share)}；排队 P50 {timing.get('wait_p50')}s，"
                    f"抽取 P50 {timing.get('extract_p50')}s",
                    "把模型抽取移出写锁（KG_HUB_PARALLEL_EXTRACTION）；在此之前加并发只会拉长锁队列")
        extract_p50 = timing.get("extract_p50")
        if isinstance(extract_p50, (int, float)) and extract_p50 >= EXTRACT_P50_SLOW_S:
            add("kghub", "slow", "单条抽取耗时高",
                f"抽取 P50 {extract_p50}s / P90 {timing.get('extract_p90')}s",
                "graphiti 每条观测多轮调用；合批联合抽取（吞吐方案 P2）")
    elif keys is not None and isinstance(keys.get("duration_p50"), (int, float)) \
            and keys["duration_p50"] >= EXTRACT_P50_SLOW_S:
        add("kghub", "slow", "单条入图耗时高",
            f"领取→终态 P50 {keys['duration_p50']}s / P90 {keys.get('duration_p90')}s（含排队）",
            "graphiti 每条观测多轮调用；合批联合抽取（吞吐方案 P2）")

    if isinstance(calls_per_obs, (int, float)) and calls_per_obs >= CALLS_PER_OBS_SLOW:
        add("gateway", "slow", "每条观测模型调用次数高",
            f"今日约 {calls_per_obs} 次调用 / 条入图",
            "降低调用倍数：合批抽取、属性合批、减少重试")
    share = digest.get("deferred_share")
    if isinstance(share, (int, float)) and share >= DEFERRED_SHARE_SLOW:
        top = "、".join(f"{k} {v}" for k, v in sorted(
            (digest.get("result_counts") or {}).items(), key=lambda kv: -kv[1])[:4])
        add("refinery", "slow", "推迟/重试占比高", f"近 24h 推迟占 {_share(share)}；{top}",
            "按推迟原因处理（409 退避、网关错误、超时）")
    last24 = digest.get("last24") or {}
    if last24.get("coverage_h", 0) >= 20 and last24.get("active_hours", 0) <= 12 \
            and (digest.get("remaining") or 0) > 0:
        add("refinery", "slow", "每天真正干活的小时数少",
            f"近 24h 只有 {last24.get('active_hours')} 个小时有产出",
            "窗口、温度、暂停都会吃掉工作时间；见上面的停流项", deliberate=True)
    remaining = digest.get("remaining")
    eta = digest.get("eta_days") or digest.get("graph_eta_days")
    if isinstance(remaining, int) and remaining > 0:
        if last24.get("coverage_h", 0) >= 20 and last24.get("backlog_terminal", 0) == 0:
            add("refinery", "stop", "近 24h 积压零消化", f"剩余 {remaining} 条，24 小时内终态 0 条",
                "先解决上面的停流项")
        elif isinstance(eta, (int, float)) and eta > ETA_DAYS_SLOW:
            add("kghub", "slow", "按当前速度清空积压需要很久",
                f"剩余 {remaining} 条，预计约 {eta} 天",
                "结构性瓶颈在单条耗时 × 并发数：先看上面的慢流项，合批抽取降低调用倍数")

    found.sort(key=lambda b: (SEVERITY_RANK[b["level"]], b["deliberate"],
                              SLOW_IMPACT_ORDER.index(b["title"])
                              if b["title"] in SLOW_IMPACT_ORDER else len(SLOW_IMPACT_ORDER)))
    return found


def _queue_label(timing: dict) -> str:
    """并行抽取下 _ingest_execution_lock 取的是并发槽位，写锁只在提交时短暂持有。"""
    return "槽位排队" if timing.get("parallel") else "写锁排队"


def _human(seconds: object) -> str:
    if not isinstance(seconds, (int, float)):
        return "—"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} 秒"
    if seconds < 5400:
        return f"{seconds // 60} 分钟"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def _share(value: object) -> str:
    return f"{value * 100:.0f}%" if isinstance(value, (int, float)) else "—"


# ---------- 图 ----------

def _m(*lines: object) -> str:
    """Mermaid 节点文字：去掉会破坏语法的字符，多行用 <br/>。"""
    clean = []
    for line in lines:
        text = str(line)
        for bad, good in (('"', "'"), ("#", "＃"), ("<", "‹"), (">", "›"),
                          ("[", "［"), ("]", "］"), ("{", "｛"), ("}", "｝"),
                          ("|", "｜"), (";", "；")):
            text = text.replace(bad, good)
        clean.append(text)
    return "<br/>".join(clean)


def _nid(stage_id: str) -> str:
    """阶段 id 直接做 Mermaid 节点 id 会撞保留字（graph），统一加前缀。"""
    return "s_" + stage_id


def topology_mermaid(stages: list[dict], choke: str | None, digest: dict) -> str:
    by_id = {s["id"]: s for s in stages}
    out = ["flowchart LR"]
    for stage in stages:
        out.append(f'  {_nid(stage["id"])}["{_m(stage["label"], stage["sub"])}"]')
    remaining = digest.get("remaining")
    active = ((by_id.get("kghub") or {}).get("metrics") or {}).get("active_extractions")
    out += [
        '  s_tools -->|hook 捕获| s_claude_mem',
        '  s_claude_mem -->|SQLite| s_sync',
        f'  s_sync -->|"{_m("积压 " + str(remaining if remaining is not None else "?"))}"| s_refinery',
        f'  s_refinery -->|"{_m("POST /api/ingest · 在飞 " + str(active if active is not None else "?"))}"| s_kghub',
        '  s_kghub <-->|每条多轮 LLM| s_gateway',
        '  s_kghub -->|写入| s_graph',
        "  classDef green fill:#E1F5EE,stroke:#1D9E75,color:#085041",
        "  classDef amber fill:#FFF3CD,stroke:#C08A00,color:#715500",
        "  classDef red fill:#FDEDED,stroke:#D64545,color:#8A1C1C",
        "  classDef grey fill:#EEEEEE,stroke:#999999,color:#555555",
        "  classDef choke stroke-width:4px,stroke-dasharray:6 3",
    ]
    for stage in stages:
        out.append(f'  class {_nid(stage["id"])} {stage["state"]}')
    if choke in by_id:
        out.append(f"  class {_nid(choke)} choke")
    return "\n".join(out)


USECASE_MERMAID = """flowchart LR
  dev(["👤 开发者"])
  cm(["⚙️ claude-mem worker"])
  cron(["⏱ launchd 同步作业"])
  ref(["⚙️ refinery"])
  ai(["🤖 AI 会话 / MCP"])
  ops(["👤 运维（你）"])
  subgraph SYS["kg-hub 知识系统"]
    u1(["在工具里编码，hook 捕获动作"])
    u2(["用 LLM 提炼 observation"])
    u3(["同步 db 到 NAS"])
    u4(["质量过滤 / 调度积压与实时"])
    u5(["提交入图（幂等）"])
    u6(["抽取实体与关系写入图谱"])
    u7(["检索知识 kg_search / episode_search"])
    u8(["查看积压与卡点"])
    u9(["扳断路器 / 调工作窗口"])
  end
  dev --- u1
  cm --- u2
  cron --- u3
  ref --- u4
  ref --- u5
  u5 -.包含.-> u6
  ai --- u7
  ops --- u8
  ops --- u9
  u1 -.触发.-> u2
"""

FLOW_MERMAID = """flowchart TD
  A["新 observation 落 SQLite"] --> B["launchd 同步到 NAS 副本"]
  B --> C{"id ≤ boundary？"}
  C -->|是| BL["积压线"]
  C -->|否| LV["实时线"]
  BL --> W{"工作窗口内？"}
  LV --> W
  W -->|否| WAIT1["只写心跳，等窗口"]
  W -->|是| BR{"人工断路？"}
  BR -->|是| WAIT2["停流，不提交"]
  BR -->|否| TH{"盘温 ≥ 阈值？"}
  TH -->|是| WAIT3["本轮歇工"]
  TH -->|否| RC{"网关恢复暂停中？"}
  RC -->|是| WAIT4["按恢复时点探测 /api/model-readiness"]
  RC -->|否| SCH["按项目阈值 / 最长等待调度"]
  SCH --> F{"质量闸通过？"}
  F -->|否| REJ["rejected（不调模型）"]
  F -->|是| P["POST /api/ingest"]
  P --> K{"IngestedKey"}
  K -->|已 ok| SKIP["skipped"]
  K -->|error 键| E409["409 → 指数退避"]
  K -->|新领取| L["排队等并发槽位（串行模式：写锁）"]
  L --> G["锁外抽取：实体 / 去重 / 抽边 / 属性 / 摘要"]
  G --> GW["每步经模型网关调 LLM"]
  GW -->|成功| CM{"短写锁提交：读集冲突？"}
  CM -->|冲突| G
  CM -->|无冲突| OK["写入 FalkorDB · 键=ok · 水印 ingested"]
  GW -->|额度/限流/5xx| PAUSE["键=error · refinery 整体暂停"]
  GW -->|结果未知| REC["needs_reconciliation · 禁止自动重放"]
"""

ARCH_MERMAID = """flowchart TB
  subgraph L1["采集层 · Mac"]
    T["Claude Code / Cursor / Codex / Qoder"] --> H["hook"] --> W["claude-mem worker"] --> S[("claude-mem.db")]
  end
  subgraph L2["传输层"]
    Y["launchd 每 15 分钟同步"] --> N[("NAS 副本 claude-mem.db")]
  end
  subgraph L3["精炼层 · kg-refinery"]
    R1["窗口 / 温度 / 断路 / 恢复门控"] --> R2["按项目调度 积压 : 实时"] --> R3["ingest_filter 质量闸"] --> R4["提交 + 轮询终态"]
  end
  subgraph L4["服务层 · kg_hub_server"]
    K1["/api/ingest 幂等领取"] --> K2["并发槽位"] --> K3["graphiti 锁外抽取"] --> K4["短写锁提交 · 冲突重算"]
  end
  subgraph L5["模型层 · credvault"]
    G1["模型网关：共享日额度 / RPM / 幂等 / 熔断"] --> G2["百炼 qwen"]
  end
  subgraph L6["存储层"]
    F[("FalkorDB 知识图谱")]
  end
  subgraph L7["使用层"]
    U1["MCP kg_search"]
    U2["报表门户 / 本看板"]
    U3["watchdog 告警"]
  end
  S --> Y
  N --> R1
  R4 --> K1
  K3 --> G1
  W -. 提炼 observation .-> G1
  K4 --> F
  F --> U1
  F --> U2
"""

APP_ARCH_MERMAID = """flowchart LR
  subgraph MAC["Mac"]
    CC["编码工具 + hook"] --> CMW["claude-mem worker :37701"]
    CMW --> CMDB[("claude-mem.db")]
    SYNC["sync_claude_mem_to_nas.sh（launchd）"]
    PROBE["capture_probe 探针（launchd）"]
  end
  subgraph NAS["NAS · docker compose"]
    NDB[("claude-mem.db 副本 · ro")]
    REF["kg-refinery 容器"]
    ST[/"refinery-state/status.json"/]
    SRV["kg_hub_server :8080（tailnet 17171）"]
    FDB[("FalkorDB")]
    GW["model-gateway :39000"]
    USG[/"gateway-usage/usage.json"/]
    WD["watchdog"]
  end
  PROV["百炼 qwen"]
  CMDB --> SYNC -->|rsync/ssh| NDB
  NDB --> REF
  REF -->|"POST /api/ingest · GET /api/ingest/status"| SRV
  REF -->|"GET /api/model-readiness"| SRV
  REF --> ST -->|ro 挂载| SRV
  SRV -->|"/v1/messages model=kg_hub.entity_extract"| GW
  CMW -->|"/v1/messages model=claude_mem.observation"| GW
  GW --> PROV
  GW -. 导出 .-> USG -->|ro 挂载| SRV
  SRV --> FDB
  PROBE -->|"POST /api/topology/report"| SRV
  WD -->|"/health · /api/topology/latest"| SRV
"""


# ---------- 组装 ----------

def build_flow(*, status: dict, snapshots: list[dict], gateway_node: dict | None,
               keys: dict | None, timing: dict, active: int | None,
               graph_daily: list[dict] | None, now: datetime,
               source_errors: list[str] | None = None,
               key_trends: list[dict] | None = None) -> dict:
    status = status if isinstance(status, dict) else {}
    probed = probe_stages(snapshots)
    per_stage = {
        **probed,
        "refinery": refinery_stage(status, now),
        "kghub": kghub_stage(keys, timing, active),
        "gateway": ({"state": gateway_node.get("state", "grey"),
                     "sub": gateway_node.get("sub") or "",
                     "detail": gateway_node.get("detail") or "",
                     "metrics": gateway_node.get("metrics") or {}}
                    if gateway_node else
                    {"state": "grey", "sub": "网关数据不可读", "detail": "", "metrics": {}}),
        "graph": graph_stage(graph_daily, now),
    }
    digest = backlog_digest(status, graph_daily, now)
    calls = calls_per_observation(gateway_node, digest, now)
    bottlenecks = find_bottlenecks(status=status, stages=per_stage, digest=digest,
                                   gateway_node=gateway_node, keys=keys, timing=timing,
                                   calls_per_obs=calls, now=now)
    for b in bottlenecks:
        stage = per_stage[b["stage"]]
        if b["level"] == "stop" and not b["deliberate"]:
            stage["state"] = "red"
        elif STATE_RANK.get(stage["state"], 1) < 2:
            stage["state"] = "amber"
    stages = [{"id": sid, "label": label, "role": role, **per_stage[sid],
               "bottlenecks": [b for b in bottlenecks if b["stage"] == sid]}
              for sid, label, role in STAGES]
    primary = bottlenecks[0] if bottlenecks else None
    efficiency = {
        "calls_per_observation": calls,
        "extract_p50": timing.get("extract_p50"),
        "extract_p90": timing.get("extract_p90"),
        "wait_p50": timing.get("wait_p50"),
        "wait_share": timing.get("wait_share"),
        "timing_samples": timing.get("samples", 0),
        "queue_label": _queue_label(timing),
        "duration_p50": (keys or {}).get("duration_p50"),
        "duration_p90": (keys or {}).get("duration_p90"),
    }
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "stages": stages,
        "backlog": digest,
        "efficiency": efficiency,
        "key_trends": key_trends or [],
        "bottlenecks": bottlenecks,
        "primary": primary,
        "diagrams": {
            "topology": topology_mermaid(stages, primary["stage"] if primary else None, digest),
            "usecase": USECASE_MERMAID,
            "flow": FLOW_MERMAID,
            "arch": ARCH_MERMAID,
            "app_arch": APP_ARCH_MERMAID,
        },
        "source_errors": source_errors or [],
    }


async def _key_stats(driver, now: datetime) -> dict:
    since = (now - timedelta(hours=24)).isoformat()
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey) RETURN coalesce(k.status,'unknown') AS s, count(k) AS c")
    by_status = {r.get("s"): int(r.get("c") or 0) for r in rows}
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey) WHERE k.status = 'pending' RETURN min(k.created_at) AS oldest")
    oldest = _parse_ts(rows[0].get("oldest")) if rows and rows[0].get("oldest") else None
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey) WHERE k.status = 'ok' AND k.updated_at >= $since "
        "RETURN k.created_at AS a, k.updated_at AS b ORDER BY k.updated_at DESC LIMIT 500",
        since=since)
    durations = []
    for r in rows:
        a, b = _parse_ts(r.get("a")), _parse_ts(r.get("b"))
        if a and b and b >= a:
            durations.append((b - a).total_seconds())
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey) WHERE k.status IN ['error','needs_reconciliation','failed'] "
        "AND k.updated_at >= $since RETURN coalesce(k.error_kind,'unknown') AS kind, count(k) AS c",
        since=since)
    return {
        "by_status": by_status,
        "pending": by_status.get("pending", 0),
        "pending_oldest_s": (now - oldest).total_seconds() if oldest else None,
        "errors_24h": {r.get("kind"): int(r.get("c") or 0) for r in rows},
        "duration_p50": _percentile(durations, 0.5),
        "duration_p90": _percentile(durations, 0.9),
        "duration_samples": len(durations),
    }


async def _graph_daily(driver, boundary: object, now: datetime) -> list[dict]:
    rows, _, _ = await driver.execute_query(
        "MATCH (n:Episodic) WHERE n.created_at >= $floor "
        "WITH n, substring(n.created_at, 0, 10) AS bucket "
        "WITH bucket, CASE "
        "  WHEN NOT n.name STARTS WITH 'claude-mem-obs-' THEN '其他源' "
        "  WHEN toInteger(substring(n.name, 15)) <= $boundary THEN '积压线' "
        "  ELSE 'live 线' END AS lane "
        "RETURN bucket, lane, count(*) AS c ORDER BY bucket",
        floor=(now - timedelta(days=14)).strftime("%Y-%m-%d"), boundary=int(boundary or 0))
    return [{"bucket": r.get("bucket"), "lane": r.get("lane"), "count": int(r.get("c") or 0)}
            for r in rows]


async def _hourly_outcomes(driver, now: datetime) -> list[dict]:
    rows, _, _ = await driver.execute_query(
        "MATCH (k:IngestedKey) WHERE k.updated_at >= $since "
        "AND k.status IN ['ok','error','failed','needs_reconciliation'] "
        "RETURN substring(k.updated_at,0,13) AS hour, k.status AS status, "
        "count(k) AS c ORDER BY hour",
        since=(now - timedelta(hours=24)).isoformat())
    return [{"hour": r.get("hour"), "status": r.get("status"),
             "count": int(r.get("c") or 0)} for r in rows]


async def collect_flow() -> dict:
    from dashboard_status import apply_gateway_health, gateway_health
    from kg_hub_server import active_extractions, get_status_driver
    from topology import (GATEWAY_USAGE_PATH, REFINERY_STATUS_PATH, _load_snapshots,
                          _read_json, gateway_quota_node)
    from utils import ingest_timing, flow_metrics

    now = datetime.now(tz=timezone.utc)
    errors: list[str] = []
    status = _read_json(REFINERY_STATUS_PATH) or {}
    if not status:
        errors.append(f"refinery 状态不可读：{REFINERY_STATUS_PATH}")
    try:
        snapshots = await _load_snapshots()
    except Exception as exc:  # noqa: BLE001
        snapshots = []
        errors.append(f"探针快照不可读：{type(exc).__name__}")
    gateway_node = None
    try:
        gateway_node, edge = gateway_quota_node(_read_json(GATEWAY_USAGE_PATH), status, now=now)
        apply_gateway_health(gateway_node, edge, await gateway_health())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"网关数据不可读：{type(exc).__name__}")
    driver = get_status_driver()
    keys = None
    try:
        keys = await _key_stats(driver, now)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"IngestedKey 查询失败：{type(exc).__name__}")
    graph_daily = None
    if status.get("boundary_id") is not None:
        try:
            graph_daily = await _graph_daily(driver, status["boundary_id"], now)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"入图量查询失败：{type(exc).__name__}")
    outcomes = None
    try:
        outcomes = await _hourly_outcomes(driver, now)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"小时入图状态不可读：{type(exc).__name__}")
    attempts = None
    try:
        attempts = await asyncio.to_thread(_model_attempt_rows, now - timedelta(hours=24))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"模型调用账本不可读：{type(exc).__name__}")
    trends = key_metric_trends(now=now, commits=flow_metrics.recent(
        since=(now - timedelta(hours=24)).timestamp()), attempts=attempts,
        outcomes=outcomes)
    return build_flow(status=status, snapshots=snapshots, gateway_node=gateway_node,
                      keys=keys, timing=ingest_timing.summary(now=time.time()),
                      active=active_extractions(), graph_daily=graph_daily, now=now,
                      source_errors=errors, key_trends=trends)


async def dashboard_flow(request: Request) -> HTMLResponse:
    data = await collect_flow()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return HTMLResponse(_HTML.replace("__DATA__", payload))


async def dashboard_flow_json(request: Request) -> JSONResponse:
    return JSONResponse(await collect_flow())


_HTML = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=120>
<title>kg-hub 积压消化链路</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:1180px;margin:1.5rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.55}
a.back{font-size:13px;color:GrayText;text-decoration:none}h1{font-size:20px;font-weight:500;margin:.3rem 0}
h2{font-size:15px;font-weight:600;margin:1.6rem 0 .5rem}
.ts,.note{color:GrayText;font-size:12px}
.verdict{border-radius:10px;padding:.8rem 1rem;margin:1rem 0;font-size:14px}
.verdict.stop{background:#FDEDED;color:#8A1C1C}.verdict.slow{background:#FFF3CD;color:#715500}.verdict.ok{background:#E1F5EE;color:#085041}
.verdict b{font-size:15px}.verdict .act{margin-top:4px;font-size:13px}
.chain{display:grid;grid-template-columns:repeat(7,1fr);gap:8px;align-items:stretch}
.st{border-radius:10px;padding:.6rem .7rem;border:2px solid transparent;cursor:pointer;position:relative;font-size:13px}
.st .n{font-weight:600;font-size:14px}.st .s{font-size:12px;margin-top:2px;word-break:break-all}
.st .r{font-size:11px;color:GrayText;margin-top:4px}
.st.green{background:#E1F5EE;color:#085041}.st.amber{background:#FFF3CD;color:#715500}
.st.red{background:#FDEDED;color:#8A1C1C}.st.grey{background:color-mix(in srgb,CanvasText 8%,transparent)}
.st.choke{border-color:#D64545;border-style:dashed}
.st .tag{position:absolute;top:-9px;right:6px;background:#D64545;color:#fff;font-size:10px;padding:0 6px;border-radius:6px}
.st:not(:last-child)::after{content:"→";position:absolute;right:-8px;top:40%;color:GrayText;font-size:13px}
pre.dt{white-space:pre-wrap;font-size:12px;font-family:ui-monospace,Menlo,monospace;background:color-mix(in srgb,CanvasText 5%,transparent);border-radius:8px;padding:.6rem;margin:.5rem 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px}
.mc{background:color-mix(in srgb,CanvasText 6%,transparent);border-radius:8px;padding:.6rem .8rem}
.mc .l{font-size:12px;color:GrayText}.mc .v{font-size:21px;font-weight:500}.mc .s{font-size:11px;color:GrayText}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent);padding:6px 8px;text-align:left;vertical-align:top}
th{font-size:12px;color:GrayText;font-weight:500}
.lv{font-size:11px;padding:1px 7px;border-radius:8px;white-space:nowrap}
.lv.stop{background:#FDEDED;color:#8A1C1C}.lv.slow{background:#FFF3CD;color:#715500}.lv.plan{background:color-mix(in srgb,CanvasText 10%,transparent)}
.row{display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px}
.row .k{width:96px;font-family:ui-monospace,Menlo,monospace;flex:none}
.bar{flex:1;height:12px;display:flex;border-radius:3px;overflow:hidden;background:color-mix(in srgb,CanvasText 6%,transparent);max-width:560px}
.bar i{display:block;height:100%}.row .c{width:240px;flex:none;color:GrayText}
.lg{font-size:12px;color:GrayText;margin:.3rem 0}.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 10px;vertical-align:-1px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:.4rem 0}
.tabs button{font:inherit;font-size:13px;padding:4px 12px;border-radius:8px;border:1px solid color-mix(in srgb,CanvasText 22%,transparent);background:transparent;color:inherit;cursor:pointer}
.tabs button.on{background:#378ADD;border-color:#378ADD;color:#fff}
.dg{border:1px solid color-mix(in srgb,CanvasText 14%,transparent);border-radius:10px;padding:.8rem;overflow:auto;min-height:200px;background:#fff}
.dg svg{max-width:100%;height:auto}
.warn{background:#FDEDED;color:#8A1C1C;border-radius:8px;padding:.5rem .8rem;font-size:13px;margin:.6rem 0}
.trend-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.trend{border:1px solid color-mix(in srgb,CanvasText 14%,transparent);border-radius:10px;padding:10px;min-width:0}
.trend h3{font-size:14px;margin:0 0 3px}.trend svg{display:block;width:100%;height:145px}
.trend .legend{font-size:11px;color:GrayText;display:flex;gap:12px;flex-wrap:wrap}
.trend .legend i{display:inline-block;width:13px;height:3px;vertical-align:3px;margin-right:4px}
@media(max-width:700px){.trend-grid{grid-template-columns:1fr}}
@media(max-width:900px){.chain{grid-template-columns:repeat(2,1fr)}.st::after{display:none}}
</style></head><body>
<a class=back href="/portal">← 报表门户</a>
<h1>🔀 积压消化链路 · 工具 → claude-mem → refinery → kg-hub → 知识图谱</h1>
<div class=ts id=gen></div>
<div id=errs></div>
<div id=verdict></div>

<h2>链路各段状态（点击展开证据）</h2>
<div class=chain id=chain></div>
<pre class=dt id=stdetail hidden></pre>

<h2>积压消化情况</h2>
<div class=cards id=bcards></div>
<div class=lg>近 48 小时每小时去向（单位：观测条数，UTC）：<i style="background:#1D9E75"></i>积压入图<i style="background:#A8B5B0"></i>积压过滤拒绝<i style="background:#5B8FF9"></i>实时入图<i style="background:#E8A33D"></i>推迟/重试</div>
<div id=hourly></div>
<div class=lg>近 14 天每日入图 Episode（图内实数，UTC）：<i style="background:#1D9E75"></i>积压线<i style="background:#5B8FF9"></i>live 线<i style="background:#B79CED"></i>其他源</div>
<div id=daily></div>

<h2>关键指标趋势</h2>
<div class=note>最近 24 小时 · UTC 整点分桶 · 每 2 分钟刷新；曲线中断表示该小时没有可用样本。当前小时截至快照时刻。</div>
<div class=trend-grid id=keytrends></div>

<h2>卡点清单（按影响排序：停流 → 慢流）</h2>
<table><thead><tr><th style="width:90px">位置</th><th style="width:60px">类型</th><th>现象</th><th>证据</th><th>建议</th></tr></thead><tbody id=bn></tbody></table>

<h2>链路图</h2>
<div class=tabs id=tabs></div>
<div class=dg id=diagram>加载图形渲染库中…</div>
<div class=note id=dgnote></div>

<h2>口径</h2>
<div class=note>
「消化」= 积压观测进入终态（入图或被质量闸拒绝）；推迟不算消化。去向账来自 refinery（观测条数），入图量来自图内 Episode（按 claude-mem-obs 编号与 boundary 分线），模型调用量来自网关（调用次数）——三者单位不同。调用倍数 = 网关当日调用 ÷ 图内当日新增，两端都是持久计数、都按 UTC 日，当日新增不足 10 条时不给数。<br>
排队 / 抽取耗时来自 kg_hub_server 进程内最近 500 条样本，服务重启后清零；并行抽取模式下「排队」是等并发槽位，「抽取」含锁外抽取、冲突重算与提交；领取→终态耗时来自 IngestedKey 时间戳，包含排队。<br>
refinery 的小时账保存在其进程内存，refinery 重启后从零开始积累；样本不足 20 小时不给按 24 小时推算的清空时间，改用近 7 天图内积压线入图量（未计过滤拒绝，偏保守）。<br>
关键指标按 UTC 小时展示：成功/失败取 IngestedKey 当前终态的 updated_at；提交/冲突取本服务进程内采样，重启后历史为空；模型调用取持久账本的 HTTP 开始时间，耗时只计已完成调用，在飞数用调用区间积分得到小时平均，未结调用最多计 15 分钟。调用/入图以同一小时开始的调用数除以该小时成功终态数，跨小时任务会带来偏差。<br>
工作窗口外暂停、人工断路属于计划内停流，不标红，但仍列出——它们是积压消化慢的真实原因之一。
</div>

<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>
const D=__DATA__;
const $=id=>document.getElementById(id);
const fmt=v=>(v===null||v===undefined)?'—':v;
const pct=v=>(v===null||v===undefined)?'—':Math.round(v*100)+'%';
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
$('gen').textContent='快照 '+D.generated_at+' UTC · 每 2 分钟自动刷新 · 数据接口 /dashboard/flow.json';
if(D.source_errors.length){$('errs').innerHTML='<div class=warn>部分数据源不可读：'+D.source_errors.map(esc).join('；')+'</div>'}

const P=D.primary;const vd=$('verdict');
if(!P){vd.className='verdict ok';vd.innerHTML='<b>✅ 未发现卡点</b><div class=act>各段正常。仍以下方消化速度与预计清空时间判断效率。</div>'}
else{const stageName=(D.stages.find(s=>s.id===P.stage)||{}).label||P.stage;
 vd.className='verdict '+(P.level==='stop'&&!P.deliberate?'stop':'slow');
 vd.innerHTML='<b>'+(P.level==='stop'?(P.deliberate?'⏸ 计划内停流':'🛑 当前卡点'):'🐢 主要瓶颈')+'：'+esc(stageName)+' · '+esc(P.title)+'</b><div>'+esc(P.evidence)+'</div><div class=act>建议：'+esc(P.action)+'</div>'
  +(D.bottlenecks.length>1?'<div class=act>另有 '+(D.bottlenecks.length-1)+' 项，见卡点清单。</div>':'')}

const chain=$('chain');
D.stages.forEach(s=>{const el=document.createElement('div');
 el.className='st '+s.state+(P&&P.stage===s.id?' choke':'');
 el.innerHTML=(P&&P.stage===s.id?'<span class=tag>卡点</span>':'')+'<div class=n>'+esc(s.label)+'</div><div class=s>'+esc(s.sub)+'</div><div class=r>'+esc(s.role)+'</div>';
 el.onclick=()=>{const d=$('stdetail');const txt=s.label+'\n'+(s.detail||'（无更多证据）')+(s.bottlenecks.length?'\n\n卡点：\n'+s.bottlenecks.map(b=>'· '+b.title+'：'+b.evidence).join('\n'):'');
  if(!d.hidden&&d.dataset.id===s.id){d.hidden=true;return}d.textContent=txt;d.dataset.id=s.id;d.hidden=false};
 chain.append(el)});

const B=D.backlog,L=B.last24||{},E=D.efficiency;
const eta=B.eta_days!=null?(B.eta_days+' 天'):(B.graph_eta_days!=null?('≈'+B.graph_eta_days+' 天*'):'—');
const etaNote=B.eta_days!=null?'按近 24h 终态速度':(B.graph_eta_days!=null?'*按近 7 天图内积压入图量（偏保守）':'样本不足');
const cards=[
 ['积压剩余',fmt(B.remaining),'工作窗口'+(B.window_open?'开':'关')+' · boundary '+fmt(B.boundary_id)],
 ['近 24h 积压消化',fmt(L.backlog_terminal),'入图 '+fmt(L.backlog_ingested)+' · 拒绝 '+fmt(L.backlog_rejected)+' · 样本 '+fmt(L.coverage_h)+'h'],
 ['预计清空',eta,etaNote],
 ['有产出小时',fmt(L.active_hours)+' / 24','每个有产出小时消化 '+fmt(B.rate_per_active_hour)+' 条'],
 ['入图率',B.accept_rate!=null?B.accept_rate+'%':'—','积压终态中进图的比例'],
 ['推迟/重试占比',pct(B.deferred_share),'近 24h 积压推迟 '+fmt(L.backlog_deferred)+' 条'],
 ['调用 / 条入图',fmt(E.calls_per_observation),'今日网关调用 ÷ 今日图内新增（UTC 日）'],
 ['单条抽取耗时',E.extract_p50!=null?(E.extract_p50+'s'):(E.duration_p50!=null?(E.duration_p50+'s'):'—'),E.extract_p50!=null?('P90 '+fmt(E.extract_p90)+'s · 不含排队'):('领取→终态 P90 '+fmt(E.duration_p90)+'s · 含排队')],
 [(E.queue_label||'排队')+'占比',pct(E.wait_share),'排队 P50 '+fmt(E.wait_p50)+'s · 样本 '+fmt(E.timing_samples)],
 ['实时线近 24h',fmt(L.live_ingested),'入图（拒绝 '+fmt(L.live_rejected)+'）'],
];
$('bcards').innerHTML=cards.map(c=>'<div class=mc><div class=l>'+c[0]+'</div><div class=v>'+c[1]+'</div><div class=s>'+esc(c[2])+'</div></div>').join('');

function stack(target,rows,label,parts){const box=$(target);
 if(!rows.length){box.innerHTML='<div class=note>暂无数据</div>';return}
 const tot=r=>parts.reduce((a,p)=>a+(p[1](r)||0),0);const peak=Math.max(1,...rows.map(tot));
 box.innerHTML=rows.map(r=>{const t=tot(r);return '<div class=row><span class=k>'+esc(label(r))+'</span><span class=bar>'
  +parts.map(p=>{const v=p[1](r)||0;return v?'<i title="'+p[0]+' '+v+'" style="width:'+(v*100/peak)+'%;background:'+p[2]+'"></i>':''}).join('')
  +'</span><span class=c>'+parts.map(p=>p[0]+' '+(p[1](r)||0)).join(' · ')+'</span></div>'}).join('')}
stack('hourly',(B.hourly||[]).slice().reverse(),r=>r.hour.slice(5).replace('T',' ')+'h',[
 ['积压入图',r=>r.backlog.ingested,'#1D9E75'],['积压拒绝',r=>r.backlog.rejected,'#A8B5B0'],
 ['实时入图',r=>r.live.ingested,'#5B8FF9'],['推迟',r=>r.backlog.deferred+r.live.deferred,'#E8A33D']]);
stack('daily',(B.daily||[]).slice().reverse(),r=>r.day,[
 ['积压线',r=>r['积压线'],'#1D9E75'],['live',r=>r['live 线'],'#5B8FF9'],['其他源',r=>r['其他源'],'#B79CED']]);

const trends=D.key_trends||[];
const trendSpecs=[
 ['入图结果（条）',[['成功','ingested','#1D9E75'],['报错','errors','#D64545']]],
 ['锁等待（秒）',[['均值','lock_wait_avg','#378ADD'],['P90','lock_wait_p90','#E8A33D']]],
 ['提交耗时（秒）',[['均值','commit_avg','#8250C4'],['P50','commit_p50','#1D9E75']]],
 ['提交尝试（次）',[['尝试','commit_attempts','#378ADD'],['冲突','conflicts','#D64545'],['锁外冲突','prevalidated_conflicts','#E8A33D'],['跳过复查','validate_skipped','#1D9E75']]],
 ['冲突率（%）',[['冲突率','conflict_rate','#D64545']]],
 ['模型调用（次）',[['调用','model_calls','#378ADD']]],
 ['模型耗时（秒）',[['已完成均值','call_duration_avg','#8250C4']]],
 ['平均在飞模型调用',[['在飞','model_inflight_avg','#1D9E75']]],
 ['调用 / 成功入图',[['同小时比值','calls_per_ingested','#E8A33D']]],
];
function trendChart(title,series){
 const vals=trends.flatMap(r=>series.map(s=>r[s[1]]).filter(v=>Number.isFinite(v)));
 const peak=Math.max(1,...vals)*1.1,W=440,H=125,L=34,R=8,T=9,B=24;
 const x=i=>L+i*(W-L-R)/Math.max(1,trends.length-1), y=v=>T+(H-T-B)*(1-v/peak);
 const curves=series.map(s=>{let pieces=[],part=[];
  trends.forEach((r,i)=>{const v=r[s[1]];if(Number.isFinite(v))part.push(x(i).toFixed(1)+','+y(v).toFixed(1));
   else if(part.length){pieces.push(part);part=[]}});if(part.length)pieces.push(part);
  return pieces.map(p=>'<polyline points="'+p.join(' ')+'" fill="none" stroke="'+s[2]+'" stroke-width="2" stroke-linejoin="round"/>').join('')
   +trends.map((r,i)=>Number.isFinite(r[s[1]])?'<circle cx="'+x(i).toFixed(1)+'" cy="'+y(r[s[1]]).toFixed(1)+'" r="2.5" fill="'+s[2]+'"><title>'+esc(r.hour)+' UTC · '+esc(s[0])+' '+r[s[1]]+'</title></circle>':'').join('')}).join('');
 const ticks=[0,peak/2,peak].map(v=>'<text x="1" y="'+(y(v)+4).toFixed(1)+'" fill="currentColor" font-size="10">'+(+v.toFixed(1))+'</text>').join('');
 const labels=trends.length?'<text x="'+L+'" y="'+(H-2)+'" fill="currentColor" font-size="10">'+esc(trends[0].hour.slice(5).replace('T',' '))+'</text><text x="'+(W-49)+'" y="'+(H-2)+'" fill="currentColor" font-size="10">'+esc(trends[trends.length-1].hour.slice(5).replace('T',' '))+'</text>':'';
 return '<div class=trend><h3>'+title+'</h3><svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+title+' 最近24小时趋势"><path d="M'+L+' '+T+'V'+(H-B)+'H'+(W-R)+'" stroke="currentColor" opacity=".25" fill="none"/>'+ticks+curves+labels+'</svg><div class=legend>'+series.map(s=>'<span><i style="background:'+s[2]+'"></i>'+s[0]+'</span>').join('')+'</div></div>';
}
$('keytrends').innerHTML=trends.length?trendSpecs.map(s=>trendChart(s[0],s[1])).join(''):'<div class=note>暂无趋势数据</div>';

const names=Object.fromEntries(D.stages.map(s=>[s.id,s.label]));
$('bn').innerHTML=D.bottlenecks.length?D.bottlenecks.map(b=>'<tr><td>'+esc(names[b.stage]||b.stage)+'</td><td><span class="lv '+(b.deliberate?'plan':b.level)+'">'+(b.deliberate?'计划内':(b.level==='stop'?'停流':'慢流'))+'</span></td><td>'+esc(b.title)+'</td><td>'+esc(b.evidence)+'</td><td>'+esc(b.action)+'</td></tr>').join(''):'<tr><td colspan=5 class=note>无</td></tr>';

const TABS=[['topology','拓扑图（实时）','节点颜色=当前状态；虚线粗框=当前卡点；连线上是积压剩余与在飞抽取数。'],
 ['usecase','用例图','谁在这条链路上做什么。'],['flow','流程图','一条 observation 从落库到入图的判定路径，每个菱形都是可能卡住的位置。'],
 ['arch','架构图','按层划分：采集 → 传输 → 精炼 → 服务 → 模型 → 存储 → 使用。'],
 ['app_arch','应用架构图','进程 / 容器 / 共享卷与它们之间的接口。']];
let cur='topology',seq=0;
function show(id){cur=id;document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.id===id));
 const t=TABS.find(x=>x[0]===id);$('dgnote').textContent=t[2];const box=$('diagram');
 if(!window.mermaid){box.innerHTML='<div class=note>图形渲染库加载失败（需能访问 cdn.jsdelivr.net），以下为图的源码：</div><pre class=dt>'+esc(D.diagrams[id])+'</pre>';return}
 window.mermaid.render('mm'+(++seq),D.diagrams[id]).then(r=>{if(cur===id)box.innerHTML=r.svg}).catch(e=>{box.innerHTML='<pre class=dt>'+esc(String(e))+'\n\n'+esc(D.diagrams[id])+'</pre>'})}
$('tabs').innerHTML=TABS.map(t=>'<button data-id="'+t[0]+'">'+t[1]+'</button>').join('');
$('tabs').onclick=e=>{const b=e.target.closest('button');if(b)show(b.dataset.id)};
if(window.mermaid){window.mermaid.initialize({startOnLoad:false,theme:'default',flowchart:{htmlLabels:true,curve:'basis'},securityLevel:'strict'})}
show('topology');
</script></body></html>"""
