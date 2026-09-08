"""Read-only dashboard signals: readiness, scheduled pauses and throughput.

Only GET /health/ready is used; never send prompts or provider requests.
"""
from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime, timezone

import httpx

_health_cache: tuple[float, dict] | None = None
_health_lock = asyncio.Lock()

_MONITOR_CHECKS = {'process', 'state_manifest', 'routes', 'credentials', 'callers',
                   'idempotency', 'historical_outcomes', 'rollback_witness',
                   'cost_state', 'passive_provider'}


def gateway_monitor_projection(body: object, http_status: int, sampled: str) -> dict:
    """Fixed metadata only; no response text, credential, prompt or exception.

    The old gateway called all armed markers write_failed. Only positive local
    write evidence proves persistence failure; an unresolved outcome is neither
    an invalid key nor proof that all business requests stopped.
    """
    result = {'version': 1, 'checked_at': sampled, 'external_calls': 0,
              'source_ok': False, 'not_ready': None, 'outcome_unresolved': None,
              'persistence_failed': None, 'authentication_failed': None,
              'provider_failed': None}
    if not isinstance(body, dict) or type(body.get('external_calls')) is not int or body['external_calls'] != 0:
        return result
    checks = body.get('checks')
    if (http_status not in (200, 503) or body.get('status') not in ('ok', 'error')
            or not isinstance(checks, dict) or set(checks) != _MONITOR_CHECKS):
        return result
    for name, check in checks.items():
        if (not isinstance(check, dict) or check.get('status') not in ('ok', 'error', 'degraded')
                or not isinstance(check.get('issues', [] if name == 'process' else None), list)
                or not all(isinstance(v, str) for v in check.get('issues', []))):
            return result
    passive = checks['passive_provider']
    businesses = passive.get('business_keys')
    if not isinstance(businesses, dict):
        return result
    for key, entry in businesses.items():
        if (not isinstance(key, str) or not key or not isinstance(entry, dict)
                or set(entry) != {'status', 'at'} or entry['status'] not in
                {'success', 'authentication_failed', 'request_rejected', 'degraded', 'provider_failed'}
                or not isinstance(entry['at'], str)):
            return result
        try:
            if datetime.fromisoformat(entry['at'].replace('Z', '+00:00')).tzinfo is None:
                return result
        except ValueError:
            return result
    metrics = passive.get('degraded_metrics', {})
    if not isinstance(metrics, dict):
        return result
    for name in ('provider_state_write_failed_business_keys', 'provider_outcome_unresolved_business_keys',
                 'provider_state_write_degraded_business_keys'):
        if name in metrics and (not isinstance(metrics[name], list) or
                not all(isinstance(key, str) and key for key in metrics[name])):
            return result
    for name in ('provider_state_degradation_marker_issue', 'provider_state_write_degraded'):
        if name in metrics and type(metrics[name]) is not bool:
            return result
    issues = {issue for check in checks.values() for issue in check.get('issues', [])}
    if issues & {'provider_state_unavailable', 'provider_pending_state_unavailable'}:
        # An unreadable passive ledger cannot clear a previously observed 401.
        return result
    local_failures = metrics.get('provider_state_write_failures_total', 0)
    if type(local_failures) is not int or local_failures < 0:
        return result
    write_issue = 'provider_state_write_failed' in issues
    if write_issue and local_failures > 0 and 'provider_state_write_failed_business_keys' not in metrics:
        # Legacy total is cumulative: a recovered old write error followed by
        # an armed unknown cannot prove a current persistence failure.
        return result
    persistence = write_issue and bool(metrics.get('provider_state_write_failed_business_keys'))
    # Compatibility for the pre-diagnostic generation: count zero + readable
    # armed marker keys is unresolved outcome evidence, not an I/O failure.
    legacy_unknown = (write_issue and not persistence and
                      bool(metrics.get('provider_state_write_degraded_business_keys')) and
                      metrics.get('provider_state_degradation_marker_issue') is False)
    result.update(source_ok=True,
        not_ready=(http_status != 200 or body['status'] != 'ok' or
                   any(c['status'] != 'ok' or c.get('issues', []) for c in checks.values())),
        outcome_unresolved=(legacy_unknown or bool(issues & {'provider_outcome_unresolved',
            'idempotency_outcome_unresolved', 'rollback_witness_preflight_unresolved'})),
        persistence_failed=persistence,
        authentication_failed=any(e['status'] == 'authentication_failed' for e in businesses.values()),
        provider_failed=any(e['status'] in {'request_rejected', 'provider_failed'} for e in businesses.values()))
    return result


