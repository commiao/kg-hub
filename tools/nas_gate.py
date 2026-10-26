"""NAS 重操作闸门：dry-run 与正式提交都要在 NAS 上做实体匹配、算向量、写 FalkorDB，会让 CPU 温度短时上升 5–16°C
（2026-10-24 实测）。战役 4 个子代理并行时，这类操作必须排队、留间隔、看温度。

本机所有执行者经这里跑这两类操作（`extract_campaign submit` 与 `agent_extract submit` 已内置，不用另外包）：
  1. 全机一把锁：同一时刻只有一个重操作在 NAS 上跑（fcntl 文件锁，同一台机器内有效）；
  2. 间隔：距上一次重操作结束至少 gap_s 秒（缺省 dry-run 60、submit 180），让温度回落；
  3. 测温：连读 samples 次（缺省 3）NAS CPU 温度取均值，≤ max_c（缺省 90°C）才放行，否则每 recheck_s 秒复测；
     连续 max_misses 次读不到温度退出码 75（EX_TEMPFAIL，不盲目放行）；FIFO 排队期间只有前方真实 HTTP
     响应或队列前移才刷新 max_wait_s 无进展预算。到队首后等锁、间隔和降温仍共用原 max_wait_s，超时退出76。

配置：config/nas_gate.json（KG_HUB_NAS_GATE_CONFIG 换整份）；KG_HUB_NAS_TEMP_SSH / KG_HUB_NAS_TEMP_SENSOR /
KG_HUB_NAS_GATE_DIR 覆盖 SSH 目标、传感器路径、锁目录。锁目录缺省与仓库外的 ~/.kg-hub/campaign-tools/nas_gate.py
是同一把锁、同一个状态文件，过渡期两者混用也互斥。

命令行（包任意命令；输出原样透传，退出码就是命令的退出码，闸门自己的进度写到 stderr）：
  python -m tools.nas_gate --kind dryrun -- <命令…>
  python -m tools.nas_gate --probe          # 只读一次温度均值与上次重操作时间，不拿锁
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "nas_gate.json"
KINDS = ("dryrun", "submit")
EXIT_NO_TEMP = 75
EXIT_TIMEOUT = 76
LOCK_POLL_S = 2
_ENV = {"ssh_target": "KG_HUB_NAS_TEMP_SSH", "sensor_path": "KG_HUB_NAS_TEMP_SENSOR", "lock_dir": "KG_HUB_NAS_GATE_DIR"}


class GateRefused(SystemExit):
    """闸门不放行：code 是退出码（75 读不到温度 / 76 等待超时），message 给人看。"""

    def __init__(self, code: int, message: str):
        super().__init__(code)
        self.message = message


def load_settings(env: dict | None = None, path: Path | None = None) -> dict:
    env = os.environ if env is None else env
    p = Path(path or env.get("KG_HUB_NAS_GATE_CONFIG") or CONFIG_PATH).expanduser()
    s = {k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")}
    for key, var in _ENV.items():
        if env.get(var):
            s[key] = env[var]
    for key in ("ssh_target", "sensor_path", "lock_dir"):
        if not s.get(key):
            raise SystemExit(f"NAS 闸门配置缺 {key}（{p}，或环境变量 {_ENV[key]}）")
    return s


def _log(msg: str) -> None:
    print(f"[nas_gate {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def read_temp(settings: dict, runner=subprocess.run) -> float | None:
    """经 ssh 读一次 NAS 温度（°C）；读不到返回 None。"""
    try:
        out = runner(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", settings["ssh_target"],
                      "cat", settings["sensor_path"]], capture_output=True, text=True, timeout=25)
        return int(out.stdout.strip()) / float(settings.get("sensor_divisor") or 1) if out.returncode == 0 else None
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


def avg_temp(settings: dict, read, sleep) -> float | None:
    """连读 samples 次取均值；有一次读不到就算这一轮读不到（不拿残缺样本放行）。"""
    n = int(settings.get("samples") or 3)
    vals = []
    for i in range(n):
        t = read(settings)
        if t is not None:
            vals.append(t)
        if i < n - 1:
            sleep(settings.get("sample_interval_s", 2))
    return sum(vals) / n if len(vals) == n else None


def state_path(settings: dict) -> Path:
    return Path(settings["lock_dir"]).expanduser() / "nas-heavy.state.json"


def last_end(settings: dict) -> float:
    try:
        return float(json.loads(state_path(settings).read_text()).get("last_end", 0))
    except (OSError, ValueError, AttributeError):
        return 0.0


def track_responses(post, gate_info):
    """Record real queue progress without replacing a successful HTTP result."""
    def send(*args, **kwargs):
        result = post(*args, **kwargs)
        progress = (gate_info or {}).get('response_progress')
        if progress:
            try:
                progress()
            except (OSError, ValueError):
                _log('FIFO 响应进度记录失败；保留真实 HTTP 结果，等待者仍受无进展预算限制')
        return result
    return send


@contextlib.contextmanager
def hold(kind: str, *, settings: dict | None = None, read=None, sleep=time.sleep, clock=time.time, log=_log):
    """拿到锁、等够间隔、温度达标后进入；退出时记下这次重操作的结束时间（拿锁期间才写，进不来不写）。"""
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {KINDS}")
    s = settings or load_settings()
    read = read or read_temp
    gap = float((s.get("gap_s") or {}).get(kind, 0))
    max_c, recheck = float(s.get("max_c", 90)), float(s.get("recheck_s", 120))
    budget = float(s.get("max_wait_s", 3600))
    lock_dir = Path(s["lock_dir"]).expanduser()
    lock_dir.mkdir(parents=True, exist_ok=True)
    from tools.nas_queue import reserve
    with reserve(lock_dir, kind, clock) as ticket, open(lock_dir / "nas-heavy.lock", "a") as lk:
        # The queue may legitimately contain hours of successful work. A waiter
        # times out only when the queue makes no progress for the same original
        # budget; the head still has that full budget for lock/gap/temperature.
        last_progress, signature, previous_position = clock(), None, None
        while True:
            position, head = ticket.position()
            if position == 0:
                break
            current = (position, head)
            if current != signature:
                last_progress, signature = clock(), current
            if position != previous_position:
                log(f"FIFO 排队：前方 {position} 个 NAS 重操作")
                previous_position = position
            left = last_progress + budget - clock()
            if left <= 0:
                raise GateRefused(EXIT_TIMEOUT, f"NAS FIFO 队列超过 {budget:g} 秒无进展，未放行 {kind}")
            sleep(min(LOCK_POLL_S, left))
        deadline = clock() + budget
        queued = False
        while True:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not queued:
                    log("另一个 NAS 重操作正在进行，排队等锁")
                    queued = True
                left = deadline - clock()
                if left <= 0:
                    raise GateRefused(EXIT_TIMEOUT, f"排队等锁超过 {s.get('max_wait_s')} 秒，未放行 {kind}")
                sleep(min(LOCK_POLL_S, left))
        wait = last_end(s) + gap - clock()
        if wait > 0:
            if clock() + wait > deadline:
                raise GateRefused(EXIT_TIMEOUT, "NAS 安全间隔超过当前等待预算，未放行")
            log(f"距上一次重操作不足 {gap:.0f} 秒，再等 {wait:.0f} 秒")
            sleep(wait)
        misses = 0
        while True:
            if clock() >= deadline:
                raise GateRefused(EXIT_TIMEOUT, "NAS 测温等待超过预算，未放行")
            t = avg_temp(s, read, sleep)
            if clock() > deadline:
                raise GateRefused(EXIT_TIMEOUT, "NAS 测温完成时已超过预算，未放行")
            if t is None:
                misses += 1
                log(f"读不到 NAS 温度（第 {misses} 次）")
                if misses >= int(s.get("max_misses", 5)):
                    raise GateRefused(EXIT_NO_TEMP, f"连续 {misses} 次读不到 NAS 温度（{s['ssh_target']}），不放行 {kind}")
            elif t <= max_c:
                log(f"NAS {t:.1f}°C ≤ {max_c:.0f}°C，放行 {kind}")
                break
            else:
                misses = 0
                log(f"NAS {t:.1f}°C > {max_c:.0f}°C，{recheck:.0f} 秒后复测")
            if clock() + recheck > deadline:
                raise GateRefused(EXIT_TIMEOUT, f"等 NAS 降温超过 {s.get('max_wait_s')} 秒（最后一次 "
                                                f"{'读不到' if t is None else f'{t:.1f}°C'}），未放行 {kind}")
            sleep(recheck)
        try:
            yield {"kind": kind, "temp_c": round(t, 1), "response_progress": ticket.response}
        finally:
            state_path(s).write_text(json.dumps({"last_end": clock(), "last_kind": kind}))
            log(f"{kind} 结束")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=KINDS)
    ap.add_argument("--gap", type=float, help="距上一次重操作结束的最短秒数（覆盖配置）")
    ap.add_argument("--max-c", type=float, help="放行温度上限（覆盖配置）")
    ap.add_argument("--probe", action="store_true", help="只读温度均值与上次重操作时间，不拿锁")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    s = load_settings()
    if a.max_c is not None:
        s["max_c"] = a.max_c
    if a.probe:
        t = avg_temp(s, read_temp, time.sleep)
        end = last_end(s)
        print(json.dumps({"temp_c": None if t is None else round(t, 1), "max_c": s["max_c"],
                          "last_end": end or None, "since_last_end_s": round(time.time() - end) if end else None},
                         ensure_ascii=False))
        return 0 if t is not None else EXIT_NO_TEMP
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not (a.kind and cmd):
        ap.error("需要 --kind 和要执行的命令（写在 -- 之后），或用 --probe")
    if a.gap is not None:
        s["gap_s"] = {**(s.get("gap_s") or {}), a.kind: a.gap}
    try:
        with hold(a.kind, settings=s):
            return subprocess.run(cmd).returncode
    except GateRefused as e:
        _log(e.message)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
