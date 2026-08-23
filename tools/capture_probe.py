#!/usr/bin/env python3
"""
capture_probe — claude-mem 采集链路的 Mac 侧探针（脱离 hooks，独立运行）。

为什么存在：2026-07-13 codex 采集静默停摆 5 周无人察觉（T-0021）；
2026-08-21 又发现 sync 脚本因 WAL 判据失效导致 kg-hub 数据冻结 11.5 小时
（T-0028）。两次都是"配置在、数据断、日志看起来正常"的静默失效。

这个探针把整条链路的**每一跳**都量化成节点/边状态，让阻塞点一眼可见：

    工具 → hook → claude-mem worker → SQLite(+WAL) → sync→NAS → ingester → FalkorDB

设计原则：
- **不依赖 hooks**：hook 本身可能就是坏的那一环，探针必须独立
- **只读**：不改任何配置、不重启任何服务
- **纯标准库**：urllib / sqlite3 / subprocess，不引新依赖
- **判据用 MAX 不用 COUNT**：见 docs/INVESTIGATION-RULES.md 规则 1
- **不打印密钥**：token 只用于请求头，从不出现在输出里

用法：
    python3 tools/capture_probe.py                 # 输出 JSON 到 stdout
    python3 tools/capture_probe.py --report        # 采集并 POST 到 kg-hub
    python3 tools/capture_probe.py --pretty        # 人眼可读的表格
    python3 tools/capture_probe.py --out FILE      # 写文件
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CM_DB = HOME / ".claude-mem" / "claude-mem.db"
CM_ENV = HOME / ".claude-mem" / ".env"
CM_HEALTH = "http://localhost:37701/api/health"
SYNC_STAMP = HOME / ".kg-hub" / "state" / "claude-mem-synced.obsid"
SYNC_LOG = HOME / ".kg-hub" / "logs" / "claude-mem-sync.out.log"
NAS_DB = "/volume2/4T/kg-hub-data/claude-mem/claude-mem.db"

# 状态枚举。green=健康 / amber=空闲但不算故障 / red=故障 / grey=未配置或不适用
GREEN, AMBER, RED, GREY = "green", "amber", "red", "grey"

# 工具层阈值：工具"没在用"不是故障，所以只有 green/amber，never red
TOOL_FRESH_S = 4 * 3600          # 4h 内有产出 = 活跃
# sync 跳阈值：这一跳是 T-0028 的现场，判据要严
# 同步健康的判据是**距上次成功同步多久**，不是落后多少条。
#
# 原来用 SYNC_LAG_ROWS_RED=50 条判故障 —— 错得很实在:实测正常单个 15 分钟
# 周期就经常新增 49/52/55/56 条，阈值直接落在正常范围里面。探针每 10 分钟
# 跑一次、与 15 分钟的同步周期错开，于是随便赶上一个活跃周期就报 red。
# 2026-08-23 真的这么误报了一次(落差 69，实际上 14 分钟前刚同步成功)。
# 条数随活跃度浮动，是**工作量**不是**健康度**；时间才是。
#
# stamp 的 mtime 只在同步**成功**时更新，所以它就是"上次成功同步"的时刻。
# 空闲期(没有新 obs，脚本报 skip)stamp 也不动，因此所有判据都加 lag>0 前置：
# 落差为 0 就是健康，跟多久没同步无关(比如笔记本睡了一夜)。
SYNC_STALL_RED_S = 45 * 60       # 连续 3 个周期没能成功同步 = 故障
SYNC_STALL_AMBER_S = 25 * 60     # 跳过 1 个周期 = 留意
SYNC_LAG_ROWS_HUGE = 500         # 落差大到不像单周期的量，单独提示积压

# 已知工具全集 —— **无数据也要列出来**（灰色"未接入"），否则"某工具从未接入"
# 这件事本身在图上不可见，正是 T-0021 那类问题的温床。
# key = claude-mem 的 platform_source；label = 展示名；note = 没数据时的说明
KNOWN_TOOLS = [
    ("claude", "Claude Code", "IDE，hook 完整"),
    ("codex", "Codex", "IDE，hook 完整"),
    ("cursor", "Cursor", "IDE"),
    ("qoder", "Qoder", "IDE，经 muxcp"),
    ("hermes", "Hermes", "cc-switch 已知 app，未见采集接入"),
    ("opencode", "OpenCode", "cc-switch 已知 app，未见采集接入"),
    ("gemini", "Gemini CLI", "cc-switch 已知 app，未见采集接入"),
]

# OpenClaw 不走 claude-mem：它在 oc-vps 上产 markdown 胶囊，由 Mac 侧
# sync_openclaw 拉回再入图。所以它是一条独立支线，单独建模。
OPENCLAW_SYNC_LOG = HOME / ".kg-hub" / "logs" / "openclaw-sync.out.log"
OPENCLAW_SNAPSHOT = Path("/Users/mac/workspace_claudeCode/kg-hub/data")

# 设备维度：来自 tailscale status。探针只能看清自己所在的那台，
# 其它设备只报可达性 —— 但"有哪些设备"这个清单必须完整。
DEVICE_ROLE = {
    # host: (框内短标签≤8字, 详情里的全称)
    "home-nas-syno": ("kg-hub", "kg-hub server + FalkorDB 图谱"),
    "oc-vps-aliyun-us": ("OpenClaw", "OpenClaw agent，产 markdown 知识胶囊"),
    "win-thinkpad": ("Win 开发机", "Windows 开发机，未接入采集"),
    "oneplus-5": ("移动端", "Android，未接入采集"),
}


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def human_idle(seconds: float | None) -> str:
    """把空闲秒数写成人能一眼读懂的形式。"""
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 90:
        return f"{s}秒"
    if s < 5400:
        return f"{s // 60}分钟"
    if s < 172800:
        return f"{s // 3600}小时"
    return f"{s // 86400}天"


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def http_json(url: str, timeout: float = 5.0, token: str | None = None,
              method: str = "GET", payload: dict | None = None) -> tuple[dict | None, str | None]:
    """返回 (json, error)。任一失败都不抛，交给调用方判状态。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode(errors="replace")
        return (json.loads(body) if body.strip() else {}), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}"


