# Phase 3 完成报告 — 打通孤岛

> **日期**：2026-05-18 → 2026-05-19（实际开干约 6 小时）
> **目标**：让 Cursor / Codex / Claude Code **写** KG，让 OpenClaw **读** KG，让其它工具按文档**自助接入**。
> **结果**：全部达成 + 异步重构 + 主动监控 + 跨 AI 共享盲点诊断（计划外副产品）。

---

## 一句话总结

> 5/14 立项的"个人级跨工具知识图谱"项目，**5/19 闭环**：
> Cursor 能写、OpenClaw 能读、其它工具能照文档自助接入、所有写入异步不阻塞、watchdog 主动告警、跨 AI 信息共享有铁证。

---

## 数据规模演化

| 时间 | entities | edges | episodes | 备注 |
|---|---|---|---|---|
| 5/14 Phase 1 起点 | — | — | — | 立项 |
| 5/15 Phase 1 完成 | 237 | 245 | 31 | Kuzu + 31 个 OpenClaw 胶囊 |
| 5/17 Phase 2 完成 | ~470 | ~2150 | ~115 | 切 FalkorDB + 多源 ingest + writer.lock |
| **5/18 Phase 3 起点** | **1893** | **4279** | **881** | 全量 backfill 后 |
| **5/19 Phase 3 完成** | **2014** | **4821** | **990** | **+121 / +542 / +109** |

---

## 路线图变更（5/18 凌晨拍板）

**原 Phase 3**（5/17 草案）：NAS 部署 → OpenClaw push hook → 多 Mac 验证。

**新 Phase 3**（user 重新拍板）：
> "NAS 不急，**真阻塞是 Cursor 不能写 + OpenClaw 不能读**。"

| 旧排序 | 新排序 |
|---|---|
| 3.1 NAS 部署 | 3.A FastAPI server（基础） |
| 3.2 OpenClaw push hook | 3.B `kg_add_episode` MCP 写工具 + Cursor 验证 |
| 3.3 sync_openclaw fallback | 3.C OpenClaw kg-query skill + 飞书验证 |
| 3.4 多 Mac | 3.D ONBOARDING.md |
| 3.5 kg_add_episode | 3.E NAS（**推迟**） |
| 3.6 验收 | 3.F push plugin（**推迟**） |
| — | 3.G 多 Mac（**推迟**） |

→ "真痛点优先"的重排, 关键阻塞消除最先做。

---

## 5 步执行轨迹

### Step 1: 文档锁决策（30 min）

- 重写 ROADMAP Phase 3
- DESIGN.md 加 决策 14/15/16

**决策 14**：所有写入走统一 `/api/ingest` HTTP 端点 + 固定 JSON schema
- `(source_description, source_obs_id)` 做幂等键
- MCP 写工具是 HTTP 的"轻量壳"，不直连 graphiti
- 让任意源（Cursor/OpenClaw/未来 webhook）按同一协议接入

**决策 15**：Bearer token 鉴权 + Tailscale 网段限制 + OpenClaw 两阶段接入
- Token 持久化 `~/.claude-mem/.env`
- OpenClaw 第一阶段只做 read（skill kg-query），write 留到 Phase 3.F

**决策 16**：手动写入路径分流，**Path A 临时 + Path Z 未来**
- Path A: `kg_add_episode` → kg-hub 直写（**现在用**）
- Path Z: `observation_add` → claude-mem.db → ingester → kg-hub（**理想态**）
- 切换条件：claude-mem 升 `server-beta` 模式（worker 模式 MCP 不暴露写工具——实测撞墙后明确了这个 trade-off）

### Step 2: kg_hub_server.py（FastAPI）+ 异步重构（1.5h）

#### 第一版（同步）
- 5 路由：`/health` `/api/ingest` `/api/search` `/api/ingest/status` `/api/queue_stats`
- Bearer auth middleware
- 服务端幂等：`MERGE (k:IngestedKey {sd, sid})` 原子 check-and-create
- 部署为 plist `com.kg-hub.server`（KeepAlive Crashed=true / ThrottleInterval=30s）

