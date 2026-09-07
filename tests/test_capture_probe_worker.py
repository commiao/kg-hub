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


def test_observer_tool_input_mentioning_marker_is_not_a_discard():
    """2026-09-07 真实误报：探针把**自己会话的调试命令**数成了 10 条丢弃。

    claude-mem 的 hook 把每条工具调用原文写进日志（多行 dump，续行无时间戳）；
    任何排查过 outputClass 的会话都会把标记串写进去。判据只能认 [PARSER] 的
    结构化记录，续行一律跳过 —— 否则观测者的动作会污染被观测的信号。
    """
    now = time.time()
    stamp = datetime.fromtimestamp(now - 30).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    lines = [
        # 带时间戳的 QUEUE 记录，工具原文里含标记串 —— 不是 PARSER，不算
        f"[{stamp}] [INFO ] [QUEUE ] [session-524] ENQUEUED | tool=Bash(grep -c "
        "'outputClass=prose' \"$L\" | 成功 xml: $(grep -c 'outputClass=xml' \"$L\")) | depth=21\n",
        # 多行 dump 的续行：无时间戳，含标记串 / queueDepth / Timeout 字样 —— 全部跳过
        "    if 'outputClass=prose' not in line: continue\n",
        "    grep -oE 'queueDepth=999' | TimeoutError\n",
        "    'outputClass=prose' \"$f\" | grep -c 'Request interrupted by user'\n",
    ] * 4  # 16 行污染，远超阈值
    # 唯一一条真实 WORKER 队列深度记录
    lines.append(f"[{stamp}] [INFO ] [WORKER] Broadcasting processing status {{queueDepth=3}}\n")
    node = _run_lines(lines, observation_epoch_s=now - 3600)
    assert node["state"] == P.GREEN, node["detail"]
    assert node["metrics"]["discards_since_observation"] == 0
    assert node["metrics"]["queue_depth"] == 3, "队列深度只能取自 [WORKER] 记录，不能被 dump 里的 999 污染"


def test_real_parser_discard_record_still_counts():
    """收紧锚点后，真实 PARSER 丢弃记录必须仍被计入（防矫枉过正）。"""
    now = time.time()
    node = _run_lines(_discards(6, now - 30, "API Error: 400 boom"),
                      observation_epoch_s=now - 3600)
    assert node["state"] == P.RED
    assert node["metrics"]["discards_since_observation"] == 6


def test_continuation_line_does_not_stop_backtracking():
    """续行既不计数也不能当回溯终点，否则一段 dump 会挡住其后的真实旧记录。"""
    now = time.time()
    stamp_new = datetime.fromtimestamp(now - 30).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    lines = _discards(7, now - 30, "API Error: 400 boom")
    lines.append("    dump 续行 outputClass=prose 无时间戳\n")
    lines.append(f"[{stamp_new}] [INFO ] [WORKER] status {{queueDepth=1}}\n")
    node = _run_lines(lines, observation_epoch_s=now - 3600)
    assert node["metrics"]["discards_since_observation"] == 7


def test_wake_blip_first_attempt_fails_then_recovers_is_green():
    """2026-09-07 10:58:20 真实误报：探针撞上 DarkWake→FullWake 的 5 秒窗口。
    单次 URLError 不构成"不可达"；重试一次成功即绿。"""
    now = time.time()
    calls = {"n": 0}
    def flaky(url, *a, **k):
        calls["n"] += 1
        return (None, "URLError") if calls["n"] == 1 else (_health(), None)
    old_once, old_sleep = P._http_json_once, P.time.sleep
    P._http_json_once = flaky; P.time.sleep = lambda s: None
    try:
        node = _run_lines([], observation_epoch_s=now - 60)
    finally:
        P._http_json_once, P.time.sleep = old_once, old_sleep
    # _run_lines 会覆盖 P.http_json 为恒成功；这里要验的是真实 http_json 的重试，单独再跑一遍
    calls["n"] = 0
    P._http_json_once = flaky; P.time.sleep = lambda s: None
    try:
        data, err = P.http_json("http://x", timeout=1, retries=2)
    finally:
        P._http_json_once, P.time.sleep = old_once, old_sleep
    assert err is None and data["status"] == "ok"
    assert calls["n"] == 2, f"应在第 2 次成功后停止，实际调用 {calls['n']} 次"


def test_hard_dead_worker_still_red_after_retries():
    calls = {"n": 0}
    def dead(url, *a, **k):
        calls["n"] += 1
        return (None, "URLError")
    old_once, old_sleep = P._http_json_once, P.time.sleep
    P._http_json_once = dead; P.time.sleep = lambda s: None
    try:
        data, err = P.http_json("http://x", timeout=1, retries=2)
    finally:
        P._http_json_once, P.time.sleep = old_once, old_sleep
    assert data is None and err == "URLError"
    assert calls["n"] == 3, "retries=2 应共尝试 3 次"


def test_default_retries_zero_keeps_single_attempt():
    """默认不重试：其它调用方行为不变。"""
    calls = {"n": 0}
    def dead(url, *a, **k):
        calls["n"] += 1
        return (None, "URLError")
    old_once = P._http_json_once
    P._http_json_once = dead
    try:
        P.http_json("http://x", timeout=1)
    finally:
        P._http_json_once = old_once
    assert calls["n"] == 1


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