def sh(cmd: list[str], timeout: float = 12.0) -> tuple[str, str | None]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return p.stdout.strip(), (p.stderr.strip() or f"exit {p.returncode}")[:120]
        return p.stdout.strip(), None
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}"


# --------------------------------------------------------------------------
# 各层采集
# --------------------------------------------------------------------------

def probe_tools() -> tuple[list[dict], dict]:
    """工具层：KNOWN_TOOLS 全量列出（无数据的也列，灰色标"未接入"）。

    判据用 MAX 不用 COUNT（规则 1）。工具"没在用"不算故障，所以
    活跃→green / 有数据但久未用→amber / 从未有数据→grey。
    """
    nodes: list[dict] = []
    seen: dict[str, dict] = {}
    stats: dict[str, tuple[int, float | None]] = {}

    if CM_DB.exists():
        try:
            con = sqlite3.connect(f"file:{CM_DB}?mode=ro", uri=True, timeout=8)
            for src, obs, last_ms in con.execute("""
                SELECT s.platform_source AS src,
                       COUNT(o.id)       AS obs,
                       MAX(o.created_at_epoch) AS last_ms
                FROM observations o
                JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id
                GROUP BY s.platform_source
            """).fetchall():
                stats[src or "?"] = (int(obs or 0), last_ms)
            con.close()
        except Exception as e:  # noqa: BLE001
            return [{"id": "tool:_error", "layer": "tool", "label": "工具层取数失败",
                     "state": RED, "detail": f"{type(e).__name__}"}], seen

    nowms = time.time() * 1000
    for key, label, note in KNOWN_TOOLS:
        obs, last_ms = stats.pop(key, (0, None))
        idle = (nowms - last_ms) / 1000 if last_ms else None
        if obs == 0:
            state, detail = GREY, f"从未有采集数据｜{note}"
        elif idle is not None and idle < TOOL_FRESH_S:
            state, detail = GREEN, f"{obs} 条观察，最后 {human_idle(idle)}前"
        else:
            state, detail = AMBER, f"{obs} 条观察，已空闲 {human_idle(idle)}"
        node = {"id": f"tool:{key}", "layer": "tool", "label": label, "state": state,
                "idle_seconds": None if idle is None else int(idle),
                "idle_human": human_idle(idle) if obs else "无数据",
                "metrics": {"obs": obs}, "detail": detail}
        nodes.append(node)
        if obs:
            seen[key] = node
    # 数据库里出现了 KNOWN_TOOLS 之外的来源 → 也列出来（清单要完整）
    for key, (obs, last_ms) in stats.items():
        idle = (nowms - last_ms) / 1000 if last_ms else None
        node = {"id": f"tool:{key}", "layer": "tool", "label": key,
                "state": GREEN if (idle or 1e9) < TOOL_FRESH_S else AMBER,
                "idle_seconds": None if idle is None else int(idle),
                "idle_human": human_idle(idle), "metrics": {"obs": obs},
                "detail": f"{obs} 条观察（KNOWN_TOOLS 未登记的来源）"}
        nodes.append(node)
        seen[key] = node

    # 排序：无数据的（grey）沉底，其余按 KNOWN_TOOLS 声明序 —— **不能按新鲜度排**。
    # 按新鲜度排会让每次 60s 刷新时节点上下互换位置，看板上找一个工具就得重新扫一遍，
    # 空间记忆全废。监控面板的位置稳定性 > 让最活跃的排最前。
    decl = {k: i for i, (k, *_ ) in enumerate(KNOWN_TOOLS)}
    nodes.sort(key=lambda n: (n["state"] == GREY,
                              decl.get(n["id"].split(":", 1)[-1], 1 << 20)))
    return nodes, seen


