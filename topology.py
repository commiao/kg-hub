"""
topology — 采集链路动态拓扑图（面板 + 上报 API）。

数据由 Mac 侧探针 `tools/capture_probe.py --report` POST 上来，
存成图里单个 :TopologySnapshot 节点（按 host 去重，MERGE 覆盖）。

暴露两个 route（在 kg_hub_server.py 里接线）：
    GET  /dashboard/topology      → SVG 拓扑图页面
    POST /api/topology/report     → 探针上报快照

为什么单独成文件：kg_hub_server.py 已 3900+ 行且常有多方并行改动，
新功能放独立模块可以零冲突合并。依赖用函数内延迟 import 避免循环。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from dashboard_status import apply_gateway_health, gateway_health, refinery_activity

from utils.device_liveness import (DEFAULT_CAPTURE_STALE_AFTER_S,
                                   DEFAULT_MAX_AGE_S, DEFAULT_PATH, device_state,
                                   load_config, load_status, positive_int)

# 链路分层：从左到右就是数据流向
import breakers  # noqa: E402  —— 与本模块同级,放在常量前便于阅读

LAYERS = [
    ("device", "设备"),
    ("tool", "工具"),
    ("hook", "hook"),
    ("worker", "worker"),
    ("storage", "存储"),
    ("transport", "传输"),
    ("nasdb", "NAS 副本"),
    ("consumer", "消费容器"),
    ("kghub", "kg-hub"),
    ("graph", "图谱 / 模型"),
]

MAX_SNAPSHOT_BYTES = 256 * 1024   # 单份快照上限，防误传大 payload
STALE_AFTER_S = DEFAULT_CAPTURE_STALE_AFTER_S  # capture_probe 兼容导出


def capture_stale_after_s(device_cfg: dict | None = None) -> int:
    """公开 device-liveness 配置是 dashboard/watchdog 阈值的唯一真源。"""
    cfg = device_cfg if isinstance(device_cfg, dict) else {}
    minutes = positive_int(
        cfg.get("capture_stale_after_min"), DEFAULT_CAPTURE_STALE_AFTER_S // 60)
    return minutes * 60


def annotate_liveness(snap: dict, liveness: dict, aliases: object = None,
                      *, stale_after_s: int = STALE_AFTER_S) -> dict:
    """把独立设备在线态合并到快照的展示/告警元数据。

    ``_snapshot_stale`` 只陈述快照年龄；``_stale`` 是可告警语义，必须同时满足
    设备明确 online。这样 Mac 睡眠显示断线，而旧 online 采样也不会冒充实况。
    """
    state, detail = device_state(liveness, snap.get("_host"), aliases)
    age = snap.get("_age_s")
    snapshot_stale = bool(isinstance(age, (int, float)) and age > stale_after_s)
    snap["_device_state"] = state
    snap["_device_liveness_detail"] = detail
    snap["_snapshot_stale"] = snapshot_stale
    snap["_stale"] = bool(snapshot_stale and state == "online")
    snap["_disconnected"] = state == "offline"
    return snap


# 模型网关配额:kg-hub 的每次抽取都要经它调模型,日上限打满则整条线停摆
# (2026-09-06 夜 kg_hub 打满 5000,218 篇失败,而拓扑上没有任何一格变色)。
GATEWAY_USAGE_PATH = Path(os.environ.get("KG_HUB_GATEWAY_USAGE", "/gateway-usage/usage.json"))
REFINERY_STATUS_PATH = Path(os.environ.get("KG_HUB_REFINERY_STATUS", "/refinery-state/status.json"))
GATEWAY_USAGE_STALE_S = 15 * 60     # export 每 ~90s 跑一次;15 分钟没更新就别再当实况
GATEWAY_AMBER_RATIO = 0.8
GATEWAY_PRIMARY_KEY = "kg_hub.entity_extract"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def gateway_quota_node(usage: dict | None, refinery_status: dict | None, *,
                       now: datetime, stale_after_s: int = GATEWAY_USAGE_STALE_S
                       ) -> tuple[dict, dict]:
    """把网关「今日用量 / 日上限」折成拓扑上的一个节点 + 一条 kg-hub→网关的边。

    数据只来自两份 NAS 本地文件,不打网关、不查库:
    - usage.json:daily 来自见证库副本;effective_limits 来自网关零调用健康接口
    - refinery status.json:quota_paused / quota_hits(refinery 撞到 429 后的整窗停发)
    日按 UTC 算,与见证库 daily_counts 的口径一致。
    状态:红 = 已打满或 refinery 正因配额停发;黄 = 任一键 ≥80%;灰 = 没有可信数据;绿 = 其余。
    """
    node: dict = {"id": "gateway", "layer": "graph", "label": "模型网关",
                  "state": "grey", "detail": "", "metrics": {}}
    edge = {"from": "kghub", "to": "gateway", "state": "grey"}
    if not usage:
        node["detail"] = f"未找到网关用量快照 {GATEWAY_USAGE_PATH}"
        node["sub"] = "无用量快照"
        return node, edge
    generated = usage.get("generated_at")
    try:
        age_s = int((now - datetime.fromisoformat(str(generated))).total_seconds())
    except (TypeError, ValueError):
        age_s = None
    ceilings = usage.get("ceilings") if isinstance(usage.get("ceilings"), dict) else {}
    limits = usage.get("effective_limits") if isinstance(usage.get("effective_limits"), dict) else {}
    today = now.strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    for item in usage.get("daily") or []:
        if isinstance(item, dict) and item.get("day") == today:
            key = str(item.get("business_key"))
            counts[key] = counts.get(key, 0) + int(item.get("count") or 0)
    keys = sorted(set(counts) | set(limits) | set(ceilings))
    per_key: dict[str, dict] = {}
    worst = 0.0
    for key in keys:
        cap = (limits.get(key) or {}).get("daily_requests") if isinstance(limits.get(key), dict) else None
        cap = cap if type(cap) is int and cap > 0 else None
        used = counts.get(key, 0)
        ratio = (used / cap) if isinstance(cap, int) and cap > 0 else None
        per_key[key] = {"today": used, "daily_requests": cap,
                        "approval_ceiling": ((ceilings.get(key) or {}).get("daily_requests")
                                             if isinstance(ceilings.get(key), dict) else None),
                        "ratio": (round(ratio, 4) if ratio is not None else None)}
        if ratio is not None and key == GATEWAY_PRIMARY_KEY:
            worst = max(worst, ratio)
    status = refinery_status or {}
    paused = bool(status.get("quota_paused"))
    hits = int(status.get("quota_hits") or 0)
    node["metrics"] = {"generated_at": generated, "age_s": age_s, "day": today,
                       "keys": per_key, "quota_paused": paused, "quota_hits": hits}

    primary = per_key.get(GATEWAY_PRIMARY_KEY)
    if primary and isinstance(primary["daily_requests"], int):
        pct = f"{primary['ratio'] * 100:.0f}%" if primary["ratio"] is not None else "?"
        node["sub"] = f"今日 {primary['today']}/{primary['daily_requests']} · {pct}"
    elif primary:
        node["sub"] = f"今日 {primary['today']} · 上限未知"
    else:
        node["sub"] = "今日无 kg-hub 调用"

    lines = [f"{k}: 今日 {v['today']} / 实际日上限 {v['daily_requests'] if v['daily_requests'] is not None else '?'}"
             + (f"({v['ratio'] * 100:.0f}%)" if v["ratio"] is not None else "")
             + (f";审批上限 {v['approval_ceiling']}(非当前额度)" if v["approval_ceiling"] is not None else "")
             for k, v in per_key.items()]
    if age_s is None or age_s < -60 or age_s > stale_after_s:
        node["state"] = "grey"
        lines.insert(0, f"⚠ 用量快照过旧或无时间戳(age={age_s}s),不作实况")
    elif paused:
        node["state"] = "red"
        lines.insert(0, f"🔴 refinery 因网关配额耗尽整窗停发(累计 {hits} 次)")
    elif worst >= 1.0:
        node["state"] = "red"
        lines.insert(0, "🔴 日上限已打满:后续请求 429,抽取全部失败")
    elif not limits or not primary or primary["ratio"] is None:
        node["state"] = "grey"
        lines.insert(0, "⚠ 未取得网关有效额度 effective_limits,无法判断余量;审批上限不能代替当前额度")
    elif worst >= GATEWAY_AMBER_RATIO:
        node["state"] = "amber"
        lines.insert(0, f"🟡 已用 {worst * 100:.0f}%,窗口尾可能打满")
    else:
        node["state"] = "green"
    node["detail"] = "\n".join(lines)
    edge["state"] = node["state"]
    return node, edge


def annotate_breakers(snap: dict, state: dict) -> None:
    """把断路器状态挂到它管的那个节点上。

    只挂 `breakers.KEY_NODES` 里列出的节点。挂不上（快照里没有那个节点）就算了：
    拓扑是按设备快照拼的，某台机器上没有 refinery 很正常，不该因此报错。
    """
    by_id = {n.get("id"): n for n in snap.get("nodes", []) if isinstance(n, dict)}
    for key, node_id in breakers.KEY_NODES.items():
        node = by_id.get(node_id)
        if node is None:
            continue
        entry = state["breakers"][key]
        node["breaker"] = {
            "key": key, "tripped": bool(entry["tripped"]),
            "reason": entry.get("reason") or "",
            "at": entry.get("at"), "by": entry.get("by"),
            # 损坏要在图上说出来。否则整条管线静悄悄停住,而看图的人只看到
            # 「已断开」,会以为是别人手动关的,去查半天。
            "corrupt": bool(state["corrupt"]),
            "enforced": bool(breakers.ENFORCED.get(key)),
            "lag_s": int(breakers.LAG_SECONDS.get(key, 0)),
        }
        if not breakers.ENFORCED.get(key):
            # 没有执行方就不许它改节点颜色:图上显示「已断开」而实际还在跑,
            # 是比没有开关更坏的谎。
            node["breaker"]["tripped"] = False
            continue
        if entry["tripped"]:
            # 断路是人为的,不是故障:用 amber 而不是 red,别和真出事的红混在一起。
            node["state"] = "amber"
            node["sub"] = "已人工断开" if not state["corrupt"] else "断路状态不可读"


def annotate_gateway(snap: dict, node: dict, edge: dict) -> dict:
    """只在含 kg-hub 节点的快照上挂网关节点(NAS 侧那一段只画一份)。"""
    nodes = snap.get("nodes")
    if not isinstance(nodes, list) or not any(
            isinstance(n, dict) and n.get("id") == "kghub" for n in nodes):
        return snap
    existing = next((n for n in nodes if isinstance(n, dict) and n.get("id") == "gateway"), None)
    if existing is None:
        nodes.append(dict(node))
    else:
        existing.update(node)
    edges = snap.setdefault("edges", [])
    if isinstance(edges, list):
        existing_edge = next((e for e in edges if e.get("from") == "kghub"
                              and e.get("to") == "gateway"), None)
        if existing_edge is None:
            edges.append(dict(edge))
        else:
            existing_edge.update(edge)
    rank = {"grey": 0, "green": 1, "amber": 2, "red": 3}
    snap["overall"] = max([snap.get("overall", "grey"), node["state"]],
                          key=lambda state: rank.get(state, 0))
    return snap


async def topology_report(request: Request) -> JSONResponse:
    """POST /api/topology/report — 接收探针快照。

    有界写：只 MERGE 一个 :TopologySnapshot{host}，payload 存 JSON 字符串。
    不进 Graphiti 抽取流程（这是运维遥测，不是知识）。
    """
    from kg_hub_server import get_status_driver  # 延迟 import 避免循环

    raw = await request.body()
    if len(raw) > MAX_SNAPSHOT_BYTES:
        return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
    try:
        snap = json.loads(raw)
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    host = str(snap.get("host") or "unknown")[:64]
    overall = str(snap.get("overall") or "grey")[:16]
    nodes = snap.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return JSONResponse({"ok": False, "error": "nodes[] required"}, status_code=400)

    try:
        driver = get_status_driver()
        await driver.execute_query(
            "MERGE (t:TopologySnapshot {host: $host}) "
            "SET t.payload = $payload, t.generated_at = $gen, "
            "    t.overall = $overall, t.received_at = $recv",
            host=host, payload=json.dumps(snap, ensure_ascii=False),
            gen=str(snap.get("generated_at") or ""), overall=overall,
            recv=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                            status_code=503)
    return JSONResponse({"ok": True, "host": host, "overall": overall,
                         "nodes": len(nodes)})


async def _load_snapshots(device_cfg: dict | None = None) -> list[dict]:
    """读出各设备最新快照，补上 _host/_recv/_age_s/_stale。

    面板(HTML)和 /api/topology/latest(JSON) 共用 —— 告警必须看**和人眼完全同一份**
    数据，否则会出现"面板红着但没告警"或反过来的裂缝。
    """
    from kg_hub_server import get_status_driver

    driver = get_status_driver()
    rows, _, _ = await driver.execute_query(
        "MATCH (t:TopologySnapshot) "
        "RETURN t.host AS host, t.payload AS payload, "
        "       t.generated_at AS gen, t.received_at AS recv "
        "ORDER BY t.received_at DESC")

    snaps = []
    device_cfg = device_cfg if isinstance(device_cfg, dict) else load_config()
    try:
        max_age_s = int(device_cfg.get(
            "device_liveness_max_age_sec", DEFAULT_MAX_AGE_S))
    except (TypeError, ValueError):
        max_age_s = DEFAULT_MAX_AGE_S
    liveness = load_status(
        device_cfg.get("device_liveness_path") or DEFAULT_PATH,
        max_age_s=max_age_s)
    aliases = device_cfg.get("capture_device_aliases")
    stale_after_s = capture_stale_after_s(device_cfg)
    now = datetime.now(tz=timezone.utc)
    # read_state 永不抛:读不动就返回「全部按断开」,这是花钱闸门唯一安全的假设。
    breaker_state = breakers.read_state()
    try:
        refinery_status = _read_json(REFINERY_STATUS_PATH) or {}
        gw_node, gw_edge = gateway_quota_node(
            _read_json(GATEWAY_USAGE_PATH), refinery_status, now=now)
        apply_gateway_health(gw_node, gw_edge, await gateway_health())
    except Exception:  # noqa: BLE001 — 配额格算不出来不能拖垮整张图
        gw_node = gw_edge = None
    for r in rows:
        try:
            snap = json.loads(r.get("payload") or "{}")
        except Exception:  # noqa: BLE001
            continue
        recv = r.get("recv") or ""
        age = None
        try:
            age = int((now - datetime.fromisoformat(recv)).total_seconds())
        except Exception:  # noqa: BLE001
            pass
        snap["_host"] = r.get("host") or "?"
        snap["_recv"] = recv
        snap["_age_s"] = age
        annotate_liveness(snap, liveness, aliases, stale_after_s=stale_after_s)
        annotate_breakers(snap, breaker_state)
        if gw_node is not None:
            annotate_gateway(snap, gw_node, gw_edge)
            activity = refinery_activity(refinery_status, now)
            for node in snap.get("nodes", []):
                if node.get("id") == "refinery" and node.get("state") != "red":
                    node["state"] = activity["state"]
                    node["sub"] = ("计划暂停" if refinery_status.get("idle_outside_window")
                                   and activity["state"] == "amber" else activity["label"])
                    node["idle_human"] = None
                    node["detail"] = (activity["label"] + "\n心跳："
                                      + str(refinery_status.get("heartbeat_at"))
                                      + "\n积压剩余：" + str(refinery_status.get("backlog_remaining")))
                    for edge in snap.get("edges", []):
                        if "refinery" in (edge.get("from"), edge.get("to")):
                            edge["state"] = node["state"]
        snaps.append(snap)
    return snaps


async def breakers_state(request: Request) -> JSONResponse:
    """GET /dashboard/breakers — 当前断路器状态（拓扑页与运维脚本共用）。"""
    return JSONResponse({"ok": True, **breakers.read_state()})


async def breakers_set(request: Request) -> JSONResponse:
    """POST /dashboard/breaker — 扳一个开关。

    与 `/dashboard/tag`、`/dashboard/capsule_requeue` 同一条既有的免鉴权面板写
    通道（17171 只绑 NAS 回环 + tailscale，不上局域网）。**这是控制面动作，不是
    只读**，信任边界就是 tailnet 本身——值得单独复核，但本次沿用既有先例，不在
    这里单独发明一套鉴权。
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "请求不是合法 JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "请求体必须是对象"}, status_code=400)
    key = payload.get("key")
    tripped = payload.get("tripped")
    if key not in breakers.KNOWN_KEYS:
        return JSONResponse({"ok": False, "error": "未知业务 key"}, status_code=400)
    if type(tripped) is not bool:
        return JSONResponse({"ok": False, "error": "tripped 必须是布尔"}, status_code=400)
    reason = payload.get("reason")
    reason = reason if isinstance(reason, str) else ""
    try:
        breakers.set_tripped(key, tripped, by="dashboard", reason=reason)
    except OSError as exc:
        # 写不进去必须如实说。谎报成功比不做更危险:操作员会以为已经断了。
        return JSONResponse(
            {"ok": False, "error": f"断路器状态写入失败：{type(exc).__name__}"},
            status_code=503)
    return JSONResponse({"ok": True, **breakers.read_state()})


