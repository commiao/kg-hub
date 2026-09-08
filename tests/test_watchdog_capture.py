"""watchdog 采集链路告警判定的测试。

重点验证「什么该报、什么不该报」这条边界 —— 它是这套告警能不能被长期
信任的关键：对黄灯告警会在一周内把人训练成静音这个群，那时红灯来了也没人看。
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.watchdog as W  # noqa: E402
from tools.watchdog import (CaptureDecision, apply_capture_decision,
                            check_capture_chain, judge_snapshots)  # noqa: E402


def _snap(host="h", age=60, stale=False, blockers=None, overall="green"):
    return {"_host": host, "_age_s": age, "_stale": stale,
            "overall": overall, "blockers": blockers or []}


def _liveness(*, online=(), offline=(), source_state="fresh", age=10):
    """watchdog 消费的独立 NAS/Tailscale 设备存活快照。"""
    devices = {host.lower(): {"state": "online"} for host in online}
    devices.update({host.lower(): {"state": "offline"} for host in offline})
    return {"source_state": source_state, "age_s": age, "devices": devices}


def test_green_silent():
    assert judge_snapshots([_snap()], {}) == CaptureDecision([], [])


def test_amber_silent():
    """黄灯(空闲/滞后)是被观测的常态,绝不能告警。"""
    assert judge_snapshots([_snap(overall="amber")], {}) == CaptureDecision([], [])


def test_red_blocker_fires():
    decision = judge_snapshots(
        [_snap(overall="red", blockers=[{"label": "Mac→NAS", "detail": "落后 900 条"}])], {})
    blocked, stale = decision.blocked, decision.stale
    assert stale == []
    assert len(blocked) == 1 and "Mac→NAS" in blocked[0] and "900" in blocked[0]


def test_stale_probe_fires():
    decision = judge_snapshots(
        [_snap(host="mac", age=7200, stale=True)], {},
        _liveness(online=("mac",)))
    blocked, stale = decision.blocked, decision.stale
    assert blocked == []
    assert len(stale) == 1 and "120 分钟" in stale[0]


def test_stale_suppresses_blocked():
    """探针失联时灯色是旧数据,不该再按它报阻塞 —— 否则一个故障报两条。"""
    decision = judge_snapshots(
        [_snap(age=7200, stale=True, overall="red",
               blockers=[{"label": "x", "detail": "y"}])], {},
        _liveness(online=("h",)))
    blocked, stale = decision.blocked, decision.stale
    assert blocked == [] and len(stale) == 1


def test_offline_device_suppresses_stale_probe():
    """Mac 睡眠/离线时整条采集链自然断线，不应误报探针故障。"""
    decision = judge_snapshots(
        [_snap(host="mac", age=7200, stale=True)], {},
        _liveness(offline=("mac",)))
    blocked, stale = decision.blocked, decision.stale
    assert blocked == [] and stale == []


def test_offline_stale_snapshot_clears_previous_capture_anomalies():
    decision = judge_snapshots(
        [_snap(host="mac", age=7200, stale=True, overall="red",
               blockers=[{"label": "old", "detail": "historical"}])], {},
        _liveness(offline=("mac",)))
    current = {"capture_blocked": False, "capture_probe_stale": False}
    apply_capture_decision(
        decision,
        {"capture_blocked": True, "capture_probe_stale": True},
        current, {})
    assert current == {"capture_blocked": False, "capture_probe_stale": False}


def test_expired_liveness_cannot_masquerade_as_online():
    """旧 Tailscale 快照里的 online 不能当真；设备状态应降级 unknown。"""
    decision = judge_snapshots(
        [_snap(host="mac", age=7200, stale=True)], {},
        _liveness(online=("mac",), source_state="stale", age=600))
    blocked, stale = decision.blocked, decision.stale
    assert blocked is None and stale is None
    assert decision.source_errors


def test_real_capture_host_can_map_to_different_tailscale_identity():
    """uname host 与 tailnet HostName/DNSName 不同，静态身份映射只负责找设备。"""
    cfg = {"capture_device_aliases": {
        "MacBook-Pro-4": ["MacBook Pro (3)", "mac-office"],
    }}
    decision = judge_snapshots(
        [_snap(host="MacBook-Pro-4", age=7200, stale=True)], cfg,
        _liveness(online=("mac-office",)))
    blocked, stale = decision.blocked, decision.stale
    assert blocked == [] and len(stale) == 1


def test_unknown_liveness_holds_stale_and_old_blocker_decisions():
    """unknown 不是健康：不得把上一轮 stale/blocker 清除。"""
    decision = judge_snapshots(
        [_snap(host="MacBook-Pro-4", age=7200, stale=True,
               overall="red", blockers=[{"label": "x", "detail": "old"}])], {},
        _liveness(source_state="stale", age=600))
    assert decision.stale is None
    assert decision.blocked is None
    assert decision.source_errors


def test_fresh_blocker_is_definitive_even_when_liveness_unknown():
    """设备在线信号只门控 probe stale；新鲜 red blocker 仍是明确坏态。"""
    decision = judge_snapshots(
        [_snap(host="MacBook-Pro-4", age=60, overall="red",
               blockers=[{"label": "hook", "detail": "未采集"}])], {},
        _liveness(source_state="missing"))
    assert len(decision.blocked or []) == 1
    assert decision.stale == []


def test_no_snapshot_only_alerts_for_configured_online_host():
    """从未上报也必须先有独立 online 证据；监控 host 清单可静态配置。"""
    cfg = {"capture_probe_hosts": ["mac"]}
    assert judge_snapshots(
        [], cfg, _liveness(offline=("mac",))) == CaptureDecision([], [])
    decision = judge_snapshots([], cfg, _liveness(online=("mac",)))
    blocked, stale = decision.blocked, decision.stale
    assert blocked == [] and len(stale) == 1 and "mac" in stale[0]


def test_no_snapshot_all_configured_hosts_offline_clears_previous_anomalies():
    cfg = {"capture_probe_hosts": ["mac", "win"]}
    decision = judge_snapshots(
        [], cfg, _liveness(offline=("mac", "win")))
    current = {"capture_blocked": False, "capture_probe_stale": False}
    apply_capture_decision(
        decision,
        {"capture_blocked": True, "capture_probe_stale": True},
        current, {})
    assert current == {"capture_blocked": False, "capture_probe_stale": False}


def test_offline_host_does_not_clear_other_host_bad_or_unknown():
    bad = judge_snapshots(
        [_snap(host="sleeping", age=7200, stale=True),
         _snap(host="active", age=60, overall="red",
               blockers=[{"label": "hook", "detail": "broken"}])], {},
        _liveness(offline=("sleeping",), online=("active",)))
    assert len(bad.blocked or []) == 1

    unknown = judge_snapshots(
        [_snap(host="sleeping", age=7200, stale=True),
         _snap(host="mystery", age=7200, stale=True)], {},
        _liveness(offline=("sleeping",)))
    assert unknown.blocked is None and unknown.stale is None


def test_no_snapshot_without_host_identity_holds_previous_stale():
    decision = judge_snapshots([], {}, _liveness(online=("some-other-host",)))
    assert decision.blocked is None and decision.stale is None


def test_cfg_threshold_overrides_server_stale_flag():
    decision = judge_snapshots(
        [_snap(host="mac", age=300, stale=False)],
        {"capture_stale_after_min": 1}, _liveness(online=("mac",)))
    blocked, stale = decision.blocked, decision.stale
    assert len(stale) == 1


def test_non_positive_or_invalid_threshold_matches_dashboard_fallback():
    """非法公开阈值不能让 watchdog 与 dashboard 分裂，统一回退 30 分钟。"""
    for value in (-1, 0, "invalid", None):
        cfg = {"capture_stale_after_min": value}
        fresh = judge_snapshots(
            [_snap(host="mac", age=1799, stale=True)], cfg,
            _liveness(online=("mac",)))
        old = judge_snapshots(
            [_snap(host="mac", age=1801, stale=False)], cfg,
            _liveness(online=("mac",)))
        assert fresh.stale == [], value
        assert len(old.stale or []) == 1, value


def test_multi_host_independent():
    decision = judge_snapshots(
        [_snap(host="mac", overall="red", blockers=[{"label": "a", "detail": "b"}]),
         _snap(host="win", age=99999, stale=True)], {},
        _liveness(online=("mac", "win")))
    blocked, stale = decision.blocked, decision.stale
    assert len(blocked) == 1 and "mac" in blocked[0]
    assert len(stale) == 1 and "win" in stale[0]


def test_normalized_duplicate_host_keeps_newest_snapshot():
    """topology newest-first；旧 mac.local 不能覆盖同一设备的新 Mac 行。"""
    decision = judge_snapshots(
        [_snap(host="Mac", age=60, stale=False),
         _snap(host="mac.local", age=7200, stale=True)], {},
        _liveness(online=("mac",)))
    assert decision == CaptureDecision([], [])


def test_configured_online_host_without_snapshot_is_not_lost_when_others_report():
    cfg = {"capture_probe_hosts": ["mac", "win"]}
    decision = judge_snapshots(
        [_snap(host="mac", age=60)], cfg,
        _liveness(online=("mac", "win")))
    assert decision.blocked == []
    assert len(decision.stale or []) == 1 and "win" in decision.stale[0]


def test_topology_api_failure_is_not_misclassified_as_probe_stale():
    """server/topology 接口异常没有设备 online 证据，不能借用 probe-stale 告警。"""
    response = type("Response", (), {"status_code": 503})()
    with patch("tools.watchdog.httpx.get", return_value=response):
        decision = check_capture_chain({})
        assert decision.blocked is None and decision.stale is None
        assert decision.source_errors
    with patch("tools.watchdog.httpx.get", side_effect=RuntimeError("boom")):
        decision = check_capture_chain({})
        assert decision.blocked is None and decision.stale is None
        assert decision.source_errors
    bad_payload = type("Response", (), {
        "status_code": 200,
        "json": lambda self: {"ok": False, "snapshots": []},
    })()
    with patch("tools.watchdog.httpx.get", return_value=bad_payload):
        decision = check_capture_chain({})
        assert decision.blocked is None and decision.stale is None
        assert decision.source_errors


def test_notify_config_cannot_override_public_capture_threshold():
    response = type("Response", (), {
        "status_code": 200,
        "json": lambda self: {"ok": True, "snapshots": [
            _snap(host="mac", age=300, stale=False),
        ]},
    })()
    public_cfg = {
        "capture_probe_hosts": ["mac"],
        "capture_stale_after_min": 30,
    }
    with (patch("tools.watchdog.httpx.get", return_value=response),
          patch("tools.watchdog.load_config", return_value=public_cfg),
          patch("tools.watchdog.load_status",
                return_value=_liveness(online=("mac",)))):
        decision = check_capture_chain({"capture_stale_after_min": 1})
    assert decision == CaptureDecision([], [])


def test_unknown_decision_holds_previous_capture_anomalies():
    prev = {"capture_blocked": True, "capture_probe_stale": True}
    current = {"capture_blocked": False, "capture_probe_stale": False}
    details = {}
    apply_capture_decision(CaptureDecision(None, None), prev, current, details)
    assert current == prev

    apply_capture_decision(CaptureDecision([], []), prev, current, details)
    assert current == {"capture_blocked": False, "capture_probe_stale": False}


def test_source_failure_is_separate_monitoring_anomaly():
    current = {"capture_blocked": False, "capture_probe_stale": False,
               "capture_monitor_unhealthy": False}
    details = {}
    apply_capture_decision(
        CaptureDecision(None, None, ("Tailscale 快照过期",)),
        {}, current, details)
    assert current["capture_monitor_unhealthy"] is True
    assert current["capture_probe_stale"] is False
    assert "Tailscale 快照过期" in details["capture_monitor_unhealthy"]


# ── 手工 runner(2026-08-25 补)────────────────────────────────────────────
# 本文件是 pytest 风格,但本机没装 pytest。此前直接 `python3 tests/xxx.py`
# 只会**定义函数、一个断言都不执行**,还返回 exit 0 —— 于是它从写下那天起
# 就是个"看起来在跑其实没跑"的假测试,正是本项目一直在消灭的失效模式。
# 加 runner 让它无论有没有 pytest 都真的执行并如实退出码。
# ---------------------------------------------------------------- unknown 沿用上限
#
# 2026-09-03：探针 POST kg-hub 收到 55 次 502，每次都让本轮不可判 → 无限沿用旧
# blocked → capture_blocked 表现为抖动/粘滞。沿用是为了防假恢复，但不能无限。


def _unknown_round(prev_bad, streak_before, kind="capture_blocked"):
    prev = {kind: prev_bad}
    cur, details = {}, {}
    prev_counters = {f"{kind}_unknown": streak_before}
    new_counters = {}
    decision = (CaptureDecision(None, None) if kind == "capture_blocked"
                else CaptureDecision([], None))
    apply_capture_decision(decision, prev, cur, details, prev_counters, new_counters)
    return cur, details, new_counters


def test_unknown_holds_within_cap():
    for streak_before in range(0, W.CAPTURE_UNKNOWN_HOLD_ROUNDS):
        cur, details, counters = _unknown_round(True, streak_before)
        assert cur["capture_blocked"] is True, f"第 {streak_before+1} 轮不该释放"
        assert "沿用" in details["capture_blocked"]
        assert counters["capture_blocked_unknown"] == streak_before + 1


def test_unknown_releases_past_cap():
    cur, details, _ = _unknown_round(True, W.CAPTURE_UNKNOWN_HOLD_ROUNDS)
    assert cur["capture_blocked"] is False, "越过上限必须停止沿用"
    assert "capture_blocked:clear" in details


def test_release_message_does_not_claim_resolved():
    """越过上限是"放弃判定"，不是"已修复"。发成 resolved 就是又一条撒谎的信号。"""
    _, details, _ = _unknown_round(True, W.CAPTURE_UNKNOWN_HOLD_ROUNDS + 3)
    msg = details["capture_blocked:clear"]
    assert "不等于故障已修复" in msg
    assert "capture_monitor_unhealthy" in msg or "capture_probe_stale" in msg


def test_known_decision_resets_streak():
    prev = {"capture_blocked": True}
    cur, details, new_counters = {}, {}, {}
    apply_capture_decision(CaptureDecision([], []), prev, cur, details,
                           {"capture_blocked_unknown": 9}, new_counters)
    assert new_counters["capture_blocked_unknown"] == 0, "拿到确定判定就该清零"
    assert cur["capture_blocked"] is False


def test_unknown_when_previously_clean_stays_clean():
    cur, details, _ = _unknown_round(False, 0)
    assert cur["capture_blocked"] is False
    assert "capture_blocked" not in details


def test_stale_branch_has_same_cap():
    prev = {"capture_probe_stale": True}
    cur, details, counters = {}, {}, {}
    apply_capture_decision(CaptureDecision([], None), prev, cur, details,
                           {"capture_probe_stale_unknown": W.CAPTURE_UNKNOWN_HOLD_ROUNDS},
                           counters)
    assert cur["capture_probe_stale"] is False
    assert "capture_probe_stale:clear" in details


def test_backward_compatible_without_counters():
    """旧调用方(4 个位置参数)不传 counters 时仍按单轮沿用，不炸。"""
    prev = {"capture_blocked": True}
    cur, details = {}, {}
    apply_capture_decision(CaptureDecision(None, None), prev, cur, details)
    assert cur["capture_blocked"] is True


# ── stats 类判据:server 不可达时不得假 CLEAR(T-0059 问题②)──────────────
# 2026-09-07 实测:每次重建 kg_hub_server,alerts.log 都出现
#   [FIRE] server_down → [CLEAR] recent_errors(假) → [CLEAR] extraction_failing(假)
#   → [CLEAR] server_down → [FIRE] recent_errors → [FIRE] extraction_failing
# 1 条真告警配 4 条噪音。根因:这两项判据在 stats 取不到时被算成 False,
# edge-trigger 把 True→False 读成"已恢复"。


def _run_main(*, alive, stats, prev_anomalies, prev_counters=None, cfg=None):
    """驱动 main() 一轮,返回 (保存的 state, 发出的告警列表)。

    告警列表元素为 (severity, kind, message)。capture 链路关掉,避免与本组无关的
    判定混入;last_run 必须非 None,否则 main 会进 60s 开机宽限期。
    """
    saved = {}
    alerts = []
    state = {"anomalies": dict(prev_anomalies),
             "counters": dict(prev_counters or {}),
             "last_run": "2026-09-07T00:00:00+00:00"}
    conf = {"capture_chain_enabled": False}
    conf.update(cfg or {})
    with (patch("tools.watchdog.load_notify_config", return_value=conf),
          patch("tools.watchdog.load_state", return_value=state),
          patch("tools.watchdog.save_state", side_effect=lambda s: saved.update(s)),
          patch("tools.watchdog.check_disk_temp", return_value=(None, "")),
          patch("tools.watchdog.check_health",
                return_value=(alive, "ok" if alive else "ConnectError: refused")),
          patch("tools.watchdog.check_queue", return_value=(stats, "ok")),
          patch("tools.watchdog.check_search_probe", return_value=("skip", 0.0, "")),
          patch("tools.watchdog.check_gateway_monitor", return_value={
              name: False for name in W.GATEWAY_ALERTS}),
          patch("tools.watchdog.emit_alert",
                side_effect=lambda sev, kind, msg: alerts.append((sev, kind, msg)))):
        W.main()
    return saved, alerts


def _clear_kinds(alerts):
    return {kind for sev, kind, _ in alerts if sev == "clear"}


def test_server_down_holds_recent_errors():
    """server 不可达 → recent_errors 沿用上一轮 True,不发 CLEAR,计数 1。"""
    saved, alerts = _run_main(alive=False, stats=None,
                              prev_anomalies={"recent_errors": True})
    assert saved["anomalies"]["recent_errors"] is True
    assert "recent_errors" not in _clear_kinds(alerts)
    assert saved["counters"]["recent_errors_unknown"] == 1


def test_server_down_holds_extraction_failing():
    saved, alerts = _run_main(alive=False, stats=None,
                              prev_anomalies={"extraction_failing": True})
    assert saved["anomalies"]["extraction_failing"] is True
    assert "extraction_failing" not in _clear_kinds(alerts)
    assert saved["counters"]["extraction_failing_unknown"] == 1


def test_server_down_still_fires_server_down():
    """真信号不受影响:server_down 该报还得报。"""
    _saved, alerts = _run_main(alive=False, stats=None, prev_anomalies={})
    assert ("fire", "server_down") in [(s, k) for s, k, _ in alerts]


def test_recent_errors_released_after_hold_limit():
    """连续不可判越过上限 → 释放,且 CLEAR 文案明说这不是恢复。"""
    limit = W.CAPTURE_UNKNOWN_HOLD_ROUNDS
    saved, alerts = _run_main(
        alive=False, stats=None,
        prev_anomalies={"recent_errors": True},
        prev_counters={"recent_errors_unknown": limit})
    assert saved["anomalies"]["recent_errors"] is False
    msg = [m for s, k, m in alerts if s == "clear" and k == "recent_errors"]
    assert msg, "越限后应发 CLEAR"
    assert "不等于故障已修复" in msg[0]
    assert msg[0] != "resolved"


def test_extraction_failing_released_after_hold_limit():
    limit = W.CAPTURE_UNKNOWN_HOLD_ROUNDS
    saved, alerts = _run_main(
        alive=False, stats=None,
        prev_anomalies={"extraction_failing": True},
        prev_counters={"extraction_failing_unknown": limit})
    assert saved["anomalies"]["extraction_failing"] is False
    msg = [m for s, k, m in alerts if s == "clear" and k == "extraction_failing"]
    assert msg and "不等于故障已修复" in msg[0]


def test_recovered_clears_normally_with_resolved():
    """server 恢复且错误真降到阈值下 → 正常 CLEAR,文案是 resolved。"""
    stats = {"pending": 0, "errored_last_1h": 0, "errored_total": 0,
             "oldest_pending_age_seconds": None}
    saved, alerts = _run_main(alive=True, stats=stats,
                              prev_anomalies={"recent_errors": True,
                                              "extraction_failing": True})
    assert saved["anomalies"]["recent_errors"] is False
    assert saved["anomalies"]["extraction_failing"] is False
    cleared = {k: m for s, k, m in alerts if s == "clear"}
    assert cleared.get("recent_errors") == "resolved"
    assert cleared.get("extraction_failing") == "resolved"
    # 取到数 → unknown 计数清零
    assert saved["counters"]["recent_errors_unknown"] == 0
    assert saved["counters"]["extraction_failing_unknown"] == 0


def test_stats_unavailable_while_alive_also_holds():
    """server 活着但 queue_stats 取数失败,同样不可判 → 沿用,不假 CLEAR。"""
    saved, alerts = _run_main(alive=True, stats=None,
                              prev_anomalies={"extraction_failing": True})
    assert saved["anomalies"]["extraction_failing"] is True
    assert "extraction_failing" not in _clear_kinds(alerts)


def test_deploy_sequence_emits_only_server_down():
    """回归本次事故序列:一次部署只该有 server_down 一条,不再有 4 条噪音。"""
    prev = {"recent_errors": True, "extraction_failing": True}
    # 部署中:server 不可达
    saved, alerts_down = _run_main(alive=False, stats=None, prev_anomalies=prev)
    assert _clear_kinds(alerts_down) == set(), "不可达轮不得发任何 CLEAR"
    assert [k for s, k, _ in alerts_down if s == "fire"] == ["server_down"]
    # 恢复:错误仍在(真故障未修) → 不该 re-FIRE(状态一直是 True,无跳变)
    stats = {"pending": 0, "errored_last_1h": 71, "errored_total": 110,
             "oldest_pending_age_seconds": None}
    _saved2, alerts_up = _run_main(alive=True, stats=stats,
                                   prev_anomalies=saved["anomalies"],
                                   prev_counters=saved["counters"])
    fired = [k for s, k, _ in alerts_up if s == "fire"]
    assert "recent_errors" not in fired and "extraction_failing" not in fired
    assert ("clear", "server_down") in [(s, k) for s, k, _ in alerts_up]


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    ok = fail = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ✅ {name}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ {name}: {type(exc).__name__}: {exc}")
            fail += 1
    print(f"\n{ok} passed, {fail} failed")
    sys.exit(1 if fail else 0)
