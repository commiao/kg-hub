"""watchdog 采集链路告警判定的测试。

重点验证「什么该报、什么不该报」这条边界 —— 它是这套告警能不能被长期
信任的关键：对黄灯告警会在一周内把人训练成静音这个群，那时红灯来了也没人看。
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    assert judge_snapshots([_snap()], {}) == ([], [])


def test_amber_silent():
    """黄灯(空闲/滞后)是被观测的常态,绝不能告警。"""
    assert judge_snapshots([_snap(overall="amber")], {}) == ([], [])


def test_red_blocker_fires():
    blocked, stale = judge_snapshots(
        [_snap(overall="red", blockers=[{"label": "Mac→NAS", "detail": "落后 900 条"}])], {})
    assert stale == []
    assert len(blocked) == 1 and "Mac→NAS" in blocked[0] and "900" in blocked[0]


def test_stale_probe_fires():
    blocked, stale = judge_snapshots(
        [_snap(host="mac", age=7200, stale=True)], {},
        _liveness(online=("mac",)))
    assert blocked == []
    assert len(stale) == 1 and "120 分钟" in stale[0]


def test_stale_suppresses_blocked():
    """探针失联时灯色是旧数据,不该再按它报阻塞 —— 否则一个故障报两条。"""
    blocked, stale = judge_snapshots(
        [_snap(age=7200, stale=True, overall="red",
               blockers=[{"label": "x", "detail": "y"}])], {},
        _liveness(online=("h",)))
    assert blocked == [] and len(stale) == 1


def test_offline_device_suppresses_stale_probe():
    """Mac 睡眠/离线时整条采集链自然断线，不应误报探针故障。"""
    blocked, stale = judge_snapshots(
        [_snap(host="mac", age=7200, stale=True)], {},
        _liveness(offline=("mac",)))
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
    blocked, stale = judge_snapshots(
        [_snap(host="mac", age=7200, stale=True)], {},
        _liveness(online=("mac",), source_state="stale", age=600))
    assert blocked is None and stale is None


def test_real_capture_host_can_map_to_different_tailscale_identity():
    """uname host 与 tailnet HostName/DNSName 不同，静态身份映射只负责找设备。"""
    cfg = {"capture_device_aliases": {
        "MacBook-Pro-4": ["MacBook Pro (3)", "mac-office"],
    }}
    blocked, stale = judge_snapshots(
        [_snap(host="MacBook-Pro-4", age=7200, stale=True)], cfg,
        _liveness(online=("mac-office",)))
    assert blocked == [] and len(stale) == 1


def test_unknown_liveness_holds_stale_and_old_blocker_decisions():
    """unknown 不是健康：不得把上一轮 stale/blocker 清除。"""
    decision = judge_snapshots(
        [_snap(host="MacBook-Pro-4", age=7200, stale=True,
               overall="red", blockers=[{"label": "x", "detail": "old"}])], {},
        _liveness(source_state="stale", age=600))
    assert decision.stale is None
    assert decision.blocked is None


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
    assert judge_snapshots([], cfg, _liveness(offline=("mac",))) == ([], [])
    blocked, stale = judge_snapshots([], cfg, _liveness(online=("mac",)))
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
    blocked, stale = judge_snapshots(
        [_snap(host="mac", age=300, stale=False)],
        {"capture_stale_after_min": 1}, _liveness(online=("mac",)))
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
    blocked, stale = judge_snapshots(
        [_snap(host="mac", overall="red", blockers=[{"label": "a", "detail": "b"}]),
         _snap(host="win", age=99999, stale=True)], {},
        _liveness(online=("mac", "win")))
    assert len(blocked) == 1 and "mac" in blocked[0]
    assert len(stale) == 1 and "win" in stale[0]


def test_normalized_duplicate_host_keeps_newest_snapshot():
    """topology newest-first；旧 mac.local 不能覆盖同一设备的新 Mac 行。"""
    decision = judge_snapshots(
        [_snap(host="Mac", age=60, stale=False),
         _snap(host="mac.local", age=7200, stale=True)], {},
        _liveness(online=("mac",)))
    assert decision == ([], [])


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
    with patch("tools.watchdog.httpx.get", side_effect=RuntimeError("boom")):
        decision = check_capture_chain({})
        assert decision.blocked is None and decision.stale is None
    bad_payload = type("Response", (), {
        "status_code": 200,
        "json": lambda self: {"ok": False, "snapshots": []},
    })()
    with patch("tools.watchdog.httpx.get", return_value=bad_payload):
        decision = check_capture_chain({})
        assert decision.blocked is None and decision.stale is None


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
    assert decision == ([], [])


def test_unknown_decision_holds_previous_capture_anomalies():
    prev = {"capture_blocked": True, "capture_probe_stale": True}
    current = {"capture_blocked": False, "capture_probe_stale": False}
    details = {}
    apply_capture_decision(CaptureDecision(None, None), prev, current, details)
    assert current == prev

    apply_capture_decision(CaptureDecision([], []), prev, current, details)
    assert current == {"capture_blocked": False, "capture_probe_stale": False}


# ── 手工 runner(2026-08-25 补)────────────────────────────────────────────
# 本文件是 pytest 风格,但本机没装 pytest。此前直接 `python3 tests/xxx.py`
# 只会**定义函数、一个断言都不执行**,还返回 exit 0 —— 于是它从写下那天起
# 就是个"看起来在跑其实没跑"的假测试,正是本项目一直在消灭的失效模式。
# 加 runner 让它无论有没有 pytest 都真的执行并如实退出码。
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