async def dashboard_topology(request: Request) -> HTMLResponse:
    """GET /dashboard/topology — 渲染拓扑图。"""
    try:
        device_cfg = load_config()
        snaps = await _load_snapshots(device_cfg)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p>拓扑取数失败: {exc}</p>", status_code=503)
    data = {"snapshots": snaps, "layers": LAYERS,
            "stale_after_s": capture_stale_after_s(device_cfg)}
    return HTMLResponse(_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))


async def topology_latest(request: Request) -> JSONResponse:
    """GET /api/topology/latest — 快照 JSON，给 watchdog 告警用。"""
    try:
        device_cfg = load_config()
        snaps = await _load_snapshots(device_cfg)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    try:
        monitor = (await gateway_health()).get("monitor")
    except Exception:
        monitor = None  # Gateway monitor failure cannot erase capture evidence.
    return JSONResponse({"ok": True,
                         # Independent of snapshot count/device idle state; the
                         # cached GET cannot call a provider. A gateway 503 does
                         # not turn this capture endpoint into an error.
                         "gateway_monitor": monitor,
                         "stale_after_s": capture_stale_after_s(device_cfg),
                         "snapshots": snaps})


_HTML = r"""<!doctype html>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>采集链路拓扑 · kg-hub</title>
<style>
:root{
  --bg:#faf9f7; --fg:#1a1a19; --mut:#6b6a66; --line:#dedcd6; --card:#fff;
  --green:#2e9b5b; --amber:#d29922; --red:#e5534b; --grey:#a3a19b;
  --band:#efece6;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#16171a; --fg:#e8e6e1; --mut:#9a9892; --line:#2c2e33; --card:#1d1f23;
  --green:#3fb950; --amber:#d29922; --red:#f85149; --grey:#6e7681;
  --band:#23262c;
}}
*{box-sizing:border-box}
body{margin:0;padding:20px;background:var(--bg);color:var(--fg);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro SC","PingFang SC",system-ui,sans-serif}
h1{font-size:17px;margin:0 0 4px;font-weight:600}
.sub{color:var(--mut);font-size:12px;margin-bottom:18px}
.host{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:16px;margin-bottom:18px}
.hh{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.hh b{font-size:14px}
.pill{font-size:11px;padding:2px 9px;border-radius:20px;border:1px solid var(--line);
  color:var(--mut);white-space:nowrap}
.pill.green{color:#fff;background:var(--green);border-color:transparent}
.pill.amber{color:#211d10;background:var(--amber);border-color:transparent}
.pill.red{color:#fff;background:var(--red);border-color:transparent}
.pill.grey{color:#fff;background:var(--grey);border-color:transparent}
.host.disconnected .wrap{opacity:.42;filter:grayscale(1)}
.host.blind .wrap{opacity:.6}
.host.disconnected{border-style:dashed}
/* 自适应：宽屏用原尺寸，窄屏等比缩放，保证 kg-hub 那一列永远在首屏内 */
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
svg{display:block;width:100%;height:auto;max-width:1180px}
.lname{fill:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.nlabel{fill:var(--fg);font-size:11.5px;font-weight:500}
.nidle{fill:var(--mut);font-size:10.5px}
.box{fill:var(--card);stroke:var(--line)}
.box.green{stroke:var(--green)} .box.amber{stroke:var(--amber)}
.box.red{stroke:var(--red);stroke-width:2} .box.grey{stroke:var(--grey)}
.dot.green{fill:var(--green)} .dot.amber{fill:var(--amber)}
.dot.red{fill:var(--red)} .dot.grey{fill:var(--grey)}
.edge{stroke:var(--grey);stroke-width:1.5;fill:none;opacity:.5;stroke-linejoin:round}
.edge.faint{opacity:.2;stroke-width:1}
.edge.bypass{stroke-dasharray:2 4;opacity:.55}
.edge.green{stroke:var(--green);opacity:.75}
.edge.amber{stroke:var(--amber);opacity:.9;stroke-dasharray:5 3}
.edge.red{stroke:var(--red);opacity:1;stroke-width:2.5;stroke-dasharray:4 3}
g.n{cursor:pointer} g.n:hover .box{filter:brightness(1.06)}
/* 断路器开关。刻意做成"闸刀"而不是普通状态点:它是控制面,点下去会改变系统
   行为,必须一眼看出可点、且和旁边只读的状态圆点区分开。 */
g.brk{cursor:pointer}
g.brk:hover .brkbox{filter:brightness(1.25)}
.brkbox{stroke-width:1}
.brkbox.on{fill:color-mix(in srgb,var(--green) 22%,transparent);stroke:var(--green)}
.brkbox.off{fill:color-mix(in srgb,var(--red) 26%,transparent);stroke:var(--red)}
.brkbox.bad{fill:color-mix(in srgb,var(--amber) 26%,transparent);stroke:var(--amber)}
.brktext{font-size:9.5px;font-weight:600;text-anchor:middle;pointer-events:none}
.brktext.on{fill:var(--green)} .brktext.off{fill:var(--red)}
.brktext.bad{fill:var(--amber)}
.blockers{margin-top:12px;border-left:3px solid var(--red);padding:8px 12px;
  background:color-mix(in srgb,var(--red) 8%,transparent);border-radius:0 6px 6px 0}
.blockers div{font-size:12.5px;margin:3px 0}
.det{margin-top:10px;font-size:12px;color:var(--mut);border-top:1px solid var(--line);
  padding-top:10px;display:none;white-space:pre-wrap}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--mut);margin-top:6px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px}
.band{fill:var(--band)}
.bandsep{stroke:var(--line);stroke-width:1.2;opacity:1}
.bandtag{fill:var(--mut);font-size:10px;letter-spacing:.4px}
.empty{color:var(--mut);padding:30px;text-align:center;border:1px dashed var(--line);
  border-radius:10px}
.back{display:inline-block;margin:0 0 .5rem;font-size:13px;color:var(--mut);
  text-decoration:none}
.back:hover{text-decoration:underline}
.tabs{display:flex;gap:6px;margin:10px 0 14px}
.tab{border:1px solid var(--line);background:var(--card);color:var(--mut);
  border-radius:7px;padding:5px 12px;cursor:pointer;font:inherit;font-size:12px}
.tab.on{color:var(--fg);border-color:var(--green);
  background:color-mix(in srgb,var(--green) 10%,var(--card))}
.hookpanel{display:none}
.hookhost{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:14px;margin-bottom:16px}
.hookgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:12px}
.toolcard{border:1px solid var(--line);border-radius:8px;overflow:hidden}
.toolhead{display:flex;align-items:center;gap:8px;padding:9px 12px;background:var(--band)}
.toolhead b{flex:1}.tooldiff{font-size:10.5px;color:var(--mut);padding:0 12px 8px;background:var(--band)}
.hrow{display:grid;grid-template-columns:88px minmax(130px,1fr) minmax(105px,.7fr);
  gap:8px;padding:9px 12px;border-top:1px solid var(--line);font-size:11.5px}
.hrow .event{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--mut)}
.hrow .meta{color:var(--mut);font-size:10.5px;margin-top:3px}.hstatus{text-align:right}
.hrow.red{border-left:3px solid var(--red)}.hrow.green{border-left:3px solid var(--green)}
.hrow.grey{border-left:3px solid var(--grey)}.hrow.amber{border-left:3px solid var(--amber)}
.srcs{font-size:10.5px;color:var(--mut);padding:7px 12px;border-top:1px solid var(--line)}
.srcs summary{cursor:pointer}.srcs div{margin-top:5px;line-height:1.5}
.statusdot{width:8px;height:8px;border-radius:50%;display:inline-block}
.statusdot.green{background:var(--green)}.statusdot.amber{background:var(--amber)}
.statusdot.red{background:var(--red)}.statusdot.grey{background:var(--grey)}
@media(max-width:700px){.hrow{grid-template-columns:82px 1fr}.hstatus{grid-column:1/-1;text-align:left}}
</style>
<a class=back href="/portal">← 报表门户</a>
<h1>采集链路拓扑</h1>
<div class=sub>工具 → hook → claude-mem → SQLite → 传输 → NAS 副本 → refinery → kg-hub → FalkorDB ｜ 每 60s 自动刷新 ｜ 点节点看详情</div>
<div class=tabs>
  <button id=tab-topology class="tab on" onclick="switchView('topology')">链路拓扑</button>
  <button id=tab-hooks class=tab onclick="switchView('hooks')">Hook 面板</button>
</div>
<div class=legend>
  <span><i style="background:var(--green)"></i>正常</span>
  <span><i style="background:var(--amber)"></i>空闲/滞后（非故障）</span>
  <span><i style="background:var(--red)"></i>故障 / 健康异常（详见节点）</span>
  <span><i style="background:var(--grey)"></i>未配置</span>
  <span>虚线 = 该跳有滞后或中断</span>
  <span>点线 = 跨层直连（如 OpenClaw 不走 claude-mem，绕行走最近空闲通道）</span>
</div>
<svg width=0 height=0 style="position:absolute"><defs>
<marker id=ag viewBox="0 0 8 8" refX=6 refY=4 markerWidth=5 markerHeight=5 orient=auto>
  <path d="M0 1 L6 4 L0 7" fill=none stroke="var(--green)" stroke-width=1.4/></marker>
<marker id=aa viewBox="0 0 8 8" refX=6 refY=4 markerWidth=5 markerHeight=5 orient=auto>
  <path d="M0 1 L6 4 L0 7" fill=none stroke="var(--amber)" stroke-width=1.4/></marker>
<marker id=ar viewBox="0 0 8 8" refX=6 refY=4 markerWidth=5 markerHeight=5 orient=auto>
  <path d="M0 1 L6 4 L0 7" fill=none stroke="var(--red)" stroke-width=1.6/></marker>
<marker id=ax viewBox="0 0 8 8" refX=6 refY=4 markerWidth=5 markerHeight=5 orient=auto>
  <path d="M0 1 L6 4 L0 7" fill=none stroke="var(--grey)" stroke-width=1.2/></marker>
</defs></svg>
<div id=root></div>
<div id=hooks class=hookpanel></div>
<script>
const D = __DATA__;
// LH=行距 BW/BH=节点框（BW 要容得下最长标签 "claude-mem worker"）。
// 列间距**不再是常量** —— 见 renderHost 里的 gapW：按实际穿过的连线数算。
// 等宽间隙是上一版的病根：设备→工具要过 9 条线、worker→存储只过 1 条，
// 给同样的 46px，前者被压成每条 5px 的一团麻。
const LH = 86, LH_C = 60, PADT = 34, BW = 104, BH = 42;   // LH_C: 无工具的行
const GAP_BASE = 52, GAP_LANE = 17;   // 间隙宽 = BASE + 过线数 * LANE

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function render(){
  const root = document.getElementById('root');
  if(!D.snapshots || !D.snapshots.length){
    root.innerHTML = '<div class=empty>还没有任何探针上报。<br>'
      + '在 Mac 上跑：<code>python3 tools/capture_probe.py --report</code></div>';
    return;
  }
  root.innerHTML = D.snapshots.map(renderHost).join('');
  root.querySelectorAll('g.n').forEach(g=>{
    g.onclick = ()=>{
      const d = document.getElementById(g.dataset.det);
      if(d) d.style.display = d.style.display==='block' ? 'none' : 'block';
    };
  });
  renderHooks();
  switchView(localStorage.getItem('kg_topology_view')||'topology');
}

function switchView(view){
  const hooks = view==='hooks';
  document.getElementById('root').style.display = hooks ? 'none' : 'block';
  document.getElementById('hooks').style.display = hooks ? 'block' : 'none';
  document.querySelector('.legend').style.display = hooks ? 'none' : 'flex';
  document.getElementById('tab-topology').classList.toggle('on',!hooks);
  document.getElementById('tab-hooks').classList.toggle('on',hooks);
  localStorage.setItem('kg_topology_view', hooks?'hooks':'topology');
}

function evidenceText(e){
  if(!e||e.kind==='none') return '未验证';
  const age = e.age_s!=null ? `${fmt(e.age_s)}前` : '已见';
  return e.kind==='hook-log' ? `日志 ${age}` : `下游 ${age}`;
}

function renderHooks(){
  const box = document.getElementById('hooks');
  box.innerHTML = (D.snapshots||[]).map((s,hi)=>{
    const inv = s.hook_inventory||[];
    if(!inv.length) return `<div class=hookhost><b>${esc(s._host)}</b><div class=empty>该探针版本尚未上报 hook_inventory</div></div>`;
    const cards = inv.map(t=>{
      const sum=t.summary||{};
      const rows=(t.hooks||[]).map(h=>{
        const status=!h.configured?(h.state==='red'?'缺失':'未配置'):
          h.approval==='missing'?'未批准':h.state==='amber'?'范围受限':'已配置';
        return `<div class="hrow ${esc(h.state||'grey')}">
          <div><span class="statusdot ${esc(h.state||'grey')}"></span>
            <span class=event>${esc(h.event||'—')}</span>
            ${h.matcher?`<div class=meta>${esc(h.matcher)}</div>`:''}</div>
          <div><b>${esc(h.label||h.component)}</b>
            <div class=meta>${esc(h.purpose||h.action||'—')}</div></div>
          <div class=hstatus><b>${esc(status)}</b>
            <div class=meta>${esc(h.scope||'—')} · ${esc(evidenceText(h.runtime_evidence))}</div>
            ${h.coverage?`<div class=meta>⚠ ${esc(h.coverage)}</div>`:''}
          </div></div>`;
      }).join('');
      const src=(t.sources||[]).map(x=>`${x.found?'✓':'—'} ${x.scope||''} ${x.path}`).join(' · ');
      const found=(t.sources||[]).filter(x=>x.found).length, total=(t.sources||[]).length;
      const summary=sum.total?`${sum.configured||0}/${sum.total} 已配`:'不适用';
      return `<section class=toolcard>
        <div class=toolhead><span class="statusdot ${esc(t.state)}"></span><b>${esc(t.label)}</b>
          <span class=pill>${summary}${sum.unapproved?` · ${sum.unapproved} 未批准`:''}${sum.limited_scope?` · ${sum.limited_scope} 受限`:''}</span></div>
        ${t.difference?`<div class=tooldiff>${esc(t.difference)}</div>`:''}
        ${rows||'<div class=empty>无本机 IDE Hook</div>'}
        ${total?`<details class=srcs><summary>配置源 ${found}/${total}</summary><div>${esc(src)}</div></details>`:
          '<div class=srcs>无本机配置源</div>'}
      </section>`;
    }).join('');
    return `<div class=hookhost><div class=hh><b>${esc(s._host)}</b>
      <span class=pill>注册表 v1 · 配置/批准/执行证据分开判定</span></div>
      <div class=hookgrid>${cards}</div></div>`;
  }).join('')||'<div class=empty>还没有 hook 快照</div>';
}

function renderHost(s, hi){
  // 按层分桶
  const cols = D.layers.map(([key,label])=>({
    key, label, nodes:(s.nodes||[]).filter(n=>n.layer===key)
  })).filter(c=>c.nodes.length);
  const nodeById = {}; (s.nodes||[]).forEach(n=>nodeById[n.id]=n);
  const ciOf = {}; cols.forEach((c,i)=>ciOf[c.key]=i);
  const devCol = cols.find(c=>c.key==='device'), toolCol = cols.find(c=>c.key==='tool');

  // ---- 按设备分组成横向 band ----
  // 工具按所属设备聚簇，band 内相邻 == 装在同一台机器上。
  // 归属关系一旦由「相邻 + 底色分带」表达，设备→工具那 8 条连线就**不用画了**
  // —— 这比把它们摊开更彻底：设备→工具间隙从 9 条降到 1 条。
  const owner = {};
  (s.edges||[]).forEach(e=>{
    if (e.from.startsWith('dev:') && nodeById[e.to] && nodeById[e.to].layer==='tool')
      owner[e.to] = e.from;
  });
  const bands = [];
  let rcur = 0;
  if (devCol && toolCol) {
    // 有工具的设备排前面 → 工具列连续成块，没有采集工具的设备（NAS/手机/离线机）
    // 收在底部，它们的空带不会把工具列切断
    const ordered = devCol.nodes.slice().sort((a,b)=>
      (toolCol.nodes.some(t=>owner[t.id]===a.id) ? 0 : 1)
      - (toolCol.nodes.some(t=>owner[t.id]===b.id) ? 0 : 1));
    ordered.forEach(dn=>{
      const tools = toolCol.nodes.filter(t=>owner[t.id]===dn.id);
      const rows = Math.max(tools.length, 1);
      bands.push({dev:dn, tools, r0:rcur, rows});
      rcur += rows;
    });
    const orphans = toolCol.nodes.filter(t=>!owner[t.id]);
    if (orphans.length){ bands.push({dev:null, tools:orphans, r0:rcur, rows:orphans.length});
                         rcur += orphans.length; }
  }

  // ---- 行号分配 ----
  const row = {};
  bands.forEach(b=>{
    if (b.dev) row[b.dev.id] = b.r0;                       // 设备框对齐它这一带的首行
    b.tools.forEach((t,k)=>{ row[t.id] = b.r0 + k; });
  });
  // 工具之后各列：行 = 前驱行的均值 → 节点贴近来源，连线更短更直，
  // hook 自然落在对应工具同一行（tool→hook 变成一条水平直线）
  const orphan = [];
  const preds = {}, succs = {};
  (s.edges||[]).forEach(e=>{ (preds[e.to] = preds[e.to]||[]).push(e.from);
                             (succs[e.from] = succs[e.from]||[]).push(e.to); });
  cols.forEach(c=>{
    if (c.key==='device' || c.key==='tool') return;
    const want = c.nodes.map(n=>{
      const ps = (preds[n.id]||[]).filter(x=>row[x]!=null);
      if (ps.length) return {n, r: ps.reduce((a,x)=>a+row[x],0)/ps.length};
      orphan.push({n, col:c});     // 没有前驱 → 留到补算 pass（见下）
      return {n, r: 0};
    }).sort((a,b)=>a.r-b.r);
    let last = -Infinity;                                  // 同列去重叠：至少隔 1 行
    want.forEach(w=>{ const r = Math.max(w.r, last+1); row[w.n.id] = r; last = r; });
  });

  // 补算 pass：没有前驱的节点（如 ingester —— 它是独立的 canonical 文档线，
  // 不读 claude-mem）改用**后继**行定位。必须放在主循环之后：它的后继
  // (kg-hub) 在更右边的列，主循环按列从左往右走时那一行还没算出来，
  // 于是只能退回 0，节点被甩到图顶孤零零飘着。
  orphan.forEach(({n, col})=>{
    const ss = (succs[n.id]||[]).filter(x=>row[x]!=null);
    if (!ss.length) return;
    let r = ss.reduce((a,x)=>a+row[x],0)/ss.length;
    const taken = col.nodes.filter(m=>m.id!==n.id).map(m=>row[m.id]).sort((a,b)=>a-b);
    while (taken.some(t=>Math.abs(t-r) < 1)) r += 1;       // 躲开同列已占的行
    row[n.id] = r;
  });
  cols.forEach(c=>c.nodes.forEach((n,ri)=>{ if(row[n.id]==null) row[n.id] = ri; }));
  const nRows = Math.ceil(Math.max(...Object.values(row))) + 1;
  const toolRows = new Set();
  bands.forEach(b=>b.tools.forEach((t,k)=>toolRows.add(b.r0+k)));
  const rowH = Array.from({length:nRows}, (_,r)=>toolRows.has(r) ? LH : LH_C);
  const rowTop = [0];
  for (let r=0; r<nRows; r++) rowTop[r+1] = rowTop[r] + rowH[r];
  const yOf = r=>{ const f = Math.max(0, Math.min(nRows-1, Math.floor(r)));
                   return PADT + rowTop[f] + (r-f)*rowH[f]; };
  const maxRows = nRows;

  const ci_ = {};
  cols.forEach((c,ci)=>c.nodes.forEach(n=>{ ci_[n.id] = {ci, ri: row[n.id]}; }));
  // 设备→工具的边不再画（归属已由分带表达）
  // 设备边一律不画。它表达的是「这东西跑在哪台机器上」——归属，不是数据流。
  // 工具的归属由分带的相邻性表达；NAS 侧那些(副本/refinery/ingester/kg-hub/
  // FalkorDB)则写进各自节点的详情里。
  //
  // 之前只滤掉了 dev→工具，留下的 dev:home-nas→kg-hub 从设备列一路拉到
  // 最右边,实测单条横段 1454px,几乎横贯整张图,而它只说了"kg-hub 在 NAS 上"。
  const el = (s.edges||[]).filter(e=>ci_[e.from]&&ci_[e.to])
    .filter(e=>!e.from.startsWith('dev:'));

  // ---- 正交路由：避免交叉与重叠的四个手段 ----
  // ① 端口分散：一个节点的多条边在边缘均匀分点，不挤同一点
  // ② 按对端 y 排序后分配端口：同一束线保持相对顺序 → 不交叉（平面图技巧）
  // ③ 间隙按需定宽 + 全局通道分配：每条线在它穿过的间隙里独占一条垂直通道
  // ④ 跨列边走底部通道，逐条错开 y → 不斜穿中间列、不互相压

  // ③-a 统计每个列间隙要过多少条线
  const nGap = Math.max(cols.length - 1, 0);
  const slots = Array.from({length: nGap}, ()=>[]);
  el.forEach(e=>{
    const a = ci_[e.from], b = ci_[e.to];
    if (b.ci - a.ci > 1) {           // 跨列：下行占 a 右侧间隙，上行占 b 左侧间隙
      if (slots[a.ci])   slots[a.ci].push({e, kind:'down'});
      if (slots[b.ci-1]) slots[b.ci-1].push({e, kind:'up'});
    } else if (b.ci - a.ci === 1) {
      slots[a.ci].push({e, kind:'mid'});
    }
  });
  // ③-b 同间隙内按「源行→目标行」排序再发通道号 → 同束线不互相穿越
  slots.forEach(g=>g.sort((p,q)=>
    (ci_[p.e.from].ri - ci_[q.e.from].ri) || (ci_[p.e.to].ri - ci_[q.e.to].ri)));
  const gapW = slots.map(g=>GAP_BASE + g.length*GAP_LANE);
  const lane = {};
  const lkey = (e,k)=>e.from+'>'+e.to+'|'+k;
  slots.forEach((g,gi)=>g.forEach((it,i)=>{ lane[lkey(it.e,it.kind)] = {i, n:g.length, gi}; }));

  // 列 x 由累积间隙决定（不再是 ci*LW）
  const colX = []; let ax = 20;
  cols.forEach((c,ci)=>{ colX[ci] = ax; ax += BW + (gapW[ci]||0); });

  const crossEdges = el.filter(e=>ci_[e.to].ci - ci_[e.from].ci > 1);
  const BUS = 26;   // 只给「全被占满」的兜底通道留一点余量
  const W = ax + 4;
  const H = PADT + rowTop[nRows] + BUS;

  const pos = {};
  cols.forEach((c,ci)=>c.nodes.forEach(n=>{
    const ri = row[n.id], y = yOf(ri);
    pos[n.id] = {ci, ri, x: colX[ci], y, cx: colX[ci]+BW/2, cy: y+BH/2};
  }));

  // ── 跨列边的横向通道 ──────────────────────────────────────────────
  // 原来所有跨列边一律绕到**图最底部**的总线再折回来。图现在有 10 层、
  // 上千像素高，于是几条绕行线并排纵贯整张图，在大片空白里重叠成一团
  // （用户 2026-08-24 截图指出）。
  //
  // 改为走「离两端最近的空闲行间通道」：每两行之间的空隙中线都是候选，
  // 选一条中间列在该高度确实没有节点挡路、且没被别的跨列边占用的。
  // 绝大多数跨列边因此只需要短短一段绕行。
  const chan = [];
  for (let r = 0; r < nRows - 1; r++) chan.push((yOf(r) + BH + yOf(r+1)) / 2);
  chan.push(PADT + rowTop[nRows] + 10);        // 兜底：仍保留图底那条
  const chanUsed = {};
  const pickChan = (aci, bci, y1, y2)=>{
    const mid = (y1 + y2) / 2;
    const order = chan.map((y,i)=>({y,i})).sort((p,q)=>Math.abs(p.y-mid)-Math.abs(q.y-mid));
    for (const c of order) {
      if (chanUsed[c.i]) continue;
      // 挡路判定看**所有列**而不只是中间列:通道横穿时会从各列旁边掠过,
      // 贴着任一节点的高度走都容易和该节点的端口短桩挤在一起。
      // (注:这条并没有消掉最后那 2 处 4-7px 的接近 —— 那是两条边**汇聚到
      //  同一节点**时各自入口短桩的间距,属节点-连线图的固有现象,
      //  与通道选取无关。留着这条是因为"通道别贴着节点走"本身是对的。)
      const blocked = cols.some(col=> col.nodes.some(n=>{
        const q = pos[n.id]; return c.y > q.y - 9 && c.y < q.y + BH + 9; }));
      if (!blocked) { chanUsed[c.i] = 1; return c.y; }
    }
    return chan[chan.length-1];                // 全被占满 → 退回图底
  };

  const outs = {}, ins = {};
  el.forEach(e=>{ (outs[e.from] = outs[e.from]||[]).push(e);
                  (ins[e.to]   = ins[e.to]  ||[]).push(e); });
  Object.values(outs).forEach(a=>a.sort((p,q)=>pos[p.to].cy - pos[q.to].cy));
  Object.values(ins ).forEach(a=>a.sort((p,q)=>pos[p.from].cy - pos[q.from].cy));

  // 通道号 → 真实 x：在该间隙里均匀分布
  const laneX = (e,k)=>{
    const L = lane[lkey(e,k)];
    if (!L) return pos[e.from].x + BW + 20;
    return colX[L.gi] + BW + gapW[L.gi]*(L.i+1)/(L.n+1);
  };

  const R = 5;   // 折角圆角
  const edges = el.map(e=>{
    const a = pos[e.from], b = pos[e.to];
    const oi = outs[e.from].indexOf(e), on = outs[e.from].length;
    const ii = ins[e.to].indexOf(e),   iN = ins[e.to].length;
    const y1 = a.y + BH*(oi+1)/(on+1);          // ① 出端口
    const y2 = b.y + BH*(ii+1)/(iN+1);          // ① 入端口
    const x1 = a.x + BW, x2 = b.x;
    let d;
    // 跨列但**同高**且中间列在这个高度没有节点挡路 → 直接一条水平直线。
    // 上一版只按"列距 > 1"就无条件送去底部绕行，于是 OpenClaw(工具) → OpenClaw(传输)
    // 这种同一行的边也绕到图底再拐回来，白跑一大圈。
    const clearStraight = (b.ci - a.ci > 1) && Math.abs(y1-y2) < 1.5 && !cols.some((c,cc)=>
      cc > a.ci && cc < b.ci && c.nodes.some(n=>{
        const q = pos[n.id]; return y1 > q.y - 4 && y1 < q.y + BH + 4; }));
    if (b.ci - a.ci > 1 && !clearStraight) {
      // ④ 跨列 → 下行/上行各走自己的垂直通道，横穿一条独占的空闲行间通道
      const xd = laneX(e,'down'), xu = laneX(e,'up');
      const yb = pickChan(a.ci, b.ci, y1, y2);
      // 通道可能在两端的上方也可能在下方，折角方向必须跟着算，
      // 否则圆角会朝反方向拐出一个小勾。
      const s1 = yb > y1 ? 1 : -1, s3 = yb > y2 ? 1 : -1;
      d = `M${x1} ${y1} L${xd-R} ${y1} Q${xd} ${y1} ${xd} ${y1+R*s1}`
        + ` L${xd} ${yb-R*s1} Q${xd} ${yb} ${xd+R} ${yb}`
        + ` L${xu-R} ${yb} Q${xu} ${yb} ${xu} ${yb-R*s3}`
        + ` L${xu} ${y2+R*s3} Q${xu} ${y2} ${xu+R} ${y2} L${x2} ${y2}`;
    } else if (Math.abs(y1-y2) < 1.5) {
      d = `M${x1} ${y1} L${x2} ${y2}`;                       // 同高 → 直线
    } else {
      // ③ 每条线在本间隙独占一条垂直通道，互不重合
      const mx = laneX(e,'mid');
      const s2 = y2 > y1 ? 1 : -1;
      d = `M${x1} ${y1} L${mx-R} ${y1} Q${mx} ${y1} ${mx} ${y1+R*s2}`
        + ` L${mx} ${y2-R*s2} Q${mx} ${y2} ${mx+R} ${y2} L${x2} ${y2}`;
    }
    // 设备→工具的边信息量低（只表达"装在这台机上"），画淡避免抢视线
    const faint = e.from.startsWith('dev:') ? ' faint' : (b.ci-a.ci>1 ? ' bypass' : '');
    const mk = faint ? "" : ` marker-end="url(#${({green:"ag",amber:"aa",red:"ar"})[e.state]||"ax"})"`;
    return `<path class="edge ${esc(e.state)}${faint}" d="${d}"${mk}/>`;
  }).join('');

  // 设备 → 本带工具：带内树形托架（一条竖脊 + 每个工具一根短横杆）
  //
  // 为什么托架不会重新制造上一版那种线团：**各带在垂直方向互不重叠**，
  // 所以所有带的竖脊可以共用同一个 x，一条通道就够，不像原来 9 条线各要一条。
  // 单工具的带退化成一条直线。
  const SPINE = 16;
  const bracket = bands.filter(b=>b.dev && b.tools.length && pos[b.dev.id]).map(b=>{
    const dp = pos[b.dev.id];
    const xs = dp.x + BW + SPINE;
    const tp = b.tools.map(t=>pos[t.id]).filter(Boolean);
    if (!tp.length) return '';
    const st = (s.nodes||[]).find(n=>n.id===b.dev.id) || {};
    const cls = `edge ${esc(st.state||'grey')} faint`;
    if (tp.length === 1)
      return `<path class="${cls}" d="M${dp.x+BW} ${dp.cy} L${tp[0].x} ${tp[0].cy}"/>`;
    const ys = tp.map(q=>q.cy);
    return `<path class="${cls}" d="M${dp.x+BW} ${dp.cy} L${xs} ${dp.cy}"/>`
      + `<path class="${cls}" d="M${xs} ${Math.min(...ys)} L${xs} ${Math.max(...ys)}"/>`
      + tp.map(q=>`<path class="${cls}" d="M${xs} ${q.cy} L${q.x} ${q.cy}"/>`).join('');
  }).join('');

  // band 底色 + 分隔线：先画，垫在连线和节点下面
  const bandSvg = bands.map((b,k)=>{
    const y0 = PADT + rowTop[b.r0] - (rowH[b.r0]-BH)/2;
    const h  = rowTop[Math.min(b.r0+b.rows, nRows)] - rowTop[b.r0];
    const fill = (k % 2) ? `<rect class=band x="6" y="${y0}" width="${W-12}" height="${h}" rx="6"/>` : '';
    const sep  = k ? `<line class=bandsep x1="6" y1="${y0}" x2="${W-6}" y2="${y0}"/>` : '';
    return fill + sep;
  }).join('');

  const heads = cols.map((c,ci)=>
    `<text class=lname x="${colX[ci]}" y="18">${esc(c.label)}</text>`).join('');

  let dets = [];
  const boxes = cols.map(c=>c.nodes.map(n=>{
    const p = pos[n.id];
    const did = `d-${hi}-${n.id.replace(/[^a-z0-9]/gi,'')}`;
    dets.push(`<div class=det id="${did}"><b>${esc(n.label)}</b>  [${esc(n.state)}]\n`
      + `${esc(n.detail||'')}\n`
      + (n.metrics ? esc(JSON.stringify(n.metrics)) : '') + `</div>`);
    const idle = n.idle_human ? `空闲 ${esc(n.idle_human)}` : (n.sub ? esc(n.sub) : '');
    return `<g class=n data-det="${did}">`
      + `<title>${esc(n.detail||n.label)}</title>`
      + `<rect class="box ${esc(n.state)}" x="${p.x}" y="${p.y}" width="${BW}" height="${BH}" rx="8"/>`
      + `<circle class="dot ${esc(n.state)}" cx="${p.x+13}" cy="${p.y+15}" r="4.5"/>`
      + `<text class=nlabel x="${p.x+24}" y="${p.y+19}">${esc(n.label)}</text>`
      + `<text class=nidle x="${p.x+24}" y="${p.y+34}">${idle}</text>`
      + `</g>` + breakerSwitch(n, p);
  }).join('')).join('');

  const ds = s._device_state||'unknown';
  const device = ds==='online'
    ? `<span class="pill green" title="${esc(s._device_liveness_detail||'')}">设备在线</span>`
    : ds==='offline'
      ? `<span class="pill grey" title="${esc(s._device_liveness_detail||'')}">设备离线/睡眠 · 链路断开</span>`
      : `<span class="pill grey" title="${esc(s._device_liveness_detail||'')}">设备状态未知</span>`;
  const stale = s._stale ? `<span class="pill red">探针失联 ${fmt(s._age_s)}</span>` : '';
  const blind = s._snapshot_stale && ds==='unknown'
    ? `<span class="pill grey">旧快照 · 暂不告警</span>` : '';
  const age = s._age_s!=null ? `<span class=pill>上报于 ${fmt(s._age_s)}前</span>` : '';
  // 旧快照里的 blocker 是历史，不继续画红；新鲜快照的 blocker 与设备态无关，仍展示。
  const blockers = (!s._snapshot_stale&&s.blockers&&s.blockers.length)
    ? `<div class=blockers>${s.blockers.map(b=>
        `<div>🔴 <b>${esc(b.label)}</b> — ${esc(b.detail)}</div>`).join('')}</div>` : '';
  const overall = s._snapshot_stale && ds!=='online' ? 'grey' : (s.overall||'grey');

  return `<div class="host ${s._disconnected?'disconnected':''} ${s._snapshot_stale&&ds==='unknown'?'blind':''}">
    <div class=hh><b>${esc(s._host)}</b>
      <span class="pill ${esc(overall)}">${esc(overall)}</span>${device}${age}${stale}${blind}</div>
    <div class=wrap><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">
      ${bandSvg}${heads}${bracket}${edges}${boxes}
    </svg></div>
    ${blockers}${dets.join('')}
  </div>`;
}

function fmt(sec){
  if(sec==null) return '—';
  if(sec<90) return sec+'秒'; if(sec<5400) return Math.floor(sec/60)+'分钟';
  if(sec<172800) return Math.floor(sec/3600)+'小时'; return Math.floor(sec/86400)+'天';
}

// ---- 人工断路器 ----------------------------------------------------------
// 采集链路上只有两个节点会调用模型(claude-mem / refinery)。这两个开关是在
// 「一边跑一边烧钱、却只能干看着」时唯一能立刻按下去的东西 —— 2026-09-10 那次
// 六小时攒了 171 条**永远清不掉**的悬账记录,当时没有任何手段单独切断一路。
//
// 关掉 = 源头停止提交(不是让请求撞墙报错):不产生请求,也不产生错误、不扣重试、
// 不留错误键。开关一开,原样接着跑。
function breakerSwitch(n, p){
  const b = n.breaker;
  if (!b) return '';
  // 没有执行方的开关不画成可按的样子。假开关比没开关更危险。
  const cls = !b.enforced ? 'bad'
    : (b.corrupt ? 'bad' : (b.tripped ? 'off' : 'on'));
  const text = !b.enforced ? '未接线'
    : (b.corrupt ? '不可读' : (b.tripped ? '已断开' : '通'));
  // 生效延迟必须说出来:操作员按下之后要知道该等多久,不能以为是瞬时的。
  const lag = b.lag_s > 0 ? `\n生效延迟最多 ${b.lag_s} 秒（经守护脚本同步）` : '';
  const w = 46, h = 16, x = p.x + BW - w - 6, y = p.y + BH - h - 5;
  const tip = !b.enforced
    ? '这一路还没有执行方：开关存得下但不会生效，先别指望它（见 T-0066）'
    : b.corrupt
    ? '断路器状态读不动，已按断开处理；点此重写一份干净状态'
    : (b.tripped
        ? `已人工断开${b.reason?'：'+b.reason:''}${b.at?'\n'+b.at:''}${b.by?' by '+b.by:''}${lag}\n点此恢复`
        : '切断后：源头停止取数，队列原样保留，恢复后从断点接着跑' + lag + '\n点此切断');
  return `<g class=brk data-key="${esc(b.key)}" data-tripped="${b.tripped?1:0}"`
    + ` data-enforced="${b.enforced?1:0}" data-lag="${b.lag_s|0}"`
    + ` data-label="${esc(n.label)}">`
    + `<title>${esc(tip)}</title>`
    + `<rect class="brkbox ${cls}" x="${x}" y="${y}" width="${w}" height="${h}" rx="8"/>`
    + `<text class="brktext ${cls}" x="${x+w/2}" y="${y+11.5}">${esc(text)}</text>`
    + `</g>`;
}

document.addEventListener('click', async ev => {
  const g = ev.target.closest && ev.target.closest('g.brk');
  if (!g) return;
  if (g.dataset.enforced !== '1') {
    ev.stopPropagation();
    alert('这一路还没有执行方，扳了也不会生效。见 T-0066。');
    return;
  }
  ev.stopPropagation();          // 别顺手把节点详情也展开
  const key = g.dataset.key, label = g.dataset.label;
  const wasTripped = g.dataset.tripped === '1';
  let reason = '';
  if (!wasTripped) {
    // 切断要写一句为什么。事后回看「谁在什么时候为什么把它关了」全靠这一行。
    const lagNote = (g.dataset.lag|0) > 0
      ? `\n注意：这一路生效最多要等 ${g.dataset.lag} 秒。` : '';
    reason = prompt(`切断【${label}】的模型调用？\n`
      + `队列原样保留、不丢数据，恢复后从断点接着跑。${lagNote}\n\n原因：`, '');
    if (reason === null) return;
  } else if (!confirm(`恢复【${label}】的模型调用？`)) {
    return;
  }
  try {
    const r = await fetch('/dashboard/breaker', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({key, tripped: !wasTripped, reason})});
    const d = await r.json();
    if (!d.ok) { alert('操作失败：' + (d.error||r.status)); return; }
  } catch (e) { alert('操作失败：' + e); return; }
  location.reload();
});

render();
setTimeout(()=>location.reload(), 60000);
</script>
"""
