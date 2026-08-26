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
