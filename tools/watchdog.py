"""
kg-hub watchdog — active monitoring (Phase 3.A.6).

Runs via launchd every ~10 min (see com.kg-hub.watchdog.plist). Polls the
server and emits EDGE-TRIGGERED alerts (only on state transitions, not while
the bad state persists). Output channels in priority order:

  1. Feishu webhook   — if KG_HUB_FEISHU_WEBHOOK env set
  2. macOS notification — fallback when no webhook
  3. Always: append to ~/.kg-hub/logs/alerts.log

State file at ~/.kg-hub/state/watchdog.json tracks the previous run's
anomaly flags so we know when transitions happen.

Anomalies tracked:
  server_down       /health not reachable
  queue_backlog     pending > BACKLOG_THRESHOLD
  stuck_jobs        oldest_pending_age > STUCK_THRESHOLD min
  recent_errors     errored_last_1h > 0（新增错误；持续故障时会归零，见下）
  extraction_failing  errored_total > 阈值（存量卡住，持续故障的诚实信号）

For each: emit one alert on OK→BAD, one on BAD→OK. No alert while BAD persists.

Exit codes:
  0  ran successfully (whether or not anything was alerted)
  1  fatal: state file unreadable / unwritable, etc.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import httpx

from utils.device_liveness import (DEFAULT_CAPTURE_STALE_AFTER_S,
                                   DEFAULT_MAX_AGE_S, DEFAULT_PATH, device_state,
                                   load_config, load_status, normalize_host,
                                   positive_int)

from dotenv import load_dotenv
load_dotenv(Path.home() / ".claude-mem" / ".env", override=True)


KG_HUB_URL = os.environ.get("KG_HUB_URL", "http://127.0.0.1:8080")
KG_HUB_TOKEN = os.environ.get("KG_HUB_API_TOKEN", "")
FEISHU_WEBHOOK = os.environ.get("KG_HUB_FEISHU_WEBHOOK", "").strip()

STATE_DIR = Path.home() / ".kg-hub" / "state"
STATE_FILE = STATE_DIR / "watchdog.json"
ALERTS_LOG = Path.home() / ".kg-hub" / "logs" / "alerts.log"

BACKLOG_THRESHOLD = int(os.environ.get("KG_HUB_BACKLOG_THRESHOLD", "5"))
STUCK_SECONDS = int(os.environ.get("KG_HUB_STUCK_THRESHOLD_MIN", "30")) * 60
# A timed /api/search probe exercises the FalkorDB query path. If it takes longer
# than this, FalkorDB is likely overloaded (the runaway-query / pegged-CPU failure
# that started this whole effort). Tune via KG_HUB_FALKORDB_SLOW_SEC.
SLOW_SECONDS = float(os.environ.get("KG_HUB_FALKORDB_SLOW_SEC", "8"))

# Hot-reloadable notification config: edit this JSON file (on a mounted volume) to
# change webhook / thresholds / enable WITHOUT rebuilding the container — it is
# re-read every watchdog cycle. Keys (all optional):
#   {"enabled": true, "feishu_webhook": "...", "backlog_threshold": 20,
#    "stuck_threshold_min": 30, "falkordb_slow_sec": 8}
NOTIFY_CONFIG_PATH = Path(
    os.environ.get("KG_HUB_NOTIFY_CONFIG", "/config/notify.json")
)


def load_notify_config() -> dict:
    try:
        return json.loads(NOTIFY_CONFIG_PATH.read_text())
    except Exception:
        return {}


def _server_hostport() -> tuple[str, int]:
    from urllib.parse import urlparse
    u = urlparse(KG_HUB_URL)
    return (u.hostname or "127.0.0.1"), (u.port or 8080)


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "anomalies": {
                "server_down": False,
                "queue_backlog": False,
                "stuck_jobs": False,
                "recent_errors": False,
                "falkordb_unreachable": False,
                "falkordb_slow": False,
            },
            "last_run": None,
        }
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"anomalies": {}, "last_run": None}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def write_alert_log(line: str) -> None:
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS_LOG.open("a") as f:
        f.write(f"{now_iso()} {line}\n")


def send_feishu(text: str) -> bool:
    """Post to Feishu group bot webhook. Returns True if sent."""
    if not FEISHU_WEBHOOK:
        return False
    try:
        r = httpx.post(
            FEISHU_WEBHOOK,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=10.0,
        )
        return r.status_code < 400
    except Exception:
        return False


def send_macos_notification(title: str, message: str) -> bool:
    """Fire a macOS Notification Center alert via osascript."""
    try:
        # escape double quotes / backslashes for AppleScript
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"').replace("\n", " ")
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe_msg}" with title "{safe_title}"',
            ],
            check=False,
            timeout=5,
        )
        return True
    except Exception:
        return False


def emit_alert(severity: str, kind: str, message: str) -> None:
    """severity: 'fire' (BAD-state-entered) or 'clear' (BAD-state-resolved)."""
    emoji = "🔴" if severity == "fire" else "✅"
    title = f"{emoji} kg-hub {kind}"
    line = f"[{severity.upper()}] {kind}: {message}"
    write_alert_log(line)
    body = f"{title}\n{message}"
    sent_via = "log"
    if FEISHU_WEBHOOK and send_feishu(body):
        sent_via = "feishu"
    elif send_macos_notification(title, message):
        sent_via = "macos"
    print(f"{title} | {message} (via {sent_via})")


DISKTEMP_DIR = Path(os.environ.get("KG_HUB_DISKTEMP_DIR", "/disktemp"))
DISK_TEMP_WARN = int(os.environ.get("KG_HUB_DISK_TEMP_WARN", "59"))


def check_disk_temp() -> tuple[int | None, str]:
    """群晖盘温预警(2026-08 过热事件后加)。DSM 到 61°C 直接强制关机、且**没有任何
    通知**——8/16 就这样停了 24 小时无人知。这里在触线前一步喊人,让"开机"这个
    唯一的人工动作能及时发生。读 /run/synostorage/disks/*/temperature(免 sudo)。"""
    temps: dict[str, int] = {}
    try:
        for f in DISKTEMP_DIR.glob("*/temperature"):
            try:
                temps[f.parent.name] = int(f.read_text().strip())
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return None, "disk temp unreadable"
    if not temps:
        return None, "no disk temp source"
    hottest = max(temps.values())
    detail = " ".join(f"{k}={v}°C" for k, v in sorted(temps.items()))
    return hottest, f"{detail}(阈值 {DISK_TEMP_WARN}°C,DSM 61°C 强制关机)"


