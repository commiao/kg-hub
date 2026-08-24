"""
monitor_topology — watchdog 存活监控拓扑（面板 + 状态 API）。

和 topology.py（采集链路 push 模型）不同：这里的状态由 **kg_hub_server 端实时探测**
算出，不依赖任何探针上报或状态文件是否新鲜 —— 打开页面看到的就是当下真实。

被监控的设备/服务 = 节点（着色）；几个 watchdog = 监控边（按目标状态着色）：
    VPS check.sh (L2)      → kg-hub@NAS / openclaw@本机
    NAS nas-probe (反向)   → openclaw@VPS(公网)
    NAS watchdog (L1)      → kg_hub_server / falkordb（容器内网）
    Mac MCP (L3)           → kg-hub@NAS（用时触发）

暴露两个 route（在 kg_hub_server.py 接线）：
    GET /dashboard/monitor         → 拓扑图页面（每 15s 轮询 JSON 动态刷新）
    GET /dashboard/monitor/status  → 实时状态 JSON（/dashboard/* 免鉴权，浏览器可直接 fetch）

单独成文件：kg_hub_server.py 已 3900+ 行，新功能独立模块零冲突合并。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

# openclaw@VPS 公网健康（nas-probe 走的同一地址；server 容器有出网可达）
OPENCLAW_VPS_URL = os.environ.get(
    "KG_HUB_MON_OPENCLAW_URL", "http://47.253.216.216:18789/health")

FALKOR_SLOW_S = 1.5          # falkordb 探测 > 此值 → amber（慢）
QUEUE_AMBER = 5             # pending > 此值 → amber
QUEUE_RED = 50             # pending > 此值 → red
MAC_FRESH_S = 2 * 3600      # Mac 摄入 < 2h → green
MAC_STALE_S = 24 * 3600      # < 24h → amber，否则 grey（Mac 本就间歇，不算故障）

_RANK = {"red": 3, "amber": 2, "green": 1, "grey": 0}


def _worst(states: list[str]) -> str:
    """一组状态取最差（grey 不拉低到故障，只在全 grey 时为 grey）。"""
    real = [s for s in states if s in _RANK]
    if not real:
        return "grey"
    return max(real, key=lambda s: _RANK[s])


async def _probe_falkordb() -> dict:
    from kg_hub_server import get_status_driver
    t0 = time.monotonic()
    try:
        driver = get_status_driver()
        await driver.execute_query("RETURN 1")
        dt = time.monotonic() - t0
        state = "green" if dt <= FALKOR_SLOW_S else "amber"
        return {"state": state, "detail": f"查询 {dt*1000:.0f}ms"
                + ("（偏慢）" if state == "amber" else "")}
    except Exception as exc:  # noqa: BLE001
        return {"state": "red", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}


async def _probe_queue() -> dict:
    from kg_hub_server import get_status_driver
    try:
        driver = get_status_driver()
        rows, _, _ = await driver.execute_query(
            "MATCH (k:IngestedKey) RETURN k.status AS status")
        pending = sum(1 for r in rows if (r.get("status") or "") == "pending")
        errored = sum(1 for r in rows if (r.get("status") or "") == "error")
        if pending > QUEUE_RED:
            state = "red"
        elif pending > QUEUE_AMBER or errored > 0:
            state = "amber"
        else:
            state = "green"
        return {"state": state, "detail": f"pending={pending} error={errored}"}
    except Exception as exc:  # noqa: BLE001
        return {"state": "red", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}


async def _probe_openclaw() -> dict:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0)) as c:
            r = await c.get(OPENCLAW_VPS_URL)
        if r.status_code < 400:
            return {"state": "green", "detail": f"HTTP {r.status_code}（公网可达）"}
        return {"state": "red", "detail": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"state": "red", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}


async def _probe_mac() -> dict:
    """Mac claude-mem 摄入新鲜度 = 图中最新一条 claude-mem-obs-* 的账龄。

    准确信号:Mac 的 launchd `com.kg-hub.claude-mem-ingest` 直连 falkordb:6379
    写 `claude-mem-obs-{id}` episode(NAS ingester 的 claude-mem 步已退役,所以
    这类节点如今只由 Mac 产生)。max(created_at)=Mac 最近一次成功入图的时刻。
    """
    from kg_hub_server import get_status_driver
    try:
        driver = get_status_driver()
        rows, _, _ = await driver.execute_query(
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'claude-mem-obs-' "
            "RETURN max(n.created_at) AS newest")
        newest = rows[0].get("newest") if rows else None
    except Exception as exc:  # noqa: BLE001
        return {"state": "grey", "detail": f"查询失败: {type(exc).__name__}"}
    if not newest:
        return {"state": "grey", "detail": "图中暂无 claude-mem 记录"}
    try:
        dt = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - dt).total_seconds()
    except Exception:  # noqa: BLE001
        return {"state": "grey", "detail": "时间解析失败"}
    if age < MAC_FRESH_S:
        state = "green"
    elif age < MAC_STALE_S:
        state = "amber"
    else:
        state = "grey"  # Mac 间歇在线，久未摄入不算故障
    return {"state": state, "detail": f"最近入图 {_fmt_age(age)}前"}


def _fmt_age(sec: float) -> str:
    sec = int(sec)
    if sec < 90:
        return f"{sec}秒"
    if sec < 5400:
        return f"{sec // 60}分钟"
    if sec < 172800:
        return f"{sec // 3600}小时"
    return f"{sec // 86400}天"


async def _collect() -> dict:
    fk, q, ocl, mac = await asyncio.gather(
        _probe_falkordb(), _probe_queue(), _probe_openclaw(), _probe_mac())
    kg = {"state": "green", "detail": "响应中（自身）"}  # 能服务这次请求即 up

    nodes = {
        "nas.kg_server": kg,
        "nas.falkordb": fk,
        "nas.queue": q,
        "vps.openclaw": ocl,
        "mac.client": mac,
    }
    hosts = {
        "nas": _worst([kg["state"], fk["state"], q["state"]]),
        "vps": ocl["state"],
        "mac": mac["state"],
    }
    # 监控边：按其目标当前状态着色（该 watchdog 此刻"看到"的颜色）
    monitors = {
        "l2_kghub": kg["state"],       # VPS check.sh → kg-hub@NAS
        "l2_ocl": ocl["state"],        # VPS check.sh → openclaw@本机
        "probe": ocl["state"],         # NAS nas-probe → openclaw@VPS(公网)
        "l1": _worst([kg["state"], fk["state"]]),  # NAS watchdog(内网)
        "l3": kg["state"],             # Mac MCP → kg-hub@NAS
    }
    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "nodes": nodes, "hosts": hosts, "monitors": monitors,
    }


async def monitor_status(request: Request) -> JSONResponse:
    """GET /dashboard/monitor/status — 实时探测状态 JSON。"""
    try:
        return JSONResponse({"ok": True, **(await _collect())})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                            status_code=503)


async def dashboard_monitor(request: Request) -> HTMLResponse:
    """GET /dashboard/monitor — 监控拓扑页面（前端轮询 status 动态刷新）。"""
    return HTMLResponse(_HTML)


_HTML = r"""<!doctype html>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>监控拓扑 · kg-hub</title>
<style>
:root{
  --bg:#faf9f7; --fg:#1a1a19; --mut:#6b6a66; --line:#dedcd6; --card:#fff;
  --green:#2e9b5b; --amber:#d29922; --red:#e5534b; --grey:#a3a19b;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#16171a; --fg:#e8e6e1; --mut:#9a9892; --line:#2c2e33; --card:#1d1f23;
  --green:#3fb950; --amber:#d29922; --red:#f85149; --grey:#6e7681;
}}
*{box-sizing:border-box}
body{margin:0;padding:20px;background:var(--bg);color:var(--fg);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro SC","PingFang SC",system-ui,sans-serif}
h1{font-size:17px;margin:0 0 4px;font-weight:600}
.sub{color:var(--mut);font-size:12px;margin-bottom:12px}
.back{display:inline-block;margin:0 0 .5rem;font-size:13px;color:var(--mut);text-decoration:none}
.back:hover{text-decoration:underline}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--mut);margin:6px 0 14px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px;vertical-align:middle}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
svg{display:block;width:100%;height:auto;max-width:980px;margin:auto}
.hostcard{fill:var(--card);stroke:var(--line);stroke-width:1.5}
.htitle{fill:var(--fg);font-size:13px;font-weight:600}
.hsub{fill:var(--mut);font-size:10.5px}
.svc{fill:var(--card);stroke:var(--line)}
.svc.green{stroke:var(--green)} .svc.amber{stroke:var(--amber)}
.svc.red{stroke:var(--red);stroke-width:2} .svc.grey{stroke:var(--grey)}
.slabel{fill:var(--fg);font-size:12px;font-weight:500}
.sdetail{fill:var(--mut);font-size:10px}
.dot.green{fill:var(--green)} .dot.amber{fill:var(--amber)}
.dot.red{fill:var(--red)} .dot.grey{fill:var(--grey)}
.edge{fill:none;stroke-width:2}
.edge.green{stroke:var(--green);opacity:.7}
.edge.amber{stroke:var(--amber);opacity:.9;stroke-dasharray:6 3}
.edge.red{stroke:var(--red);opacity:1;stroke-width:2.6;stroke-dasharray:5 3}
.edge.grey{stroke:var(--grey);opacity:.5;stroke-dasharray:2 4}
.elabel{fill:var(--mut);font-size:10px}
.pill{font-size:11px;font-weight:600}
.pill.green{fill:var(--green)} .pill.amber{fill:var(--amber)}
.pill.red{fill:var(--red)} .pill.grey{fill:var(--grey)}
#bar{font-size:12px;color:var(--mut);margin-top:10px}
#err{color:var(--red);font-size:12.5px;margin-top:8px;display:none}
</style>
<a class=back href="/portal">← 报表门户</a>
<h1>监控拓扑 · 设备/服务存活</h1>
<div class=sub>被监控对象 = 节点（灯色=实时探测）｜watchdog = 监控边（按目标状态着色）｜每 15s 自动刷新</div>
<div class=legend>
  <span><i style="background:var(--green)"></i>正常</span>
  <span><i style="background:var(--amber)"></i>降级/偏慢/滞后</span>
  <span><i style="background:var(--red)"></i>故障/不可达</span>
  <span><i style="background:var(--grey)"></i>间歇/未知（非故障）</span>