def probe_openclaw() -> list[dict]:
    """OpenClaw 支线：它在 oc-vps 上产 markdown 胶囊，不走 claude-mem。

    Mac 侧只能看到 sync 这一段（launchd 跑的 openclaw-sync）；VPS 内部
    状态要等那台机也装探针。清单完整性优先——即使只能看一段也要画出来。
    """
    nodes = []
    log_idle, last_line = None, ""
    if OPENCLAW_SYNC_LOG.exists():
        log_idle = time.time() - OPENCLAW_SYNC_LOG.stat().st_mtime
        try:
            lines = OPENCLAW_SYNC_LOG.read_text(errors="replace").splitlines()
            last_line = lines[-1] if lines else ""
        except Exception:  # noqa: BLE001
            pass
    # 快照目录的新鲜度 = 最近一次真的拉到了东西
    snap_idle = None
    if OPENCLAW_SNAPSHOT.exists():
        newest = 0.0
        for p in OPENCLAW_SNAPSHOT.glob("openclaw-snapshot-*/**/*.md"):
            try:
                newest = max(newest, p.stat().st_mtime)
            except Exception:  # noqa: BLE001
                continue
        if newest:
            snap_idle = time.time() - newest

    nodes.append({
        "id": "tool:openclaw", "layer": "tool", "label": "OpenClaw",
        "state": GREY if log_idle is None else (GREEN if log_idle < 3 * 3600 else AMBER),
        "idle_human": human_idle(log_idle) if log_idle is not None else "无数据",
        "idle_seconds": None if log_idle is None else int(log_idle),
        "detail": ("在 oc-vps 上产 markdown 胶囊，不走 claude-mem hook｜"
                   + (f"sync 日志 {human_idle(log_idle)}前更新" if log_idle is not None
                      else "本机无 openclaw-sync 日志")),
    })
    nodes.append({
        "id": "sync:openclaw", "layer": "transport", "label": "OpenClaw",
        "state": GREY if log_idle is None else (GREEN if log_idle < 3 * 3600 else AMBER),
        "idle_human": human_idle(log_idle) if log_idle is not None else "—",
        "idle_seconds": None if log_idle is None else int(log_idle),
        "detail": (f"胶囊快照最后更新 {human_idle(snap_idle)}前｜"
                   f"最后日志：{last_line[-48:] or '—'}"),
    })
    return nodes


def probe_devices() -> list[dict]:
    """设备维度：清单来自 tailscale status。

    探针只能看清自己那台机；其它设备只报可达性。但"有哪些设备、
    各自什么角色、离线多久"这份清单必须完整可见。
    """
    out, err = sh(["tailscale", "status"], timeout=10)
    if err and not out:
        return [{"id": "dev:_none", "layer": "device", "label": "设备清单不可用",
                 "state": GREY, "detail": f"tailscale status 失败（{err}）"}]
    me = os.uname().nodename.split(".")[0]
    nodes = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0][0].isdigit():
            continue
        ip, host, os_name = parts[0], parts[1], parts[3]
        rest = " ".join(parts[4:])
        is_self = ("-" == rest.strip()) or host.startswith(me.lower())
        short, full = DEVICE_ROLE.get(host, ("本机", "主力开发机") if is_self else ("", "未登记"))
        if "offline" in rest:
            m = re.search(r"last seen (\S+)", rest)
            state = AMBER
            badge = f"离线 {m.group(1) if m else '?'}"
            detail = f"{os_name}｜离线（最后 {m.group(1) if m else '?'}）"
        elif is_self:
            state, badge, detail = GREEN, "本机·探针", f"{os_name}｜本机，探针在此运行"
        else:
            live = "active" if "active" in rest else "idle"
            state, badge = GREEN, short or "在线"
            detail = f"{os_name}｜在线（{live}）"
        short_host = host.replace("-aliyun-us","").replace("-syno","")
        nodes.append({"id": f"dev:{host}", "layer": "device", "label": short_host,
                      "state": state, "idle_human": badge,
                      "metrics": {"ip": ip},
                      "detail": f"{detail}｜角色：{full}"
                                + ("" if is_self else "｜该机无探针，内部状态不可见")})
    return nodes