def check_health() -> tuple[bool, str]:
    """Returns (alive, message)."""
    try:
        r = httpx.get(f"{KG_HUB_URL}/health", timeout=5.0)
        if r.status_code == 200:
            return True, "healthy"
        return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def check_queue() -> tuple[dict | None, str]:
    """Returns (stats_dict, message)."""
    if not KG_HUB_TOKEN:
        return None, "KG_HUB_API_TOKEN not set in env"
    try:
        r = httpx.get(
            f"{KG_HUB_URL}/api/queue_stats",
            headers={"Authorization": f"Bearer {KG_HUB_TOKEN}"},
            timeout=10.0,
        )
        if r.status_code == 200:
            return r.json(), "ok"
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def check_search_probe() -> tuple[str, float, str]:
    """Timed probe of /api/search — exercises the FalkorDB query path.

    Returns (status, latency_sec, message); status in {ok, slow, error, skip}.
    Detects FalkorDB unreachable (error) or CPU-pegged / runaway queries (slow).
    """
    if not KG_HUB_TOKEN:
        return "skip", 0.0, "no token"
    try:
        t0 = time.monotonic()
        r = httpx.get(
            f"{KG_HUB_URL}/api/search",
            params={"q": "__watchdog_probe__", "num_results": 1},
            headers={"Authorization": f"Bearer {KG_HUB_TOKEN}"},
            timeout=20.0,
        )
        dt = time.monotonic() - t0
        if r.status_code != 200:
            return "error", dt, f"HTTP {r.status_code}: {r.text[:150]}"
        if dt > SLOW_SECONDS:
            return "slow", dt, f"search took {dt:.1f}s (> {SLOW_SECONDS:.0f}s) — FalkorDB may be overloaded"
        return "ok", dt, f"{dt:.2f}s"
    except Exception as exc:
        return "error", 0.0, f"{type(exc).__name__}: {exc}"


class CaptureDecision(NamedTuple):
    """每个告警维度独立三态：list=已知结果，None=本轮不可判，沿用上一轮。"""

    blocked: list[str] | None
    stale: list[str] | None


