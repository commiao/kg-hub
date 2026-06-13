# 个人 AI 工程记忆栈 — 整体架构

> **范围**：本文档是**整个个人 AI 工程记忆栈**的架构总览，包含 kg-hub、claude-mem、cookbook、cc-switch、muxcp、各 IDE 客户端。
>
> **不是什么**：本文档**不是** kg-hub 自身的内部架构与决策——那是 [`DESIGN.md`](DESIGN.md)。
>
> **维护者**：jingmiao@liblib.ai（Lovart）
> **最近更新**：2026-06-13
> **目标读者**：(1) 6 个月后回来的自己 (2) 接手人 (3) 跟他人解释这套系统是什么时的 single source of truth

---

## 1. 一图概览

```
┌─────────────────────────────────────────────────────────────────────┐
│                  你的多设备多 IDE 工作流                              │
│   Mac 笔记本 / Windows 台式 / Linux 服务器 / VPS                     │
│   Claude Code · Cursor · Codex · Qoder · OpenClaw                   │
└───────────┬────────────────────────────────────┬────────────────────┘
            │                                    │
            │ 工具调用 (hook 捕获)                │ 显式查询 (MCP)
            ▼                                    │
┌─────────────────────────────────────┐          │
│ L1: claude-mem (各设备本地)          │          │
│   ─ Bun worker @ localhost:37701    │          │
│   ─ qwen3.6-plus 抽 obs              │          │
│   ─ SQLite: ~/.claude-mem/*.db       │          │
│   ─ 每设备一份，互不相通              │          │
└───────────────┬─────────────────────┘          │
                │                                │
                │ 异步 push (每 15 min)           │
                │ kg-hub ingester                 │
                ▼                                 │
┌─────────────────────────────────────┐           │
│ L2: 多源 ingester (kg-hub)           │           │
│   ─ ingesters/claude_mem_obs.py      │           │
│   ─ ingesters/openclaw_capsule.py    │           │
│   ─ writer_lock 串行化                │           │
│   ─ watermark + 幂等                  │           │
└───────────────┬─────────────────────┘           │
                │                                  │
                ▼                                  │
┌─────────────────────────────────────┐            │
│ L3: 中央 KG (kg-hub) ─ always-on NAS │ ◄──────────┤
│   ─ FalkorDB (Docker)                │   MCP 查询  │
│   ─ Graphiti (实体抽取 + bi-temporal)│   HTTP API  │
│   ─ kg_hub_server (FastAPI :8080)    │   PUSH hook │
│   ─ mcp_server (FastMCP)             │            │
└─────────────────────────────────────┘            │
                ▲                                   │
                │ 经 Tailscale 内网                  │
                │                                   │
┌───────────────┴─────────────────────────────────┐│
│ 分发层                                          ││
│   muxcp (MCP 聚合代理) ──┐                       ││
│   ~/.config/muxcp/        ├──→ 各 IDE 客户端 ◄───┘│
│                           │                      │
│   cc-switch (跨设备 cfg) ─┤                      │
│   WebDAV / Tailscale      │                      │
└─────────────────────────────────────────────────┘
```

## 2. 组件清单

| 组件 | 角色 | 仓库 / 位置 | 类型 |
|---|---|---|---|
| **claude-mem** | 低层工具调用捕获 → 结构化观察 | 上游 `thedotmack/claude-mem`，本地 fork `commiao/claude-mem` | 开源 npm 包，TS/JS |
| **claude-mem-integration-cookbook** | claude-mem 跨平台 / 跨 IDE 部署手册 | `commiao/claude-mem-integration-cookbook` | 文档 repo |
| **kg-hub** | 多源知识图谱 + MCP 查询接口（**本仓库**） | `commiao/kg-hub` | 用户原创 Python 项目 |
| **OpenClaw** | 人在回路提炼知识胶囊 + MEMORY.md 概念 | VPS 私有部署 | Agent 服务 |
| **cc-switch** | 跨设备 / 跨 IDE 配置同步 | `farion1231/cc-switch`（开源），本地 `~/.cc-switch/` | 桌面应用 |
| **muxcp** | 多 MCP server 聚合代理 | `~/.config/muxcp/`，私有部署 | Go 服务 |
| **各 IDE 客户端** | 用户日常工作面 | Claude Code / Cursor / Codex / Qoder / OpenClaw | 各家产品 |