</div>
<div class=wrap><svg viewBox="0 0 960 400">
  <defs>
    <marker id=mg viewBox="0 0 10 10" refX=8 refY=5 markerWidth=7 markerHeight=7 orient=auto>
      <path d="M0 1 L8 5 L0 9" fill=none stroke="var(--green)" stroke-width=1.6/></marker>
    <marker id=ma viewBox="0 0 10 10" refX=8 refY=5 markerWidth=7 markerHeight=7 orient=auto>
      <path d="M0 1 L8 5 L0 9" fill=none stroke="var(--amber)" stroke-width=1.6/></marker>
    <marker id=mr viewBox="0 0 10 10" refX=8 refY=5 markerWidth=7 markerHeight=7 orient=auto>
      <path d="M0 1 L8 5 L0 9" fill=none stroke="var(--red)" stroke-width=1.8/></marker>
    <marker id=mx viewBox="0 0 10 10" refX=8 refY=5 markerWidth=7 markerHeight=7 orient=auto>
      <path d="M0 1 L8 5 L0 9" fill=none stroke="var(--grey)" stroke-width=1.4/></marker>
  </defs>

  <!-- ===== 监控边（画在卡片下层）===== -->
  <!-- L2 check.sh: VPS → kg-hub@NAS（上行）-->
  <path id="e-l2_kghub" class="edge grey" d="M260 232 C310 200 320 165 360 158"/>
  <text class=elabel x="266" y="188">check.sh · L2</text>
  <!-- nas-probe: NAS → openclaw@VPS 公网（下行，反向）-->
  <path id="e-probe" class="edge grey" d="M360 300 C320 330 310 300 262 268"/>
  <text class=elabel x="266" y="312">nas-probe · 反向/公网</text>
  <!-- L3 MCP: Mac → kg-hub@NAS -->
  <path id="e-l3" class="edge grey" d="M700 236 C650 200 640 168 600 160"/>
  <text class=elabel x="612" y="192">MCP · L3 用时</text>

  <!-- ===== VPS 卡 ===== -->
  <rect class=hostcard x="40" y="150" width="220" height="150" rx="12"/>
  <text class=htitle x="58" y="176">VPS · oc-vps</text>
  <text class=hsub x="58" y="192">常开 · 探针+openclaw</text>
  <text id="pill-vps" class="pill grey" x="240" y="176" text-anchor="end">—</text>
  <g>
    <rect id="svc-vps.openclaw" class="svc grey" x="58" y="212" width="184" height="46" rx="8"/>
    <circle id="dot-vps.openclaw" class="dot grey" cx="74" cy="230" r="5"/>
    <text class=slabel x="88" y="230">openclaw :18789</text>
    <text id="det-vps.openclaw" class=sdetail x="88" y="247">…</text>
  </g>

  <!-- ===== NAS 卡（中心）===== -->
  <rect class=hostcard x="360" y="80" width="240" height="285" rx="12"/>
  <text class=htitle x="380" y="108">NAS · home-nas-syno</text>
  <text class=hsub x="380" y="124">常开中心 · falkordb+server</text>
  <text id="pill-nas" class="pill grey" x="580" y="108" text-anchor="end">—</text>
  <g>
    <rect id="svc-nas.kg_server" class="svc grey" x="380" y="140" width="200" height="48" rx="8"/>
    <circle id="dot-nas.kg_server" class="dot grey" cx="398" cy="160" r="5"/>
    <text class=slabel x="412" y="160">kg_hub_server :17171</text>
    <text id="det-nas.kg_server" class=sdetail x="412" y="178">…</text>
  </g>
  <g>
    <rect id="svc-nas.falkordb" class="svc grey" x="380" y="200" width="200" height="48" rx="8"/>
    <circle id="dot-nas.falkordb" class="dot grey" cx="398" cy="220" r="5"/>
    <text class=slabel x="412" y="220">FalkorDB</text>
    <text id="det-nas.falkordb" class=sdetail x="412" y="238">…</text>
  </g>
  <g>
    <rect id="svc-nas.queue" class="svc grey" x="380" y="260" width="200" height="48" rx="8"/>
    <circle id="dot-nas.queue" class="dot grey" cx="398" cy="280" r="5"/>
    <text class=slabel x="412" y="280">摄入队列 (ingester)</text>
    <text id="det-nas.queue" class=sdetail x="412" y="298">…</text>
  </g>
  <text class=hsub x="380" y="336">watchdog · L1 内网（↑ 盯 server/falkordb/队列）</text>

  <!-- ===== Mac 卡 ===== -->
  <rect class=hostcard x="700" y="150" width="220" height="150" rx="12"/>
  <text class=htitle x="718" y="176">Mac · mac-office</text>
  <text class=hsub x="718" y="192">客户端 · 间歇在线</text>
  <text id="pill-mac" class="pill grey" x="900" y="176" text-anchor="end">—</text>
  <g>
    <rect id="svc-mac.client" class="svc grey" x="718" y="212" width="184" height="46" rx="8"/>
    <circle id="dot-mac.client" class="dot grey" cx="734" cy="230" r="5"/>
    <text class=slabel x="748" y="230">claude-mem 摄入</text>
    <text id="det-mac.client" class=sdetail x="748" y="247">…</text>
  </g>