def probe_hooks(tool_seen: dict) -> list[dict]:
    """hook 层：配置是否在 + 是否存在已知的失信风险。

    codex 的 trusted_hash 机制是 T-0021 的现场：approve 过的 hook 集合与
    磁盘上实际的 hook 集合不一致 → Codex 静默拒绝执行。这里把两个集合都
    数出来做对比，而**不是数 trusted_hash 的条数**（数量不是判据，见
    INVESTIGATION-RULES 附录「判据陷阱」）。
    """
    nodes: list[dict] = []

    # --- Claude Code：plugin 注册在 settings.json ---
    cc_cfg = HOME / ".claude" / "settings.json"
    cc_state, cc_detail = GREY, "settings.json 不存在"
    if cc_cfg.exists():
        txt = cc_cfg.read_text(errors="replace")
        if "claude-mem" in txt or "thedotmack" in txt:
            cc_state, cc_detail = GREEN, "plugin 已注册"
        else:
            # 也可能通过 --plugin-dir 注入，数据能说明真相
            has_data = "claude" in tool_seen
            cc_state = AMBER if has_data else RED
            cc_detail = ("settings.json 无 claude-mem，但有数据流入"
                         "（可能经 --plugin-dir 注入）" if has_data
                         else "settings.json 无 claude-mem 且无数据")
    nodes.append({"id": "hook:claude", "layer": "hook", "label": "Claude Code",
                  "state": cc_state, "detail": cc_detail})

    # --- Codex：trusted_hash 集合 vs 磁盘 hook 集合 ---
    cx_cfg = HOME / ".codex" / "config.toml"
    cx_state, cx_detail = GREY, "config.toml 不存在"
    if cx_cfg.exists():
        txt = cx_cfg.read_text(errors="replace")
        approved = set(re.findall(r"codex-hooks\.json:([a-z_]+:\d+:\d+)", txt))
        on_disk: set[str] = set()
        ev_map = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit",
                  "PreToolUse": "pre_tool_use", "PostToolUse": "post_tool_use", "Stop": "stop"}
        for hp in sorted(HOME.glob(".codex/plugins/cache/*/claude-mem/*/hooks/codex-hooks.json")):
            try:
                hooks = json.loads(hp.read_text())["hooks"]
            except Exception:  # noqa: BLE001
                continue
            for ev, arr in hooks.items():
                for gi, grp in enumerate(arr if isinstance(arr, list) else []):
                    for hi, _ in enumerate(grp.get("hooks", [])):
                        on_disk.add(f"{ev_map.get(ev, ev.lower())}:{gi}:{hi}")
            break  # 只看第一个（最新）安装
        if not on_disk:
            cx_state = AMBER if "codex" in tool_seen else RED
            cx_detail = "磁盘上找不到 codex-hooks.json"
        else:
            missing = on_disk - approved     # 磁盘上有但没 approve → 会被静默拒绝
            stale = approved - on_disk       # approve 过但 hook 已不存在 → 无害残留
            if missing:
                cx_state = RED
                cx_detail = (f"{len(missing)} 个 hook 未 approve（会被静默拒绝）："
                             + ", ".join(sorted(missing)))
            else:
                cx_state = GREEN
                cx_detail = f"{len(on_disk)} 个 hook 全部已 approve"
                if stale:
                    cx_detail += f"；另有 {len(stale)} 条旧残留（无害）"
    nodes.append({"id": "hook:codex", "layer": "hook", "label": "Codex",
                  "state": cx_state, "detail": cx_detail})

    # --- Cursor / Qoder：有 hooks 配置文件即算配置在，真相看数据 ---
    for src, label, globs in (
        ("cursor", "Cursor", [".cursor/hooks.json", ".cursor/settings.json"]),
        ("qoder", "Qoder", [".qoder/settings.json", ".qoder/hooks.json"]),
    ):
        found = [g for g in globs if (HOME / g).exists()
                 and "claude-mem" in (HOME / g).read_text(errors="replace")]
        if found:
            st, dt = GREEN, f"已配置（{found[0]}）"
        elif src in tool_seen:
            st, dt = AMBER, "未见 claude-mem 配置，但历史有数据"
        else:
            st, dt = GREY, "未配置"
        nodes.append({"id": f"hook:{src}", "layer": "hook", "label": label,
                      "state": st, "detail": dt})
    return nodes