#### 实测发现"慢"（user push）
端到端测量：
- 短内容写入：**2.7s**（LLM 觉得没啥可抽，0 nodes/edges）
- 真实长内容：**196s**（LLM 抽 15 nodes / 21 edges）

慢点定位：
```
HTTP/auth/idempotency 检查    <50ms
writer.lock 获取               瞬时（无竞争）
graphiti.add_episode():
  ├─ LLM extract entities      ~30s
  ├─ LLM dedup entities         ~30s   ┐
  ├─ LLM extract edges          ~30s   │ 5 次串行 LLM call
  ├─ LLM classify edges         ~30s   │ ≈ 150-200s
  └─ LLM extract attributes     ~30s   ┘
```

→ MCP / FastAPI / 网络 < 100ms。**慢全在 LLM 上**。

#### 异步重构

**架构**：
- `POST /api/ingest` 默认异步：原子 MERGE IngestedKey → `asyncio.create_task(do_extract)` → 返 202
- `IngestedKey.status` 三态机：`pending` → `ok` / `error`
- `GET /api/ingest/status` 支持 `(sd, sid)` 或 `episode_uuid` 查询
- `GET /api/queue_stats` 暴露 pending / ok / error 计数 + 最老 pending 年龄
- **每次新 ingest piggyback cleanup**：删超过 30 min 还 pending 的孤儿（无单独 cron 任务）
- `?sync=true` 兼容老调用方

**踩坑**：
1. `writer_lock` 用 `time.sleep()` **阻塞事件循环** → 202 没真异步，仍要 182s。
   → 加 `async_writer_lock` 用 `asyncio.sleep()` 让 event loop 喘气。
2. `graphiti.add_episode(uuid=X)` 不是"创建时指定 UUID"——它是"更新已有 episode"，抛 NodeNotFoundError。
   → 弃用 pre-assign UUID，改用 `(sd, sid)` 做追踪键，让 graphiti 生成 episode_uuid，后台完成时回填。

**最终**：
```
POST /api/ingest → 1.89s 返 202 + poll_url
后台 LLM 抽取 ~30-200s, 完成后 IngestedKey status='ok' + episode_uuid 填入
```

### Step 3: kg_add_episode MCP 写工具 + Cursor 验证（40 min）

`mcp_server.py` 新增 `kg_add_episode(content, source_description, source_obs_id?, name?, reference_time?)`：
- httpx.AsyncClient(**timeout=240.0**) ←  > server lock-wait 180s，避免 ReadTimeout 误导
- 内部 POST 到 `/api/ingest`，复用同一鉴权 + 幂等管线
- 错误结构化：`server_unreachable` / `request_failed` / `missing_token`

**实战验证**：
- 用户重启 Cursor → MCP 列表多出 `kg_add_episode`
- 实际写入：episode `761cc789-762d-46ed-9a01-0bc5a0e75d58`
- 同 source_obs_id 重试：返 `{status:skipped, reason:duplicate}` —— **幂等机制工作**

### Step 4: OpenClaw kg-query skill + 飞书验证（30 min）

部署到 VPS `oc-vps-aliyun-us`：
- `clawd/skills/kg-query/SKILL.md`（YAML frontmatter + 调用规则 + 错误兜底）
- `clawd/scripts/kg-query.sh`（curl wrapper + env 检查 + 错误码分级）
- `~/.openclaw/env.sh` 加 `KG_HUB_URL=http://mac-office:8080` + `KG_HUB_TOKEN`（mode 600）

**实战验证**——用户在飞书问小迪两个问题，server.out.log 两条铁证：
```
INFO: 100.79.177.102:56784 GET /api/search?q=Cron 通知失败           200 OK
INFO: 100.79.177.102:52382 GET /api/search?q=sync_openclaw Mac rsync 200 OK
              ↑↑↑↑↑↑↑↑↑↑↑↑
              这是 OpenClaw VPS 的 Tailscale IP，
              本机会显示 127.0.0.1。
```
小迪**自主决定**调 kg-query 并把结果纳入回答——**跨设备读路径完整闭环**。

合成数据时序错位：小迪说"Phase 2 rsync 冷备份"——这是 GraphRAG 典型小毛病（多 fact 合成时编新关系），核心答案正确（用 tar+ssh 不用 rsync）。