async def gateway_health() -> dict:
    global _health_cache
    async with _health_lock:
        if _health_cache and time.monotonic() - _health_cache[0] < 60:
            return copy.deepcopy(_health_cache[1])
        from model_gateway_client import gateway_base_url
        sampled = datetime.now(timezone.utc).isoformat()
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False,
                                         follow_redirects=False) as client:
                response = await client.get(gateway_base_url() + "/health/ready")
            body = response.json()
            checks = body.get("checks", {})
            issues = [str(issue) for check in checks.values()
                      if isinstance(check, dict) for issue in check.get("issues", [])]
            healthy = (response.status_code == 200 and body.get("status") == "ok")
            recovery = checks.get("rollback_witness", {})
            historical = checks.get("historical_outcomes", {})
            # Categories overlap: a request may have all three ledger records.
            # Do not add marker counts and mislabel the sum as unique requests.
            quarantined = recovery.get("quarantined_count", 0)
            historical_warning = bool(quarantined or historical.get("idempotency_count", 0)
                                      or historical.get("provider_marker_count", 0))
            result = {"state": ("amber" if historical_warning else "green") if healthy else "red",
                      "monitor": gateway_monitor_projection(body, response.status_code, sampled),
                      "http_status": response.status_code, "checked_at": sampled,
                      "issues": issues,
                      "quarantined_count": quarantined,
                      "historical_outcomes": historical,
                      "quarantined": recovery.get("quarantined", []),
                      "provider_last_results": checks.get("passive_provider", {}).get("business_keys", {})}
        except Exception as exc:
            result = {"state": "grey", "http_status": None,
                      "checked_at": sampled, "issues": [type(exc).__name__]}
        _health_cache = (time.monotonic(), result)
        return copy.deepcopy(result)


def apply_gateway_health(node: dict, edge: dict, health: dict) -> None:
    node["metrics"]["readiness"] = health
    quota_state = node["state"]
    node["metrics"]["quota_state"] = quota_state
    http = health.get("http_status")
    if health["state"] == "red":
        node["state"] = "red"
        summary = f"健康检查异常 · HTTP {http}"
    elif health["state"] == "grey":
        node["state"] = "red" if quota_state == "red" else "grey"
        summary = "健康状态未知"
    elif health["state"] == "amber":
        node["state"] = "red" if quota_state == "red" else "amber"
        summary = "可用 · 有历史隔离登记"
    else:
        summary = "健康检查通过"
    issue_text = "；".join(health.get("issues", []))
    if "rollback_witness_preflight_unresolved" in health.get("issues", []):
        issue_text = "存在未收敛的调用前记录；健康检查异常不代表全部调用已停止。" + issue_text
    node["sub"] = f"健康异常 {http}" if health["state"] == "red" else summary
    node["detail"] = (summary + "\n" + issue_text + "\n配额：\n" + node["detail"]
                      + "\n检查时间：" + str(health.get("checked_at")))
    if health.get("quarantined_count") or health.get("historical_outcomes", {}).get("recovery_state") == "quarantined":
        node["detail"] += "\n历史隔离清单登记（不是当前待处理数），类别可能重叠；原始记录是否仍在当前库需另行核实，禁止自动重放。"
        historical = health.get("historical_outcomes", {})
        node["detail"] += (f"\n调用前见证 {health.get('quarantined_count', 0)} 条；"
                           f"登记的未决请求 {historical.get('idempotency_count', 0)} 条；"
                           f"供应商标记 {historical.get('provider_marker_count', 0)} 条（可能对应同一请求，不相加）。")
        for entry in health.get("quarantined", []):
            node["detail"] += f"\n隔离记录 {entry.get('business_key')}：{entry.get('reserved_at')}"
    for key, result in health.get("provider_last_results", {}).items():
        node["detail"] += (f"\n最近调用 {key}：{result.get('status')} "
                           f"{result.get('at')}（历史结果，不是本次探测）")
    edge["state"] = node["state"]


def refinery_activity(status: dict, now: datetime) -> dict:
    try:
        at = datetime.fromisoformat(str(status.get("heartbeat_at")).replace("Z", "+00:00"))
        age = (now - at).total_seconds()
    except (ValueError, TypeError):
        return {"state": "grey", "label": "状态未知：缺少有效心跳"}
    if age < -60:
        return {"state": "grey", "label": "状态未知：心跳时间异常"}
    if age > 900:
        return {"state": "red", "label": "异常：超过 15 分钟未收到心跳"}
    if status.get("last_error"):
        return {"state": "red", "label": "处理异常：" + str(status["last_error"])[:160]}
    if status.get("thermal_hold"):
        return {"state": "amber", "label": "温度保护暂停"}
    if status.get("idle_outside_window"):
        return {"state": "amber", "label": "计划暂停：等待工作窗口"}
    if status.get("quota_paused"):
        return {"state": "red", "label": "配额耗尽，暂停提交"}
    return {"state": "green", "label": "工作窗口内；以入图完成量判断进展"}


def pipeline_signal(status: dict, now: datetime, month: int, pending: int | None) -> dict:
    activity = refinery_activity(status, now)
    low = isinstance(pending, int) and pending > 0 and month * 100 < pending
    return {"activity": activity, "stalled": activity["state"] == "red",
            "low_throughput": low,
            "throughput_note": "本月已完成量不足待处理量的 1%，消化速度偏低" if low else ""}