def probe_worker() -> dict:
    """claude-mem worker：唯一真正的"服务不可达就是红"的节点。"""
    data, err = http_json(CM_HEALTH, timeout=4)
    if err or not data:
        return {"id": "worker", "layer": "worker", "label": "claude-mem",
                "state": RED, "detail": f"health 不可达（{err or 'empty'}）@ :37701"}
    up = int(data.get("uptime") or 0)
    return {"id": "worker", "layer": "worker", "label": "claude-mem",
            "state": GREEN if data.get("status") == "ok" else RED,
            "metrics": {"pid": data.get("pid"), "uptime_s": up},
            "detail": (f"v{data.get('version')} pid={data.get('pid')} "
                       f"已运行 {human_idle(up)}｜{(data.get('ai') or {}).get('authMethod', '?')}")}


def probe_sqlite() -> tuple[dict, int | None]:
    """SQLite 层：WAL 模式下主库 mtime 会骗人，所以三个都量。"""
    if not CM_DB.exists():
        return ({"id": "sqlite", "layer": "storage", "label": "SQLite",
                 "state": RED, "detail": "claude-mem.db 不存在"}, None)
    wal = CM_DB.with_name(CM_DB.name + "-wal")
    now = time.time()
    main_idle = now - CM_DB.stat().st_mtime
    wal_idle = (now - wal.stat().st_mtime) if wal.exists() else None
    wal_mb = (wal.stat().st_size / 1048576) if wal.exists() else 0.0
    max_id, mode = None, "?"
    try:
        con = sqlite3.connect(f"file:{CM_DB}?mode=ro", uri=True, timeout=8)
        mode = con.execute("PRAGMA journal_mode;").fetchone()[0]
        max_id = con.execute("SELECT MAX(id) FROM observations;").fetchone()[0]
        con.close()
    except Exception as e:  # noqa: BLE001
        return ({"id": "sqlite", "layer": "storage", "label": "SQLite",
                 "state": RED, "detail": f"读库失败 {type(e).__name__}"}, None)
    # 真正的新鲜度看 WAL（WAL 模式下），不看主库
    eff_idle = wal_idle if (mode == "wal" and wal_idle is not None) else main_idle
    state = GREEN if eff_idle < TOOL_FRESH_S else AMBER
    detail = (f"journal_mode={mode}｜MAX(obs.id)={max_id}｜"
              f"WAL {wal_mb:.1f}MB 更新于 {human_idle(wal_idle)}前｜"
              f"主库 mtime {human_idle(main_idle)}前")
    if mode == "wal" and wal_idle is not None and main_idle - wal_idle > 3600:
        detail += "  ⚠️ 主库 mtime 明显滞后于 WAL —— 任何用主库 mtime/sha 判变化的逻辑都会失效"
    return ({"id": "sqlite", "layer": "storage", "label": "SQLite",
             "state": state, "idle_seconds": int(eff_idle), "idle_human": human_idle(eff_idle),
             "metrics": {"max_obs_id": max_id, "wal_mb": round(wal_mb, 1), "journal_mode": mode},
             "detail": detail}, max_id)


