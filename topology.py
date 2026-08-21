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
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

# 链路分层：从左到右就是数据流向
LAYERS = [
    ("device", "设备"),
    ("tool", "工具"),
    ("hook", "hook"),
    ("worker", "worker"),
    ("storage", "存储"),
    ("transport", "传输"),
    ("kghub", "kg-hub"),
    ("graph", "图谱"),
]

MAX_SNAPSHOT_BYTES = 256 * 1024   # 单份快照上限，防误传大 payload
STALE_AFTER_S = 30 * 60           # 快照本身超过 30 分钟未更新 → 探针可能挂了


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


async def dashboard_topology(request: Request) -> HTMLResponse:
    """GET /dashboard/topology — 渲染拓扑图。"""
    from kg_hub_server import get_status_driver

    try:
        driver = get_status_driver()
        rows, _, _ = await driver.execute_query(
            "MATCH (t:TopologySnapshot) "
            "RETURN t.host AS host, t.payload AS payload, "
            "       t.generated_at AS gen, t.received_at AS recv "
            "ORDER BY t.received_at DESC")
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p>拓扑取数失败: {exc}</p>", status_code=503)

    snaps = []
    now = datetime.now(tz=timezone.utc)
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
        # 探针自己失联也要能看出来：快照过期就整体降级
        snap["_stale"] = bool(age is not None and age > STALE_AFTER_S)
        snaps.append(snap)

    data = {"snapshots": snaps, "layers": LAYERS, "stale_after_s": STALE_AFTER_S}
    return HTMLResponse(_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))