### Step 5a: Watchdog 主动监控（45 min）

`tools/watchdog.py` + plist `com.kg-hub.watchdog`（每 10 min）：

**4 个 anomaly**：
- `server_down` — `/health` 不可达
- `queue_backlog` — pending > 5
- `stuck_jobs` — 有任务 pending > 30 min
- `recent_errors` — 上一小时 error 数 > 0

**边沿触发**：
- 状态文件 `~/.kg-hub/state/watchdog.json`
- OK→BAD：发 "fire" 告警
- BAD→OK：发 "clear" 告警
- BAD→BAD：**静默**（不刷屏）

**输出**（按优先级）：
- 飞书 webhook（如 env `KG_HUB_FEISHU_WEBHOOK` 设了）
- macOS 通知中心 osascript（兜底）
- `~/.kg-hub/logs/alerts.log`（永久审计）

**合成测试通过**：手动伪造"全 BAD" prev state → 实际 OK → 4 个 CLEAR 告警；再跑 OK→OK → 0 新告警。

### Step 5b: docs/ONBOARDING.md（30 min）

371 行 / 12KB / 9 大节：
- TL;DR 决策树（你的工具是不是 MCP-capable）
- 路径 A：MCP 接入（muxcp 即可）
- 路径 B：HTTP 接入（schema + 鉴权 + 异步语义 + Python/Bash 范例）
- 数据模型概览
- 异步语义解释（什么情况下返回什么 status）
- 监控指南
- FAQ（"我重复推会重复实体吗"、"我推完查不到怎么办"、...）
- 设计原则速查

---

## 收尾事项（5/19 上午）

### 跨 AI 共享盲点诊断（**项目的副产品级洞察**）

Codex 被问 "Phase 3 进展如何" 时，**说 Cursor / 飞书未验证**——但其实都已验证。

诊断：
- ✅ claude-mem 134 条 24h obs 全在库（数据层完整共享）
- ✅ Codex 有 .codex-plugin，通过 muxcp 能调 claude-mem search（数据可达）
- ❌ Codex 用了它**更早的旧 obs#968（11:15 "unverified"）**+ 文件 scan，**没拿最新 obs#971/972（11:34 "verified"）**

**核心洞察**：
```
跨 AI 共享有两层:
  数据层 (DATA)           : 已打通 ✅
  解读层 (INTERPRETATION) : 不一致 ⚠️ ← 同一份数据, 不同 AI 解出不同结论
```

**对策**：调 Codex 时给明确 prompt——"查最新 24h obs, **不要用旧结论**"。Codex 用新 prompt 后正确识别全部验证证据。

这不是 kg-hub 一个工具能解决的——是 LLM 行业的对齐问题。但**意识到这点本身有价值**。

### IngestedKey.status=NULL 迁移

发现 4 条 legacy rows（Phase 3.A v1 时期，那版没 status 字段）：
- 有 episode_uuid 但 status=NULL → 迁移到 `status='ok'`
- 顺手修了 `queue_stats.ok_last_1h` 之前显示 0 的 bug（NULL `updated_at` 导致比较失败）

### Boot race 修复

**问题**：Mac 重启时 plist 先 fire，Docker / FalkorDB / Tailscale 还没起来 → ConnectionError 撞墙若干次自愈。

**修复**（6 个文件）：
- 新增 `utils/wait_for_dependencies.py`：`wait_for_port` / `wait_for_falkordb` / `wait_for_kg_hub_server`
- `claude_mem_obs.py`, `openclaw_capsule.py`, `kg_hub_server.py`：顶部 `wait_for_falkordb(60-90s)`，失败 exit 0/2 让 plist/launchd 自动 retry
- `watchdog.py`：首次跑等 60s kg-hub server，避免 boot 时 false alarm

单元测试：
```
wait_for_port up:    True, 0.00s    ← 服务已在: 立即返回
wait_for_port down:  False, 3.01s   ← 服务不在: 干净超时
```

---

## 完整文件清单（今天新增 / 大改）