def probe_sync(local_max: int | None, nas_host: str | None) -> dict:
    """Mac→NAS 同步跳：T-0028 的现场。判据是两侧 MAX(obs.id) 的落差。"""
    last_line = ""
    if SYNC_LOG.exists():
        try:
            tail = SYNC_LOG.read_text(errors="replace").splitlines()
            last_line = tail[-1] if tail else ""
        except Exception:  # noqa: BLE001
            pass
    stamp_age = None
    if SYNC_STAMP.exists():
        stamp_age = time.time() - SYNC_STAMP.stat().st_mtime

    nas_max: int | None = None
    nas_integrity: str | None = None
    nas_err = None
    if nas_host:
        # 一次往返同时取完整性和 watermark。**必须查 integrity_check**：
        # 传输被截断时 NAS 上会落一个短文件，SELECT MAX(id) 直接报
        # "database disk image is malformed"、取不到数 —— 如果只看 MAX(id)
        # 拿不到就判 amber「读不到」，那么**库损坏这件事就是静默的**
        # （amber 按设计不告警）。这正是本项目要抓的失效类型，2026-08-21
        # 在探针自己身上复现了一次。
        out, nas_err = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                           "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", nas_host,
                           f"sqlite3 -readonly '{NAS_DB}' 'PRAGMA integrity_check;' 2>&1 | head -1; "
                           f"echo '@@'; "
                           f"sqlite3 -readonly '{NAS_DB}' 'SELECT MAX(id) FROM observations;' 2>&1"],
                          timeout=25)
        if "@@" in out:
            integ_raw, max_raw = out.split("@@", 1)
            nas_integrity = integ_raw.strip()
            if max_raw.strip().isdigit():
                nas_max = int(max_raw.strip())

    node = {"id": "sync", "layer": "transport", "label": "Mac→NAS",
            "metrics": {"local_max_obs_id": local_max, "nas_max_obs_id": nas_max,
                        "nas_integrity": nas_integrity}}

    # 库损坏 = RED。它和"NAS 连不上"是两件完全不同的事：前者是数据已经坏了、
    # ingester 正在读一个坏库；后者多半是 tailscale 抖一下，下轮就好。
    if nas_integrity and nas_integrity != "ok":
        node["state"] = RED
        node["detail"] = (f"⚠️ NAS 侧 db 已损坏：{nas_integrity[:80]}"
                          f"｜ingester 正在读一个坏库，需重传全量（见 T-0033）")
        return node

    if nas_max is None:
        # 走到这里说明 ssh 本身没通（或输出格式不对）—— 传输层抖动，暂态。
        node["state"] = AMBER
        node["detail"] = f"读不到 NAS 侧 db（{nas_err or 'no host'}）｜最后日志：{last_line[-60:]}"
        return node

    lag = (local_max or 0) - nas_max
    age = stamp_age or 0
    node["metrics"]["lag_rows"] = lag
    node["metrics"]["since_last_sync_s"] = int(age) if stamp_age else None

    if lag <= 0:
        node["state"] = GREEN          # 追平了就是健康，无关多久没同步
    elif age > SYNC_STALL_RED_S:
        node["state"] = RED
    elif age > SYNC_STALL_AMBER_S:
        node["state"] = AMBER
    else:
        node["state"] = GREEN          # 刚同步过，落差只是本周期的正常累积

    node["idle_seconds"] = int(age) if stamp_age else None
    node["idle_human"] = human_idle(stamp_age)
    node["detail"] = (f"落差 {lag} 条（本机 {local_max} / NAS {nas_max}）｜"
                      f"上次成功同步 {human_idle(stamp_age)}前｜最后日志：{last_line[-48:]}")
    if node["state"] == RED:
        node["detail"] += (f"  ⚠️ 已 {human_idle(age)}没能成功同步且仍有 {lag} 条未过去，"
                           f"kg-hub 拿不到新观察")
    elif lag >= SYNC_LAG_ROWS_HUGE:
        node["detail"] += f"  ｜积压 {lag} 条偏大，留意是否在追赶"
    return node


def probe_kghub(url: str | None, token: str | None) -> list[dict]:
    """kg-hub 服务 + 图谱规模。"""
    if not url:
        return [{"id": "kghub", "layer": "kghub", "label": "kg-hub",
                 "state": GREY, "detail": "未配置 KG_HUB_URL"}]
    nodes = []
    h, err = http_json(f"{url}/health", timeout=6)
    ok = bool(h) and h.get("status") == "ok"
    nodes.append({"id": "kghub", "layer": "kghub", "label": "kg-hub",
                  "state": GREEN if ok else RED,
                  "detail": (f"{url} 健康" if ok else f"{url} 不可达（{err or 'bad status'}）")})
    if ok:
        st, _ = http_json(f"{url}/api/stats", timeout=8, token=token)
        if isinstance(st, dict) and st:
            ent = st.get("entities") or st.get("nodes")
            nodes.append({"id": "falkordb", "layer": "graph", "label": "FalkorDB",
                          "state": GREEN,
                          "metrics": {k: v for k, v in st.items() if isinstance(v, int)},
                          "detail": f"entities={ent} edges={st.get('edges')} episodes={st.get('episodes')}"})
        else:
            nodes.append({"id": "falkordb", "layer": "graph", "label": "FalkorDB",
                          "state": AMBER, "detail": "/api/stats 取数失败（可能需要 token）"})
    return nodes