def check_capture_chain(_notify_cfg: dict) -> CaptureDecision:
    """采集链路健康：读 kg-hub 的拓扑快照，判断"该喊人"的两件事。

    返回 CaptureDecision。字段为空 list 表示明确健康；None 表示本轮未知，调用方
    必须沿用上一轮状态，不能把“读不到”解释成 resolved。

    ## 为什么只对 red 和"探针失联"告警，黄灯一概不报

    黄灯（Cursor 三天没动、Mac→NAS 落后几十条）是**被观测的常态**，不是故障。
    对黄灯告警等于每天几十条噪音，一周内人就把这个群静音了 —— 那时真的红灯
    来了也没人看。这与探针 --exit-zero 是同一个判断。

    ## 探针失联必须有独立 online 证据

    旧快照本身无法区分“Mac 睡眠”与“探针进程死亡”。只有 NAS host 的新鲜
    Tailscale 快照明确显示设备 online，旧采集快照才是一条独立告警；offline
    明确不报，unknown（包括 Tailscale 快照自身过期）则沿用上一轮状态。
    """
    if not KG_HUB_URL:
        return CaptureDecision(None, None)
    try:
        r = httpx.get(f"{KG_HUB_URL.rstrip('/')}/api/topology/latest",
                      headers={"Authorization": f"Bearer {KG_HUB_TOKEN}"} if KG_HUB_TOKEN else {},
                      timeout=10.0)
        if r.status_code >= 400:
            print(f"[watchdog] topology API HTTP {r.status_code}; "
                  "not classifying as capture_probe_stale")
            return CaptureDecision(None, None)
        payload = r.json() or {}
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            print("[watchdog] topology API returned non-ok payload; holding capture state")
            return CaptureDecision(None, None)
        snaps = payload.get("snapshots")
        if not isinstance(snaps, list):
            print("[watchdog] topology API snapshots is not a list; holding capture state")
            return CaptureDecision(None, None)
    except Exception as exc:  # noqa: BLE001
        print(f"[watchdog] topology API unreadable ({type(exc).__name__}: {exc}); "
              "not classifying as capture_probe_stale")
        return CaptureDecision(None, None)

    # 容器不假设装有 tailscale CLI。NAS host 每分钟把 `tailscale status --json`
    # 原子写进 /device-liveness；watchdog 与 dashboard 只读同一份独立设备在线信号。
    # capture host/identity/threshold 只读公开 device-liveness.json。notify.json
    # 只控制通知开关/渠道，不能覆盖阈值，否则 dashboard 与 watchdog 会分裂。
    device_cfg = load_config()
    liveness_path = device_cfg.get("device_liveness_path") or DEFAULT_PATH
    try:
        liveness_max_age_s = int(
            device_cfg.get("device_liveness_max_age_sec", DEFAULT_MAX_AGE_S))
    except (TypeError, ValueError):
        liveness_max_age_s = DEFAULT_MAX_AGE_S
    liveness = load_status(liveness_path, max_age_s=liveness_max_age_s)
    return judge_snapshots(snaps, device_cfg, liveness)


def _capture_probe_hosts(cfg: dict) -> list[str]:
    """允许静态声明“监控哪些 host”，但不允许静态声明其在线状态。"""
    raw = cfg.get("capture_probe_hosts") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list):
        return []
    return [str(host).strip() for host in raw if str(host).strip()]