| 文件 | 大小 | 状态 |
|---|---|---|
| `kg_hub_server.py` | ~24 KB | **新**（Phase 3.A） |
| `mcp_server.py` | ~12 KB | **+ kg_add_episode 工具** |
| `tools/watchdog.py` | ~7.5 KB | **新** |
| `utils/wait_for_dependencies.py` | ~2.5 KB | **新** |
| `utils/writer_lock.py` | +50 行 | **+ async_writer_lock** |
| `openclaw-deploy/skills/kg-query/SKILL.md` | 3 KB | **新** + 已 scp 到 VPS |
| `openclaw-deploy/scripts/kg-query.sh` | 1.5 KB | **新** + 已 scp 到 VPS |
| `docs/ONBOARDING.md` | 12 KB | **新** |
| `docs/PHASE-3-REPORT.md` | 本文件 | **新** |
| `ROADMAP.md` | Phase 3 章节重写 | 全部 checkbox + 证据 |
| `DESIGN.md` | + 决策 14/15/16 | 含妥协说明 |

## 4 个 launchd plist 后台运行

```
com.kg-hub.server              FastAPI :8080 (KeepAlive Crashed=true)
com.kg-hub.claude-mem-ingest   每 15 min × --limit 10
com.kg-hub.openclaw-sync       每 30 min, tar+ssh 拉 VPS
com.kg-hub.watchdog            每 10 min 主动巡检 + 边沿告警
```

---

## 端到端验证证据（铁证）

### Cursor 写入

```
FalkorDB Episodic 节点 761cc789-762d-46ed-9a01-0bc5a0e75d58
  name:    "Cursor kg_add_episode visibility verification"
  content: "Cursor MCP verification: kg_hub__kg_add_episode became 
            visible after restarting Cursor/MuxCP on 2026-05-18..."
```

### OpenClaw 读取

```
~/.kg-hub/logs/server.out.log:
  INFO: 100.79.177.102:56784 GET /api/search?q=Cron%20通知失败 200 OK
  INFO: 100.79.177.102:52382 GET /api/search?q=sync_openclaw%20...  200 OK
```

`100.79.177.102` = oc-vps-aliyun-us 的 Tailscale IP。本机调用会显示 `127.0.0.1`，**只可能是从 VPS 发起**。

### 异步验证

```
T1 POST /api/ingest (rich content)        →  202 in 1.89s
T2 status poll immediately                →  pending, episode_uuid=null
T3 retry same source_obs_id while pending →  202 in_progress (correct dedup)
T4 wait 67s, poll status                  →  ok, episode_uuid populated, 3 nodes/1 edge
T_queue queue_stats                       →  pending:0, ok_total:5, ok_last_1h:4
```

### Watchdog 边沿触发

```
伪造 "all 4 anomalies BAD" prev state → 实际全 OK 
  ✅ kg-hub server_down    resolved
  ✅ kg-hub queue_backlog  resolved  
  ✅ kg-hub stuck_jobs     resolved
  ✅ kg-hub recent_errors  resolved
再跑 (OK→OK)                          → 0 新告警 (alerts.log 仍 4 行)
```

---

## 已锁定的架构决策（DESIGN.md 决策 14/15/16）

> 详见 `DESIGN.md`，下面只是要点摘录。

### 决策 14：统一接入协议
- 所有写入走 `/api/ingest` 单一端点
- JSON schema: `{name, episode_body, source_description, reference_time, source_obs_id, sync}`
- 唯一约束键：`(source_description, source_obs_id)`
- 响应 status 枚举：`ok` / `skipped` / `accepted` / `in_progress` / `error`

### 决策 15：鉴权 + OpenClaw 接入约定
- Bearer token + Tailscale 网段限制
- OpenClaw 第一阶段只 read（skill + curl），write 留到 Phase 3.F（可选）

### 决策 16：手动写入路径
- Path A（当前）：`kg_add_episode` MCP → kg-hub 直写
- Path Z（未来）：`observation_add` → claude-mem.db → ingester → kg-hub
- 切换条件：claude-mem 升 server-beta 模式 → `observation_add` MCP 工具暴露

---

## 项目目标达成度（5/14 立项时的承诺）

