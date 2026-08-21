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


async def _load_snapshots() -> list[dict]:
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
    return snaps


async def dashboard_topology(request: Request) -> HTMLResponse:
    """GET /dashboard/topology — 渲染拓扑图。"""
    try:
        snaps = await _load_snapshots()
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(f"<p>拓扑取数失败: {exc}</p>", status_code=503)
    data = {"snapshots": snaps, "layers": LAYERS, "stale_after_s": STALE_AFTER_S}
    return HTMLResponse(_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))


async def topology_latest(request: Request) -> JSONResponse:
    """GET /api/topology/latest — 快照 JSON，给 watchdog 告警用。"""
    try:
        snaps = await _load_snapshots()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return JSONResponse({"ok": True, "stale_after_s": STALE_AFTER_S,
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
.band{fill:var(--band)}
.bandsep{stroke:var(--line);stroke-width:1.2;opacity:1}
.bandtag{fill:var(--mut);font-size:10px;letter-spacing:.4px}
.empty{color:var(--mut);padding:30px;text-align:center;border:1px dashed var(--line);
  border-radius:10px}
.back{display:inline-block;margin:0 0 .5rem;font-size:13px;color:var(--mut);
  text-decoration:none}
.back:hover{text-decoration:underline}
</style>
<a class=back href="/portal">← 报表门户</a>
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
  const preds = {};
  (s.edges||[]).forEach(e=>{ (preds[e.to] = preds[e.to]||[]).push(e.from); });
  cols.forEach(c=>{
    if (c.key==='device' || c.key==='tool') return;
    const want = c.nodes.map(n=>{
      const ps = (preds[n.id]||[]).filter(x=>row[x]!=null);
      return {n, r: ps.length ? ps.reduce((a,x)=>a+row[x],0)/ps.length : 0};
    }).sort((a,b)=>a.r-b.r);
    let last = -Infinity;                                  // 同列去重叠：至少隔 1 行
    want.forEach(w=>{ const r = Math.max(w.r, last+1); row[w.n.id] = r; last = r; });
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
  const el = (s.edges||[]).filter(e=>ci_[e.from]&&ci_[e.to])
    .filter(e=>!(e.from.startsWith('dev:') && nodeById[e.to] && nodeById[e.to].layer==='tool'));

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
  const BUS = 14 + crossEdges.length*7;     // 底部绕行通道，每条跨列边一层
  const W = ax + 4;
  const H = PADT + rowTop[nRows] + BUS;

  const pos = {};
  cols.forEach((c,ci)=>c.nodes.forEach(n=>{
    const ri = row[n.id], y = yOf(ri);
    pos[n.id] = {ci, ri, x: colX[ci], y, cx: colX[ci]+BW/2, cy: y+BH/2};
  }));

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
      // ④ 跨列 → 下行/上行各走自己的通道，底部横穿一条独占的 y
      const xd = laneX(e,'down'), xu = laneX(e,'up');
      const yb = PADT + rowTop[nRows] + 8 + crossEdges.indexOf(e)*7;
      d = `M${x1} ${y1} L${xd-R} ${y1} Q${xd} ${y1} ${xd} ${y1+R}`
        + ` L${xd} ${yb-R} Q${xd} ${yb} ${xd+R} ${yb}`
        + ` L${xu-R} ${yb} Q${xu} ${yb} ${xu} ${yb-R}`
        + ` L${xu} ${y2+R} Q${xu} ${y2} ${xu+R} ${y2} L${x2} ${y2}`;
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

render();
setTimeout(()=>location.reload(), 60000);
</script>
"""