# --------------------------------------------------------------------------

def build_edges(tool_seen: dict, nodes_by_id: dict) -> list[dict]:
    """链路的边。边的状态 = 下游节点是否收到了上游的东西。"""
    def st(nid: str) -> str:
        return (nodes_by_id.get(nid) or {}).get("state", GREY)

    edges = []
    # 本机设备 → 它上面跑的工具（只连本机那台，其它设备没探针连不出细节）
    me = os.uname().nodename.split(".")[0].lower()
    self_dev = next((n["id"] for n in nodes_by_id.values()
                     if n["layer"] == "device" and (
                         "本机" in (n.get("detail") or "") or me.startswith(n["label"].lower()))), None)

    for key, _, _ in KNOWN_TOOLS:
        tid, hid = f"tool:{key}", f"hook:{key}"
        if tid not in nodes_by_id:
            continue
        if self_dev:
            edges.append({"from": self_dev, "to": tid, "state": st(tid)})
        if hid not in nodes_by_id:
            continue
        edges.append({"from": tid, "to": hid,
                      "state": GREY if st(tid) == GREY else (
                          st(hid) if st(hid) != GREEN else GREEN)})
        edges.append({"from": hid, "to": "worker",
                      "state": GREY if st(tid) == GREY else (
                          GREEN if (key in tool_seen and st(hid) == GREEN) else st(hid))})

    # OpenClaw 支线：VPS 设备 → OpenClaw → 拉取 → kg-hub（绕过 claude-mem）
    oc_dev = next((n["id"] for n in nodes_by_id.values()
                   if n["layer"] == "device" and "oc-vps" in n["label"]), None)
    if "tool:openclaw" in nodes_by_id:
        if oc_dev:
            edges.append({"from": oc_dev, "to": "tool:openclaw", "state": st("tool:openclaw")})
        if "sync:openclaw" in nodes_by_id:
            edges.append({"from": "tool:openclaw", "to": "sync:openclaw",
                          "state": st("sync:openclaw")})
            edges.append({"from": "sync:openclaw", "to": "kghub",
                          "state": st("sync:openclaw") if st("sync:openclaw") != GREEN
                          else st("kghub")})
    # NAS 设备 → kg-hub
    nas_dev = next((n["id"] for n in nodes_by_id.values()
                    if n["layer"] == "device" and "nas" in n["label"].lower()), None)
    if nas_dev and "kghub" in nodes_by_id:
        edges.append({"from": nas_dev, "to": "kghub", "state": st("kghub")})

    edges += [
        {"from": "worker", "to": "sqlite", "state": st("sqlite")},
        {"from": "sqlite", "to": "sync", "state": st("sync")},
        {"from": "sync", "to": "kghub", "state": st("sync") if st("sync") != GREEN else st("kghub")},
        {"from": "kghub", "to": "falkordb", "state": st("falkordb")},
    ]
    return [e for e in edges if e["from"] in nodes_by_id and e["to"] in nodes_by_id]


def collect() -> dict:
    env = read_env(CM_ENV)
    url = (env.get("KG_HUB_URL") or "").rstrip("/") or None
    token = env.get("KG_HUB_API_TOKEN") or None
    nas_host = None
    if env.get("KG_HUB_FALKORDB_HOST"):
        nas_host = f"commiao@{env['KG_HUB_FALKORDB_HOST']}"

    device_nodes = probe_devices()
    tool_nodes, tool_seen = probe_tools()
    oc_nodes = probe_openclaw()
    tool_nodes += [n for n in oc_nodes if n["layer"] == "tool"]
    hook_nodes = probe_hooks(tool_seen)
    # hook 行序对齐工具行序，否则拓扑图上 tool→hook 的连线会交叉
    order = [n["id"].split(":", 1)[1] for n in tool_nodes]
    hook_nodes.sort(key=lambda h: (order.index(h["id"].split(":", 1)[1])
                                   if h["id"].split(":", 1)[1] in order else 99))
    worker = probe_worker()
    sqlite_node, local_max = probe_sqlite()
    sync_node = probe_sync(local_max, nas_host)
    kg_nodes = probe_kghub(url, token)

    nodes = (device_nodes + tool_nodes + hook_nodes + [worker, sqlite_node, sync_node]
             + [n for n in oc_nodes if n["layer"] == "transport"] + kg_nodes)
    by_id = {n["id"]: n for n in nodes}
    edges = build_edges(tool_seen, by_id)

    worst = RED if any(n["state"] == RED for n in nodes) else (
        AMBER if any(n["state"] == AMBER for n in nodes) else GREEN)
    blockers = [{"id": n["id"], "label": n["label"], "detail": n.get("detail", "")}
                for n in nodes if n["state"] == RED]

    return {
        "generated_at": now_iso(),
        "host": os.uname().nodename.split(".")[0],
        "overall": worst,
        "blockers": blockers,
        "layers": ["device", "tool", "hook", "worker", "storage", "transport",
                   "kghub", "graph"],
        "nodes": nodes,
        "edges": edges,
    }