| 目标 | 达成 |
|---|---|
| 跨工具数据汇聚 | ✅ OpenClaw 胶囊 + claude-mem obs + Cursor 手写, 全进同一 FalkorDB |
| 跨工具事实级查询 | ✅ 任何工具通过 MCP 或 HTTP 都能查 |
| OpenClaw 反向查询 kg-hub | ✅ kg-query skill + 飞书实战验证 |
| 不阻塞 IDE | ✅ 异步 API, 1.9s 返回 vs 200s 等 |
| 并发安全 | ✅ writer.lock 跨进程串行化 |
| 幂等去重 | ✅ IngestedKey MERGE 原子 |
| 主动监控不刷屏 | ✅ Watchdog 边沿触发 |
| 新工具自助接入 | ✅ ONBOARDING.md 12KB |

---

## 未做的事（明确推迟到 Phase 4 / 按需做）

| | 任务 | 触发条件 |
|---|---|---|
| 3.E | NAS 部署 FalkorDB + server | Mac 当 always-on 不可靠时（关机/休眠多） |
| 3.F | OpenClaw push plugin（TS+Bun） | 想要"胶囊产生即入图"实时性 |
| 3.G | 多 Mac 验证 | 你有第二台 Mac 在用 |
| 边名归一脚本 `--apply` | 588 重复边合并 | 数据 cleanup 周期里跑 |
| File 类型滥用 35.7% | schema description 收紧 + LLM prompt 强约束 | Schema v0.3 重构时 |
| 25→56 unclassified Entity | schema-only reclassify pass | 同上 |

---

## 项目副产品级洞察

### 1. 跨 AI 共享是双层问题（数据 + 解读）

我之前以为搭好数据共享就完——实际上每个 AI 都自己**解读**，可能得出不同结论。**对齐解读层是开放问题**，不是 kg-hub 一个工具能解决的。

### 2. claude-mem worker 模式的边界

Mac 上的 claude-mem 是 worker 模式 → MCP 只暴露读不暴露写。理想的 Path Z 接入路径**被这个边界临时挡住**，只能用 Path A 直写。决策 16 明确写了"等 server-beta 模式再切"。

### 3. graphiti `add_episode(uuid=X)` 是"更新"不是"创建"

文档里看不出来，**靠 NodeNotFoundError 反向工程**才搞清楚。这种 API 边界踩坑值得记。

### 4. async + 跨进程 lock 的混搭

`fcntl.flock` 是文件锁可跨进程，但**默认实现用 `time.sleep` 会阻塞 event loop**。要做异步 server 必须自己写 `async_writer_lock` 用 `asyncio.sleep`。这块教训完整记录在 `utils/writer_lock.py`。

### 5. 端到端验证 ≠ 单元测试

代码 import 干净 ≠ 真有 Cursor 用过。**只有 server.out.log 里某条来自 100.79.177.102 的 GET 才证明小迪真的调过**。本项目坚持"看 access log 拿铁证"的习惯应当延续到 Phase 4+。

---

## 文件指针速查

| 你想找什么 | 看哪 |
|---|---|
| 怎么接入新工具 | `docs/ONBOARDING.md` |
| 完整设计决策 | `DESIGN.md` §3 决策 1-16 |
| Phase 路线图 | `ROADMAP.md` |
| HTTP API 实现 | `kg_hub_server.py` |
| MCP 工具实现 | `mcp_server.py` |
| 主动监控 | `tools/watchdog.py` |
| OpenClaw 端 read 实现 | `openclaw-deploy/` 目录 |
| 当前状态自查 | `curl /api/queue_stats` + `python stats.py` |

---

## 致谢

这份成果是 **user + Claude（我） + Cursor + Codex + 小迪**这套真实多工具协作的产物。每个角色的功能边界 + 解读差异都暴露了真问题，反过来让架构决策更扎实。

跨工具知识图谱**不是一个孤立技术问题**——它是 AI 协同的基础设施。今天能闭环 Phase 3，证明这个基础设施在个人级规模上**真的能跑**。

下一步：用一段时间，看哪儿真痛，再决定 Phase 4 做哪几项。
