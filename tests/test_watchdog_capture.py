"""watchdog 采集链路告警判定的测试。

重点验证「什么该报、什么不该报」这条边界 —— 它是这套告警能不能被长期
信任的关键：对黄灯告警会在一周内把人训练成静音这个群，那时红灯来了也没人看。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.watchdog import judge_snapshots  # noqa: E402


def _snap(host="h", age=60, stale=False, blockers=None, overall="green"):
    return {"_host": host, "_age_s": age, "_stale": stale,
            "overall": overall, "blockers": blockers or []}


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
    blocked, stale = judge_snapshots([_snap(age=7200, stale=True)], {})
    assert blocked == []
    assert len(stale) == 1 and "120 分钟" in stale[0]


def test_stale_suppresses_blocked():
    """探针失联时灯色是旧数据,不该再按它报阻塞 —— 否则一个故障报两条。"""
    blocked, stale = judge_snapshots(
        [_snap(age=7200, stale=True, overall="red",
               blockers=[{"label": "x", "detail": "y"}])], {})
    assert blocked == [] and len(stale) == 1


def test_no_snapshot_is_stale_not_silence():
    """一条快照都没有 = 探针从没跑过,必须报,不能当成'没问题'。"""
    blocked, stale = judge_snapshots([], {})
    assert blocked == [] and len(stale) == 1


def test_cfg_threshold_overrides_server_stale_flag():
    blocked, stale = judge_snapshots([_snap(age=300, stale=False)],
                                     {"capture_stale_after_min": 1})
    assert len(stale) == 1


def test_multi_host_independent():
    blocked, stale = judge_snapshots(
        [_snap(host="mac", overall="red", blockers=[{"label": "a", "detail": "b"}]),
         _snap(host="win", age=99999, stale=True)], {})
    assert len(blocked) == 1 and "mac" in blocked[0]
    assert len(stale) == 1 and "win" in stale[0]