def pretty(snap: dict) -> str:
    icon = {GREEN: "🟢", AMBER: "🟡", RED: "🔴", GREY: "⚪"}
    out = [f"采集链路拓扑  {snap['generated_at']}  host={snap['host']}  "
           f"总体={icon[snap['overall']]}{snap['overall']}", ""]
    for layer in snap["layers"]:
        group = [n for n in snap["nodes"] if n["layer"] == layer]
        if not group:
            continue
        out.append(f"[{layer}]")
        for n in group:
            idle = n.get("idle_human") or "—"
            out.append(f"  {icon[n['state']]} {n['label']:<22} 空闲 {idle:<8} {n.get('detail','')}")
        out.append("")
    if snap["blockers"]:
        out.append("阻塞点：")
        out += [f"  🔴 {b['label']}: {b['detail']}" for b in snap["blockers"]]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--report", action="store_true", help="POST 到 kg-hub /api/topology/report")
    ap.add_argument("--pretty", action="store_true", help="人眼可读输出")
    ap.add_argument("--out", type=str, default=None, help="同时写入该文件")
    ap.add_argument("--exit-zero", action="store_true",
                    help="总是返回 0。给 launchd/cron 用 —— 黄灯是被观测的常态而非"
                         "装置故障，让退出码携带健康度会把定时任务标成 errored，"
                         "淹掉真正的装置异常。健康度请从面板 / --out 的 JSON 读。")
    ap.add_argument("--html", type=str, default=None,
                    help="渲染成独立 HTML 文件（本机直接用浏览器打开，"
                         "不依赖 kg-hub 部署 —— 面板上线前的临时通道）")
    args = ap.parse_args()

    snap = collect()
    text = pretty(snap) if args.pretty else json.dumps(snap, ensure_ascii=False, indent=2)
    print(text)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(snap, ensure_ascii=False, indent=2))

    if args.html:
        # 复用面板的同一套模板渲染，保证本机预览与 kg-hub 上线后长得一样
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        try:
            import topology  # noqa: PLC0415
            view = dict(snap)
            view.update({"_host": snap["host"], "_recv": snap["generated_at"],
                         "_age_s": 0, "_stale": False})
            html = topology._HTML.replace("__DATA__", json.dumps(
                {"snapshots": [view], "layers": topology.LAYERS,
                 "stale_after_s": topology.STALE_AFTER_S}, ensure_ascii=False))
            p = Path(args.html)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(html)
            print(f"[html] {p}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[html] 渲染失败: {type(e).__name__}: {e}", file=sys.stderr)

    if args.report:
        env = read_env(CM_ENV)
        url = (env.get("KG_HUB_URL") or "").rstrip("/")
        if not url:
            print("[report] 跳过：未配置 KG_HUB_URL", file=sys.stderr)
            return 0
        _, err = http_json(f"{url}/api/topology/report", timeout=10,
                           token=env.get("KG_HUB_API_TOKEN"), method="POST", payload=snap)
        print(f"[report] {'失败: ' + err if err else 'ok'}", file=sys.stderr)

    # 退出码：红=2，黄=1，绿=0 —— 方便被 shell / 监控直接消费。
    # 但定时任务要用 --exit-zero：见该参数的 help。
    if args.exit_zero:
        return 0
    return {RED: 2, AMBER: 1}.get(snap["overall"], 0)


if __name__ == "__main__":
    sys.exit(main())