def judge_snapshots(snaps: list[dict], cfg: dict,
                    liveness: dict | None = None) -> CaptureDecision:
    """纯判定：快照 → 每个异常维度各自的 known-bad/known-good/unknown。

    快照旧只说明“看不见了”，不能区分 Mac 睡眠与探针死亡。只有 NAS 独立采样
    新鲜且明确认为设备 online，才把旧快照判成探针失联。offline/unknown 静默；
    新鲜快照里的 red blocker 始终告警；amber（工具空闲）始终不告警。
    """
    aliases = cfg.get("capture_device_aliases")
    stale_after = positive_int(
        cfg.get("capture_stale_after_min"),
        DEFAULT_CAPTURE_STALE_AFTER_S // 60) * 60
    blocked, stale = [], []
    blocked_unknown = stale_unknown = False

    # 必须看“配置清单 ∪ 已有快照”：否则只要任意 host 仍在上报，另一个从未上报
    # 的 configured-online host 就会从判定里消失。
    snapshots_by_host: dict[str, dict] = {}
    host_labels: dict[str, str] = {}
    for sn in snaps:
        if not isinstance(sn, dict):
            blocked_unknown = True
            stale_unknown = True
            continue
        host = str(sn.get("_host") or "?")
        key = normalize_host(host)
        # topology API 按 received_at newest-first；规范化后重复历史 host 只保留首条。
        snapshots_by_host.setdefault(key, sn)
        host_labels.setdefault(key, host)
    for host in _capture_probe_hosts(cfg):
        key = normalize_host(host)
        host_labels.setdefault(key, host)

    if not host_labels:
        blocked_unknown = stale_unknown = True

    for key, host in host_labels.items():
        sn = snapshots_by_host.get(key)
        if sn is None:
            state = device_state(liveness, host, aliases)[0]
            if state == "online":
                stale.append(f"{host} 设备在线，但从未收到采集链路快照")
            elif state == "unknown":
                blocked_unknown = True
                stale_unknown = True
            # offline/online 都明确说明旧 blocker 不适用；仅 unknown 才 HOLD。
            continue

        age = sn.get("_age_s")
        snapshot_stale = (age > stale_after
                          if isinstance(age, (int, float)) else None)
        if snapshot_stale is None:
            blocked_unknown = True
            stale_unknown = True
            continue
        if snapshot_stale:
            if liveness is None:
                state = str(sn.get("_device_state") or "unknown")
            else:
                state = device_state(liveness, host, aliases)[0]
            if state == "online":
                stale.append(
                    f"{host} 设备在线，但探针 {int(age or 0) // 60} 分钟未上报")
            elif state == "unknown":
                blocked_unknown = True
                stale_unknown = True
            # known online/offline 时旧 blocker 都不再可信且应清除；只有 unknown
            # host 才 HOLD。fresh blocker 会在其它 host 分支重新把聚合态判为 BAD。
            continue
        for b in (sn.get("blockers") or []):
            blocked.append(f"{host} · {b.get('label', '?')}: {b.get('detail', '')}")

    # 任一明确坏态足以 FIRE；没有坏态时，只要还有旧/不可判 host 就必须 HOLD。
    blocked_decision = blocked if blocked else (None if blocked_unknown else [])
    stale_decision = stale if stale else (None if stale_unknown else [])
    return CaptureDecision(blocked_decision, stale_decision)


def apply_capture_decision(decision: CaptureDecision, prev_anomalies: dict,
                           new_anomalies: dict, details: dict) -> None:
    """把三态判定并入 edge-trigger 状态，unknown 明确沿用 prev。"""
    if decision.blocked is None:
        new_anomalies["capture_blocked"] = bool(
            prev_anomalies.get("capture_blocked", False))
        if new_anomalies["capture_blocked"]:
            details["capture_blocked"] = "采集链路状态本轮不可判，沿用上一轮阻塞状态"
    else:
        new_anomalies["capture_blocked"] = bool(decision.blocked)
        if decision.blocked:
            details["capture_blocked"] = (
                "采集链路有阻塞点:\n  " + "\n  ".join(decision.blocked[:6])
                + "\n看 /dashboard/topology")

    if decision.stale is None:
        new_anomalies["capture_probe_stale"] = bool(
            prev_anomalies.get("capture_probe_stale", False))
        if new_anomalies["capture_probe_stale"]:
            details["capture_probe_stale"] = (
                "设备在线/探针状态本轮不可判，沿用上一轮失联状态")
    else:
        new_anomalies["capture_probe_stale"] = bool(decision.stale)
        if decision.stale:
            details["capture_probe_stale"] = (
                "采集探针没在上报(设备已知在线，面板显示旧数据):\n  "
                + "\n  ".join(decision.stale[:6])
                + "\n查该机 launchctl print gui/$UID/com.kg-hub.capture-probe"
                  " 与 ~/.kg-hub/logs/capture-probe.err.log")


def main() -> int:
    # Hot-read notification config (file on a mounted volume) — overrides env-derived
    # defaults each cycle, so rules change without a rebuild/recreate.
    global FEISHU_WEBHOOK, BACKLOG_THRESHOLD, STUCK_SECONDS, SLOW_SECONDS
    cfg = load_notify_config()
    if cfg.get("enabled") is False:
        print("[watchdog] disabled via notify config")
        return 0
    if cfg.get("feishu_webhook"):
        FEISHU_WEBHOOK = str(cfg["feishu_webhook"]).strip()
    if "backlog_threshold" in cfg:
        BACKLOG_THRESHOLD = int(cfg["backlog_threshold"])
    if "stuck_threshold_min" in cfg:
        STUCK_SECONDS = int(cfg["stuck_threshold_min"]) * 60
    if "falkordb_slow_sec" in cfg:
        SLOW_SECONDS = float(cfg["falkordb_slow_sec"])

    state = load_state()
    prev_anomalies = state.get("anomalies", {})

    # Boot-race mitigation: on first-ever run (no last_run yet), give the
    # kg-hub server a 60s grace period to start before declaring it down.
    # This prevents spurious server_down → server_up "flicker" alerts at boot.
    if state.get("last_run") is None:
        import socket
        probe_host, probe_port = _server_hostport()
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((probe_host, probe_port), timeout=2.0):
                    break
            except OSError:
                time.sleep(2.0)
    new_anomalies = {
        "server_down": False,
        "queue_backlog": False,
        "stuck_jobs": False,
        "capsule_stale": False,
        "recent_errors": False,
        "falkordb_unreachable": False,
        "falkordb_slow": False,
        "disk_temp_high": False,
        "capture_blocked": False,
        "capture_probe_stale": False,
        "extraction_failing": False,
    }
    details: dict[str, str] = {}

    # 0. 盘温(优先级最高:硬件保护,且与 server 存活无关)
    hottest, temp_msg = check_disk_temp()
    if hottest is not None and hottest >= DISK_TEMP_WARN:
        new_anomalies["disk_temp_high"] = True
        details["disk_temp_high"] = f"⚠️ 硬盘温度 {hottest}°C 逼近强制关机线 · {temp_msg}"

    # 1. health probe
    alive, health_msg = check_health()
    if not alive:
        new_anomalies["server_down"] = True
        details["server_down"] = health_msg

    # 2. queue stats (only meaningful if server alive)
    stats = None
    if alive:
        stats, qmsg = check_queue()
        if stats:
            pending = int(stats.get("pending", 0))
            oldest_age = stats.get("oldest_pending_age_seconds")
            errored_1h = int(stats.get("errored_last_1h", 0))
            if pending > BACKLOG_THRESHOLD:
                new_anomalies["queue_backlog"] = True
                details["queue_backlog"] = f"pending={pending} > threshold {BACKLOG_THRESHOLD}"
            # ── OpenClaw 直推链路:只报「卡住」,不报「没产出」 ─────────────────
            # 2026-08-24 校准:原判据是纯账龄(阈值 76h)。但实测 kb-001 的产出
            # 间隔是 2 天 / 16 天 / 9 天(7-24、7-26、8-11、8-20)——**阈值落在
            # 正常范围里面**,于是"OpenClaw 这几天没写新胶囊"这件正常事天天报警。
            # 而"没写"是内容依赖的,不是故障;kb-001 每天 04:00 都跑且返回 ok。
            #
            # 三个信号分开(混在一起就没法行动):
            #   装置存活 → 探针 sync:openclaw 读 VPS push.log 心跳(静默6h判红)
            #   内容产出 → 不告警(不规律是常态)
            #   管线卡住 → 本项:产出了但进不去图 = 隔离区积压 / 错误键 ← 可行动
            #
            # 故账龄只作**必要条件**,还须有真卡住的证据才报。
            stale_h = float(cfg.get("capsule_stale_hours", 30))
            cap_age = stats.get("openclaw_capsule_age_hours")
            if stale_h > 0:
                if isinstance(cap_age, (int, float)):
                    nq = int(stats.get("quarantined_capsules") or 0)
                    if cap_age > stale_h and nq > 0:
                        new_anomalies["capsule_stale"] = True
                        details["capsule_stale"] = (
                            f"OpenClaw 胶囊卡在门口:隔离区积压 {nq} 条,最新入图已 {cap_age}h。"
                            f"→ /dashboard/inbox ③格式异常(补来源标或丢弃)"
                        )
                else:
                    # 取数暂态失败(字段缺失/null):沿用上一轮判定,避免 30h+ 长异常
                    # 被一次抖动打成 resolved→again 的成对噪音
                    new_anomalies["capsule_stale"] = bool(prev_anomalies.get("capsule_stale"))
                    if new_anomalies["capsule_stale"]:
                        details["capsule_stale"] = "胶囊账龄暂不可读(暂态),沿用上一轮 stale 判定"
            if isinstance(oldest_age, (int, float)) and oldest_age > STUCK_SECONDS:
                new_anomalies["stuck_jobs"] = True
                details["stuck_jobs"] = (
                    f"oldest pending {int(oldest_age)}s old "
                    f"(threshold {STUCK_SECONDS}s)"
                )
            # ── 抽取持续失败(2026-08-25 补的盲区)────────────────────────
            # recent_errors 判据是 errored_last_1h > 0 —— 它测的是「**新增**错误」,
            # 而故障持续时新增会归零:LLM 供应商 key 到期后,重试全被 409 挡住、
            # 不再产生新 error 键 → errored_1h = 0 → **告警在故障持续期间自己
            # CLEAR 了**。实测 8/24 16:01 FIRE / 17:35 CLEAR、23:01 FIRE /
            # 00:00 CLEAR,而故障一直持续到次日 11:00 人工发现,零产出 9 小时。
            #
            # 故这里改测**存量**:error 键总数 > 阈值 = 有一批数据卡着进不了图,
            # 无论它们是刚失败的还是失败后一直没能重试。存量只有被清理(24h 过期
            # 或人工)或成功入图才会降 —— 这才是"故障是否结束"的诚实信号。
            err_total = int(stats.get("errored_total", 0))
            # 阈值 5:测试复盘发现 20 太高——事故初期(16:01)只有 7 条 error 键时
            # 抽取其实已全挂,却要等堆到 20+ 才报,白等几小时。5 条足以区分
            # "零星失败"(writer.lock 抢锁等,历史上 1-2 条)与"供应商挂了"。
            err_gate = int(cfg.get("extraction_error_threshold", 5))
            if err_total > err_gate:
                new_anomalies["extraction_failing"] = True
                samples = stats.get("recent_error_samples") or []
                why = (samples[0].get("error", "")[:120] if samples else "")
                details["extraction_failing"] = (
                    f"⚠️ {err_total} 条 obs 卡在抽取失败(阈值 {err_gate}),数据进不了图。"
                    + (f"样本原因:{why}" if why else "")
                    + " → 多为 LLM 供应商失效(key 过期/配额/限流),查 kg_hub_server 日志"
                )
            if errored_1h > 0:
                new_anomalies["recent_errors"] = True
                samples = stats.get("recent_error_samples") or []
                sample_txt = "; ".join(
                    f"{s.get('sid', '?')}: {s.get('error', '')}" for s in samples[:3]
                )
                details["recent_errors"] = (
                    f"{errored_1h} errored in last hour"
                    + (f" — {sample_txt}" if sample_txt else "")
                )

    # 2b. FalkorDB probe — timed /api/search (only if server alive)
    if alive:
        pstatus, plat, pmsg = check_search_probe()
        if pstatus == "error":
            new_anomalies["falkordb_unreachable"] = True
            details["falkordb_unreachable"] = pmsg
        elif pstatus == "slow":
            new_anomalies["falkordb_slow"] = True
            details["falkordb_slow"] = pmsg

    # 2c. 采集链路（各设备/工具 → claude-mem → SQLite → NAS → kg-hub）
    if cfg.get("capture_chain_enabled", True):
        apply_capture_decision(
            check_capture_chain(cfg) if alive else CaptureDecision(None, None),
            prev_anomalies, new_anomalies, details)

    # 3. edge-triggered alerts (only on state transitions)
    for kind, is_bad_now in new_anomalies.items():
        was_bad = bool(prev_anomalies.get(kind, False))
        if is_bad_now and not was_bad:
            emit_alert("fire", kind, details.get(kind, "anomaly detected"))
        elif was_bad and not is_bad_now:
            emit_alert("clear", kind, "resolved")

    # 4. persist state
    save_state({
        "anomalies": new_anomalies,
        "last_run": now_iso(),
        "last_stats": stats,
    })

    # short summary on stdout (visible in plist log)
    any_bad = any(new_anomalies.values())
    if any_bad:
        bad = ",".join(k for k, v in new_anomalies.items() if v)
        print(f"[watchdog] state=BAD [{bad}]")
    else:
        print("[watchdog] state=OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
