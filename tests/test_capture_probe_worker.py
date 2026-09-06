"""claude-mem worker 不能只看 HTTP 进程存活。

真实事故（T-0021，2026-08-26）：worker /api/health 持续返回 ok，但 Claude SDK
因 Gateway token 过期反复 401；hook 仍在入队，observation 却两天没有落库。
只有“最新鉴权失败是否已被更新的 observation 证明恢复”才是生成链路的真信号。
"""
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.capture_probe as P  # noqa: E402


def _write_db(path: Path, observation_epoch_s: float) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE observations (created_at_epoch INTEGER NOT NULL)")
    con.execute("INSERT INTO observations VALUES (?)", (int(observation_epoch_s * 1000),))
    con.commit()
    con.close()


def _health():
    return {
        "status": "ok",
        "version": "13.15.0",
        "pid": 123,
        "uptime": 3600,
        "ai": {"authMethod": "Gateway auth token"},
    }


def _run(failure_epoch_s: float, observation_epoch_s: float):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log_dir = root / "logs"
        log_dir.mkdir()
        stamp = datetime.fromtimestamp(failure_epoch_s).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        (log_dir / "claude-mem-test.log").write_text(
            f"[{stamp}] [INFO ] [WORKER] Broadcasting processing status "
            "{isProcessing=true, queueDepth=1139, activeSessions=28}\n"
            f"[{stamp}] [ERROR] [PARSER] SDK authentication failed; "
            "API Error: 401 invalid_api_key\n"
        )
        db = root / "claude-mem.db"
        _write_db(db, observation_epoch_s)

        old_log_dir, old_db, old_http = P.CM_LOG_DIR, P.CM_DB, P.http_json
        P.CM_LOG_DIR, P.CM_DB = log_dir, db
        P.http_json = lambda *a, **k: (_health(), None)
        try:
            return P.probe_worker()
        finally:
            P.CM_LOG_DIR, P.CM_DB, P.http_json = old_log_dir, old_db, old_http


def test_health_ok_but_unrecovered_auth_failure_is_red():
    now = time.time()
    node = _run(failure_epoch_s=now - 60, observation_epoch_s=now - 3600)
    assert node["state"] == P.RED
    assert "401" in node["detail"]
    assert "1139" in node["detail"]


def test_new_observation_after_auth_failure_proves_recovery():
    now = time.time()
    node = _run(failure_epoch_s=now - 3600, observation_epoch_s=now - 60)
    assert node["state"] == P.GREEN


# ---------------------------------------------------------------- 产出判据
#
# 2026-08-28 起真实故障形态从 401 换成 400 anthropic-beta，而判据锁死在
# "SDK authentication failed" 字面量上 → 采集静默 5.5 天面板全绿。
# 以下用例回放各种失败形态，钉住「判产出本身、不枚举原因」。


def _run_lines(log_lines, observation_epoch_s, *, health=None):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log_dir = root / "logs"
        log_dir.mkdir()
        (log_dir / "claude-mem-test.log").write_text("".join(log_lines))
        db = root / "claude-mem.db"
        _write_db(db, observation_epoch_s)
        old = (P.CM_LOG_DIR, P.CM_DB, P.http_json)
        P.CM_LOG_DIR, P.CM_DB = log_dir, db
        P.http_json = lambda *a, **k: (health or _health(), None)
        try:
            return P.probe_worker()
        finally:
            P.CM_LOG_DIR, P.CM_DB, P.http_json = old


def _discards(n, epoch_s, body):
    """造 n 条「批次被丢弃」日志行（outputClass 非 xml/idle）。"""
    stamp = datetime.fromtimestamp(epoch_s).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return [f"[{stamp}] [ERROR] [PARSER] [session-1] {body} "
            "{outputClass=prose, preview=...}\n" for _ in range(n)]