每个组件的详细文档：
- claude-mem：[`thedotmack/claude-mem` docs site](https://docs.claude-mem.ai/)
- cookbook：见 `commiao/claude-mem-integration-cookbook` README
- kg-hub：本仓库 [README.md](README.md) / [DESIGN.md](DESIGN.md) / [ROADMAP.md](ROADMAP.md) / [`docs/INTEGRATION-GUIDE.md`](docs/INTEGRATION-GUIDE.md)
- cc-switch：[farion1231/cc-switch](https://github.com/farion1231/cc-switch)

---

## 3. 端到端数据流

### 3.1 写入路径（IDE 工具调用 → KG）

```
1. 你在 IDE 里让 agent 跑命令     (e.g. Read foo.ts / Bash ls / Edit bar.py)
                ↓
2. IDE PostToolUse hook 触发        (Claude Code / Codex 有；Cursor / Qoder 无)
                ↓
3. claude-mem worker 接收           (POST localhost:37701)
                ↓
4. qwen3.6-plus 生成结构化观察      (title/facts/narrative/concepts)
                ↓
5. SQLite 写入                       (~/.claude-mem/claude-mem.db)
                ↓
6. (异步) kg-hub claude-mem-ingest   (每 15 min launchd cron)
   ─ 读 watermark 之后的新 obs
   ─ Graphiti add_episode → 实体抽取
   ─ FalkorDB MERGE 节点 + 边
                ↓
7. KG 中央就绪供查询                  (FalkorDB :6379, group_id=kg_hub)
```

**OpenClaw 路径**（人在回路）：
```
你在 OpenClaw 主动总结 → markdown 胶囊 / 知识库文档 / MEMORY.md 概念
       ↓ (每 30 min launchd cron, sync_openclaw.py)
kg-hub openclaw-sync 从 VPS tar+ssh 拉
       ↓
kg-hub openclaw ingester → Graphiti add_episode → FalkorDB
```

### 3.2 读取路径（IDE 查询 KG）

#### 方式 A：MCP（IDE 内自然语言）
```
你在 IDE 里问 "之前我们怎么修飞书通知那个问题？"
                ↓
IDE agent 选用 mcp__muxcp__kg_hub__kg_search
                ↓
muxcp 路由到 kg_hub backend
                ↓
mcp_server.py 走 Cypher 查 FalkorDB
                ↓
返回边事实 + 实体邻居 → 注入 agent 上下文 → 答出
```

#### 方式 B：HTTP API（脚本 / CI）
```
任意客户端 → http://<tailscale-ip>:8080/api/canonical_context?project=foo
            (Bearer KG_HUB_API_TOKEN)
                ↓
FastAPI → FalkorDB → JSON 返回
```

#### 方式 C：PUSH hook（SessionStart 自动注入）
```
IDE 启动新会话
                ↓
SessionStart hook 调 kg-hub /api/canonical_context
                ↓
返回 3 条 canonical episodes（DESIGN / OBSERVATION-PHASE / ONBOARDING）
                ↓
注入 system prompt → agent 一开会话就"知道"项目历史
```

---

## 4. 职责边界（避免重叠造轮子）

| 任务 | 谁负责 | 谁**不**负责 |
|---|---|---|
| 自动捕获 IDE 工具调用 | claude-mem | OpenClaw、kg-hub |
| 把工具调用变成结构化观察 | claude-mem 的 qwen 抽取 | kg-hub 不重抽 |
| 提炼"高质量胶囊"（人在回路） | OpenClaw | claude-mem 自动流水账不做提炼 |
| 跨源聚合到统一图谱 | kg-hub | OpenClaw 自己的 `graph-*.json` 已废弃 |
| MCP 接口暴露给 IDE | kg-hub + claude-mem 各自 MCP server，**经 muxcp 聚合** | 各 IDE 不自己写 |
| 跨设备配置同步 | cc-switch + WebDAV | kg-hub 不管 |
| 跨网络互通 | Tailscale | 不暴露公网 HTTPS |
| LLM 凭证管理 | `~/.claude-mem/.env`（claude-mem 控制） | 其他工具读它不写它 |

**反模式**（DESIGN.md §3 决策 10 已锁定）：
- ❌ OpenClaw 自己维护图谱子系统
- ❌ claude-mem 也做胶囊提炼
- ❌ 多工具各自维护独立图谱
- ❌ Cursor 自己实现 hook 写 claude-mem.db
- ❌ 把 LLM 凭证散落各处

---

## 5. 不同设备 / 不同 IDE 的接入路径

### 5.1 接入矩阵

| 接 / 工具 | 接 claude-mem | 接 kg-hub | 备注 |
|---|---|---|---|
| Claude Code (Mac/Linux/Win) | 见 cookbook `ide-setup/claude-code.md` | 见 `docs/INTEGRATION-GUIDE.md` §4.1 | 主力 IDE |
| Cursor | 见 cookbook `ide-setup/cursor.md` | 见 `docs/INTEGRATION-GUIDE.md` §4.2 | 只读 |
| Codex 桌面版 | 见 cookbook `ide-setup/codex-desktop.md` | 见 `docs/INTEGRATION-GUIDE.md` §4.4 | 完整 |
| Codex CLI | 见 cookbook `ide-setup/codex-cli.md` | 见 `docs/INTEGRATION-GUIDE.md` §4.4 | 完整 |
| Qoder | 见 cookbook `ide-setup/qoder.md` | 见 `docs/INTEGRATION-GUIDE.md` §4.3 | 经 muxcp |
| OpenClaw | 见 cookbook `ide-setup/openclaw.md` | 见 `docs/INTEGRATION-GUIDE.md` §4.5 | 走中央 KG 桥接 |

### 5.2 跨 OS 差异（仅"部署"层）

|  | macOS | Linux | Windows |
|---|---|---|---|
| claude-mem worker 服务管理 | LaunchAgent | systemd user | Task Scheduler 或 WSL2 |
| kg-hub launchd ingester | LaunchAgent | systemd | （NAS / 远程，本地不跑） |
| muxcp | LaunchAgent / 手动 | systemd | 同上 |

详见 cookbook `INSTALL.md` 跨平台章节。

---

## 6. 新机器从零部署的 ordered checklist

按依赖图，**不能并行**：

```
Step 1: 物理网络
  □ 装 Tailscale，让本机 join 同一 tailnet
  □ 拿到中央 kg-hub 的 tailscale IP（如 100.123.208.32）

Step 2: 配置同步层
  □ 装 cc-switch 桌面应用 (brew tap farion1231/ccswitch)
  □ 配 WebDAV，拉同步 ~/.cc-switch/
  □ ~/.cc-switch/ 会带过来 cookbook / muxcp / .env 等配置模板

Step 3: claude-mem (底层捕获)
  □ 走 cookbook/docs/INSTALL.md 选你 OS 章节
  □ npx claude-mem@latest install
  □ 配 LLM provider (用 cc-switch 同步的 .env 或重新登录 Claude OAuth)
  □ 装 LaunchAgent / systemd / Task Scheduler
  □ 验证: curl http://localhost:37701/api/health

Step 4: kg-hub 客户端 (查询接入)
  □ 不需要本地跑 kg-hub server (服务跑在中央 NAS)
  □ 配 ~/.claude-mem/.env 加 KG_HUB_URL / KG_HUB_API_TOKEN
  □ 走 kg-hub/docs/INTEGRATION-GUIDE.md §2 + §3 配本机客户端

Step 5: muxcp (MCP 聚合，可选但推荐)
  □ 装 muxcp binary 到 ~/.local/bin/
  □ ~/.config/muxcp/ 已被 cc-switch 同步过来
  □ 重启 IDE，让它通过 muxcp 接入 kg-hub + claude-mem

Step 6: 你常用的 IDE
  □ 按 cookbook/docs/ide-setup/<你的 IDE>.md 接 claude-mem hook (写记忆)
  □ 按 kg-hub/docs/INTEGRATION-GUIDE.md §4.<你的 IDE> 接 MCP / PUSH (读记忆)

Step 7: 验收
  □ IDE 里跑一个工具调用，观察 SQLite 有新 obs
  □ 等 15 min 后查 kg-hub: kg_search 能搜到那条新 obs 抽出来的实体
  □ 新会话问 "我刚才做了什么"，应能基于 KG 答出
```

如果某步卡住：去 cookbook/docs/TROUBLESHOOTING.md 查 14 个已知坑。

---

## 7. 各组件演进现状

| 组件 | 版本 / 阶段 | 健康度 | 当前关注 |
|---|---|---|---|
| claude-mem (上游) | v13.6.0+ | ✅ 活跃，82k stars | 上游持续发新 release |
| cookbook | initial commit | ✅ 文档完整 | 待 IDE 实战修订（Windows 章节未实测） |
| kg-hub | Phase 2 完成 / NAS 化 | ✅ 生产运行 | Phase 3 设计中（OpenClaw 主动 push） |
| OpenClaw | VPS 自部署 | ⚠️ 单点 | 长期：迁 NAS 或 push 改 kg-hub 主动 |
| cc-switch | 1.x 上游 | ✅ 稳 | 跟上游版本即可 |
| muxcp | 私有部署 | ✅ 稳 | 偶发：muxcp 工具发现机制需调（[`docs/muxcp-discoverability-2026-05-21.md`](docs/muxcp-discoverability-2026-05-21.md)） |
| MCP 协议层 | 业界标准 | ✅ 各 IDE 都支持 | 关注 stdio → HTTP 转换层 |

---

## 8. 给接手人的 3 步上手提示

如果你接手了这个全栈：

### 第 1 步：先弄懂 *为什么*
读这 3 份文档（按顺序）：
1. [README.md](README.md) — kg-hub 是什么，怎么来的
2. [DESIGN.md](DESIGN.md) §3 — 10 个关键决策（**别重开讨论**）
3. 本文档 — 全栈视图

### 第 2 步：在你机器上跑一遍
按本文档 §6 的 ordered checklist 走完 Step 1–7。中间任何一步卡住，回 cookbook 的 TROUBLESHOOTING。

### 第 3 步：开始干活前先做最小验证
1. 在你 IDE 里跑一条简单命令（`ls ~`）
2. 等 15 min，去 kg-hub web ui (`http://<tailscale-ip>:3001`) 看是否有新节点
3. 在 IDE 里问 "我刚才跑了什么 ls"，能基于 KG 答出 = 全栈通了

如果第 3 步过了，恭喜你拿到了完整的"个人工程记忆"系统。

---

## 9. 演进路线

| 阶段 | 节点 | 状态 |
|---|---|---|
| 2026-05-11 | claude-mem 单设备部署 + cookbook 起草 | ✅ |
| 2026-05-14 | kg-hub 立项 + Graphiti SPIKE | ✅ |
| 2026-05-15 | kg-hub Phase 1：237/245/31 跑通 | ✅ |
| 2026-05-17 | kg-hub Phase 2：自动化 + Kuzu→FalkorDB | ✅ |
| 2026-05-22+ | kg-hub NAS 化 + 监控 + push hook 加固 | ✅ |
| 2026-06-13 | cookbook 独立成 repo + 全栈架构文档落地 | ⏳ 进行中 |
| 后续 | OpenClaw 主动 push 改造 | 📝 计划 |
| 后续 | Schema 演进 v0.3（File 类型收紧、UPPER/lower 合并） | 📝 计划 |
| 后续 | 本地 KG 副本（kg-hub Phase 3，离线优先） | 📝 按需 |

---

## 10. 不在本栈范围内（明确划线）

- ❌ **不做**对话短期记忆（user prompt 历史）—— 那是 IDE 各自的事
- ❌ **不做** git 替代品 —— 代码版本由 git 管
- ❌ **不做**多用户隔离 —— 个人栈，每用户一套独立部署
- ❌ **不解决** Cursor / Qoder 上游缺失的自动 hook —— 那是上游产品决策
- ❌ **不修改** claude-mem 源码 —— fork 只是为了挂 cookbook 文档分支（最终改成独立 repo）

---

## 11. 联系 / 反馈

任何新组件接入、任何反模式发现、任何架构演进的 *根本性* 改动，更新本文档 + 更新 kg-hub README 的「资源总索引」表格。两处保持同步。