_HTML = r"""<!doctype html>
<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>采集链路拓扑 · kg-hub</title>
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
.blockers{margin-top:12px;border-left:3px solid var(--red);padding:8px 12px;
  background:color-mix(in srgb,var(--red) 8%,transparent);border-radius:0 6px 6px 0}
.blockers div{font-size:12.5px;margin:3px 0}
.det{margin-top:10px;font-size:12px;color:var(--mut);border-top:1px solid var(--line);
  padding-top:10px;display:none;white-space:pre-wrap}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--mut);margin-top:6px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px}
.empty{color:var(--mut);padding:30px;text-align:center;border:1px dashed var(--line);
  border-radius:10px}
</style>
<h1>采集链路拓扑</h1>
<div class=sub>工具 → hook → claude-mem → SQLite → 传输 → kg-hub ｜ 每 60s 自动刷新 ｜ 点节点看详情</div>
<div class=legend>
  <span><i style="background:var(--green)"></i>正常</span>
  <span><i style="background:var(--amber)"></i>空闲/滞后（非故障）</span>
  <span><i style="background:var(--red)"></i>阻塞或故障</span>
  <span><i style="background:var(--grey)"></i>未配置</span>
  <span>虚线 = 该跳有滞后或中断</span>
  <span>底部点线 = 跨层直连（如 OpenClaw 不走 claude-mem）</span>
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
<script>
const D = __DATA__;
// LW=列间距 LH=行距 BW/BH=节点框。BW 要容得下最长标签（"claude-mem worker"）
const LW = 150, LH = 60, PADT = 34, BW = 104, BH = 42;

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
}

function renderHost(s, hi){
  // 按层分桶
  const cols = D.layers.map(([key,label])=>({
    key, label, nodes:(s.nodes||[]).filter(n=>n.layer===key)
  })).filter(c=>c.nodes.length);
  const maxRows = Math.max(...cols.map(c=>c.nodes.length), 1);
  const W = cols.length*LW + 24;
  const BUS = 26;                       // 底部绕行通道高度，给跨列边走
  const H = PADT + maxRows*LH + BUS;

  const pos = {};
  cols.forEach((c,ci)=>c.nodes.forEach((n,ri)=>{
    pos[n.id] = {ci, ri, x: 20+ci*LW, y: PADT+ri*LH,
                 cx: 20+ci*LW+BW/2, cy: PADT+ri*LH+BH/2};
  }));

  // ---- 正交路由：避免交叉与重叠的三个手段 ----
  // ① 端口分散：一个节点的多条边在边缘上均匀分点，不再挤同一点
  // ② 按对端 y 排序后再分配端口：同一束线保持相对顺序 → 不交叉（平面图技巧）
  // ③ 汇流轴按边序错开 + 跨列边走底部通道 → 垂直段不重叠、不穿节点
  const el = (s.edges||[]).filter(e=>pos[e.from]&&pos[e.to]);
  const outs = {}, ins = {};
  el.forEach(e=>{ (outs[e.from] = outs[e.from]||[]).push(e);
                  (ins[e.to]   = ins[e.to]  ||[]).push(e); });
  Object.values(outs).forEach(a=>a.sort((p,q)=>pos[p.to].cy - pos[q.to].cy));
  Object.values(ins ).forEach(a=>a.sort((p,q)=>pos[p.from].cy - pos[q.from].cy));

  const R = 5;   // 折角圆角
  const edges = el.map((e,ei)=>{
    const a = pos[e.from], b = pos[e.to];
    const oi = outs[e.from].indexOf(e), on = outs[e.from].length;
    const ii = ins[e.to].indexOf(e),   iN = ins[e.to].length;
    const y1 = a.y + BH*(oi+1)/(on+1);          // ① 出端口
    const y2 = b.y + BH*(ii+1)/(iN+1);          // ① 入端口
    const x1 = a.x + BW, x2 = b.x;
    let d;
    if (b.ci - a.ci > 1) {
      // ③ 跨列 → 走底部通道，绝不斜穿中间列的节点
      const yb = PADT + maxRows*LH + 8 + (ei % 2) * 8;
      d = `M${x1} ${y1} L${x1+10} ${y1} L${x1+10} ${yb-R} Q${x1+10} ${yb} ${x1+10+R} ${yb}`
        + ` L${x2-16-R} ${yb} Q${x2-16} ${yb} ${x2-16} ${yb-R} L${x2-16} ${y2+R}`
        + ` Q${x2-16} ${y2} ${x2-16+R} ${y2} L${x2} ${y2}`;
    } else if (Math.abs(y1-y2) < 1.5) {
      d = `M${x1} ${y1} L${x2} ${y2}`;                       // 同高 → 直线
    } else {
      // ③ 汇流轴错开：多对一汇聚时按**入边序**错开（多条边共用一个目标，
      //    各自 on=1 用出边序会全部重合），一对多时按出边序错开。
      const spread = iN > 1 ? ii/(iN-1) : (on > 1 ? oi/(on-1) : 0.5);
      const mx = x1 + (x2-x1)*(0.28 + 0.44*spread);
      const s2 = y2 > y1 ? 1 : -1;
      d = `M${x1} ${y1} L${mx-R} ${y1} Q${mx} ${y1} ${mx} ${y1+R*s2}`
        + ` L${mx} ${y2-R*s2} Q${mx} ${y2} ${mx+R} ${y2} L${x2} ${y2}`;
    }
    // 设备→工具的边信息量低（只表达"装在这台机上"），画淡避免抢视线
    const faint = e.from.startsWith('dev:') ? ' faint' : (b.ci-a.ci>1 ? ' bypass' : '');
    const mk = faint ? "" : ` marker-end="url(#${({green:"ag",amber:"aa",red:"ar"})[e.state]||"ax"})"`;
    return `<path class="edge ${esc(e.state)}${faint}" d="${d}"${mk}/>`;
  }).join('');

  const heads = cols.map((c,ci)=>
    `<text class=lname x="${20+ci*LW}" y="18">${esc(c.label)}</text>`).join('');

  let dets = [];
  const boxes = cols.map(c=>c.nodes.map(n=>{
    const p = pos[n.id];
    const did = `d-${hi}-${n.id.replace(/[^a-z0-9]/gi,'')}`;
    dets.push(`<div class=det id="${did}"><b>${esc(n.label)}</b>  [${esc(n.state)}]\n`
      + `${esc(n.detail||'')}\n`
      + (n.metrics ? esc(JSON.stringify(n.metrics)) : '') + `</div>`);
    const idle = n.idle_human ? `空闲 ${esc(n.idle_human)}` : '';
    return `<g class=n data-det="${did}">`
      + `<title>${esc(n.detail||n.label)}</title>`
      + `<rect class="box ${esc(n.state)}" x="${p.x}" y="${p.y}" width="${BW}" height="${BH}" rx="8"/>`
      + `<circle class="dot ${esc(n.state)}" cx="${p.x+13}" cy="${p.y+15}" r="4.5"/>`
      + `<text class=nlabel x="${p.x+24}" y="${p.y+19}">${esc(n.label)}</text>`
      + `<text class=nidle x="${p.x+24}" y="${p.y+34}">${idle}</text>`
      + `</g>`;
  }).join('')).join('');

  const stale = s._stale ? `<span class="pill red">探针失联 ${fmt(s._age_s)}</span>` : '';
  const age = s._age_s!=null ? `<span class=pill>上报于 ${fmt(s._age_s)}前</span>` : '';
  const blockers = (s.blockers&&s.blockers.length)
    ? `<div class=blockers>${s.blockers.map(b=>
        `<div>🔴 <b>${esc(b.label)}</b> — ${esc(b.detail)}</div>`).join('')}</div>` : '';

  return `<div class=host>
    <div class=hh><b>${esc(s._host)}</b>
      <span class="pill ${esc(s.overall)}">${esc(s.overall)}</span>${age}${stale}</div>
    <div class=wrap><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">
      ${heads}${edges}${boxes}
    </svg></div>
    ${blockers}${dets.join('')}
  </div>`;
}

function fmt(sec){
  if(sec==null) return '—';
  if(sec<90) return sec+'秒'; if(sec<5400) return Math.floor(sec/60)+'分钟';
  if(sec<172800) return Math.floor(sec/3600)+'小时'; return Math.floor(sec/86400)+'天';
}

render();
setTimeout(()=>location.reload(), 60000);
</script>
"""