</svg></div>
<div id=bar>加载中…</div>
<div id=err></div>

<script>
const NODES = ["nas.kg_server","nas.falkordb","nas.queue","vps.openclaw","mac.client"];
const HOSTS = ["nas","vps","mac"];
const EDGES = ["l2_kghub","l2_ocl","probe","l1","l3"];
const MK = {green:"mg",amber:"ma",red:"mr",grey:"mx"};

function setClass(el, base, state){ if(el) el.setAttribute("class", base+" "+state); }

async function tick(){
  const errEl = document.getElementById("err");
  try{
    const r = await fetch("/dashboard/monitor/status", {cache:"no-store"});
    const d = await r.json();
    if(!d.ok) throw new Error(d.error||"status not ok");
    NODES.forEach(id=>{
      const n = d.nodes[id] || {state:"grey",detail:""};
      setClass(document.getElementById("svc-"+id), "svc", n.state);
      setClass(document.getElementById("dot-"+id), "dot", n.state);
      const det = document.getElementById("det-"+id); if(det) det.textContent = n.detail||"";
    });
    HOSTS.forEach(h=>{
      const st = d.hosts[h] || "grey";
      const p = document.getElementById("pill-"+h);
      if(p){ setClass(p,"pill",st); p.textContent = st.toUpperCase(); }
    });
    EDGES.forEach(e=>{
      const st = d.monitors[e] || "grey";
      const el = document.getElementById("e-"+e);
      if(el){ setClass(el,"edge",st); el.setAttribute("marker-end","url(#"+MK[st]+")"); }
    });
    errEl.style.display = "none";
    document.getElementById("bar").textContent =
      "更新于 " + new Date().toLocaleTimeString() + " ｜ 探测时刻 " + (d.generated_at||"");
  }catch(e){
    errEl.style.display = "block";
    errEl.textContent = "取状态失败：" + e.message + "（下一轮重试）";
  }
}
tick();
setInterval(tick, 15000);
</script>
"""