def test_beta_400_stall_is_red_even_though_no_auth_failure_string():
    """真实故障回放：credvault 网关拒 anthropic-beta。旧判据对此完全盲。"""
    now = time.time()
    node = _run_lines(
        _discards(8, now - 60,
                  'API Error: 400 {"type":"invalid_request_error",'
                  '"message":"anthropic-beta 包含未允许的功能"}'),
        observation_epoch_s=now - 7200)
    assert node["state"] == P.RED
    assert "400" in node["detail"], node["detail"]
    assert "401" not in node["detail"], "不得再硬编码 401；原因须取自日志实况"


def test_gateway_502_stall_is_red():
    now = time.time()
    node = _run_lines(_discards(6, now - 30, "HTTP 502 Bad Gateway from upstream"),
                      observation_epoch_s=now - 3600)
    assert node["state"] == P.RED
    assert "502" in node["detail"]


def test_prompt_too_long_stall_is_red():
    """8/27 真实事故：超大工具输出撑爆提取会话，整批丢弃。"""
    now = time.time()
    node = _run_lines(_discards(9, now - 45, "Prompt is too long"),
                      observation_epoch_s=now - 5400)
    assert node["state"] == P.RED
    assert "Prompt is too long" in node["detail"]


def test_idle_machine_is_not_red():
    """空闲免疫：没有流量就没有丢弃，observation 再旧也不判红。

    这正是此前三轮误报的病根（阈值落在正常作息分布里），不能再犯第四次。
    """
    now = time.time()
    stamp = datetime.fromtimestamp(now - 60).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    node = _run_lines(
        [f"[{stamp}] [INFO ] [WORKER] Broadcasting processing status "
         "{isProcessing=false, queueDepth=0}\n"],
        observation_epoch_s=now - 86400 * 2)   # 两天没新观察，但也没人干活
    assert node["state"] == P.GREEN, node["detail"]


def test_occasional_discard_below_threshold_is_not_red():
    """偶发丢弃不是故障。弱模型本来就会零星不遵守输出契约。"""
    now = time.time()
    node = _run_lines(_discards(P.GENERATION_STALL_DISCARDS - 1, now - 60,
                                "API Error: 400 whatever"),
                      observation_epoch_s=now - 3600)
    assert node["state"] == P.GREEN, node["detail"]


def test_discards_older_than_last_observation_are_water_under_bridge():
    """一次成功落库即证明恢复；更早的丢弃不该继续压着判红。"""
    now = time.time()
    node = _run_lines(_discards(20, now - 7200, "API Error: 400 old failure"),
                      observation_epoch_s=now - 60)
    assert node["state"] == P.GREEN, node["detail"]


def test_xml_and_idle_are_not_counted_as_discards():
    """outputClass=xml 是成功、idle 是无事可做，都不能算进丢弃。"""
    now = time.time()
    stamp = datetime.fromtimestamp(now - 60).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    lines = [f"[{stamp}] [INFO ] [PARSER] ok {{outputClass=xml}}\n" for _ in range(10)]
    lines += [f"[{stamp}] [INFO ] [PARSER] nothing {{outputClass=idle}}\n"
              for _ in range(10)]
    node = _run_lines(lines, observation_epoch_s=now - 3600)
    assert node["state"] == P.GREEN, node["detail"]


def test_stall_detail_reports_discard_count_and_queue():
    now = time.time()
    stamp = datetime.fromtimestamp(now - 60).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    lines = [f"[{stamp}] [INFO ] [WORKER] status {{queueDepth=42}}\n"]
    lines += _discards(7, now - 60, "API Error: 400 boom")
    node = _run_lines(lines, observation_epoch_s=now - 3600)
    assert node["state"] == P.RED
    assert "7 批" in node["detail"], node["detail"]
    assert "42" in node["detail"], node["detail"]
    assert node["metrics"]["discards_since_observation"] == 7


def test_health_unreachable_still_red():
    """进程存活仍是独立信号，不能被产出判据取代。"""
    old = P.http_json
    P.http_json = lambda *a, **k: (None, "URLError")
    try:
        node = P.probe_worker()
    finally:
        P.http_json = old
    assert node["state"] == P.RED
    assert ":37701" in node["detail"]


if __name__ == "__main__":
    fns = [(name, fn) for name, fn in sorted(globals().items())
           if name.startswith("test_") and callable(fn)]
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
