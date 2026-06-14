# 个人 AI 工程记忆栈 — 5 分钟速读

> **定位**：让看的人对整套系统**5 分钟内**形成清晰认知。
> **不是什么**：不是详细架构（那是 [ARCHITECTURE.md](ARCHITECTURE.md)），不是部署手册（那是 [`docs/INTEGRATION-GUIDE.md`](docs/INTEGRATION-GUIDE.md) + cookbook），不是决策记录（那是 [DESIGN.md](DESIGN.md)）。
> **要点导览**：30 秒定位 → 全栈一图 → 5 组件速看 → 3 个典型场景 → 想深入读哪份文档。

---

## 30 秒搞懂这是什么

**这是个人级的 GraphRAG 系统**——把你在不同设备 / 不同 IDE / 不同 project 上做过的所有工程动作（修 bug、改架构、踩坑），**自动**变成可被 AI 查询的**知识图谱**。

下次在任何设备的任何 IDE 里问"上次我们怎么修飞书通知那个 bug"，AI 能从图谱里拉出**实体 + 因果链**回答——不只是搜索关键字。

3 句话定位：
1. **底层**：claude-mem 自动捕获 IDE 工具调用 → qwen3.6-plus 抽成结构化"观察"
2. **中央**：kg-hub 把多源（claude-mem 各设备 / OpenClaw 胶囊）观察聚合成 FalkorDB 知识图谱
3. **分发**：MCP / HTTP / PUSH hook 三种方式把 KG 暴露给任意 IDE 查询

---

## 全栈一图

```
你（多设备多 IDE）
   ↓ 跑工具
   ┌──────────────┐
   │  claude-mem  │ ← 自动写记忆（hook 捕获）
   │  (每设备一份) │
   └──────┬───────┘
          ↓ 每 15 min push
   ┌──────────────┐
   │   kg-hub     │ ← 中央 KG，always-on NAS
   │  FalkorDB    │   Graphiti 抽实体 + bi-temporal
   └──────┬───────┘
          ↑ MCP / HTTP / PUSH hook
          │
       你的 IDE
   ─ Claude Code (读+写)
   ─ Cursor    (只读)
   ─ Codex     (读+写)
   ─ Qoder     (经 muxcp 中转)
```

---

## 5 个核心组件（一句话各自）

| 组件 | 角色 | 仓库 |
|---|---|---|
| **claude-mem** | 自动捕获 IDE 工具调用 → 结构化观察（每设备本地 SQLite） | [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) |
| **cookbook** | claude-mem 跨 OS / 跨 IDE 部署实战手册（独立 repo） | [commiao/claude-mem-integration-cookbook](https://github.com/commiao/claude-mem-integration-cookbook) |
| **kg-hub**（本仓库） | 多源知识图谱 + MCP 查询接口（FalkorDB + Graphiti） | [commiao/kg-hub](https://github.com/commiao/kg-hub) |
| **cc-switch** | 跨设备 / 跨 IDE 配置同步（让多机一致） | [farion1231/cc-switch](https://github.com/farion1231/cc-switch) |
| **muxcp** | 多 MCP server 聚合代理（给 IDE 一个统一入口）  | 私有部署 `~/.config/muxcp/` |

---

## 3 个典型场景

### 场景 ① 跨设备问历史

```
你昨天 在公司 MacBook 上 Claude Code 修了个飞书通知 bug
   ↓ claude-mem hook 自动捕获 → 公司 Mac 的 SQLite
   ↓ 每 15 min kg-hub 拉走 → 中央 FalkorDB
   ↓
今天 在家里 Cursor 问 "上次飞书通知 bug 怎么修的"
   ↓ Cursor 调 muxcp → kg_search
   ↓ 返回 obs 因果链
   ↓
答出："改了 capsule-feishu-alert.py，Cron 路由错了，已 verify"
```

### 场景 ② 新机器装机

走 [`ARCHITECTURE.md §6`](ARCHITECTURE.md) 的 7 步 ordered checklist：

```
Tailscale → cc-switch → claude-mem → kg-hub 客户端 →
  muxcp → IDE → 验收（任意 IDE 跑命令 → 等 15 min → KG 能搜到）
```

### 场景 ③ 多 IDE 同时工作

| IDE | 写记忆 | 读记忆 |
|---|---|---|
| Claude Code | ✅ hook | ✅ MCP + SessionStart 注入 |
| Cursor | ❌ 上游缺 hook | ✅ MCP |
| Codex 桌面版 | ✅ hook（7 个 hook 要 Approve） | ✅ MCP + SessionStart |
| Qoder | ⚠️ TranscriptWatcher | ✅ MCP 经 muxcp |
| OpenClaw | 人在回路写胶囊 → kg-hub pull | 通过 kg-hub MCP 查 |

---

## 想深入？按这个顺序读

| 想问的问题 | 读哪份 |
|---|---|
| 整个系统怎么协作的？ | [ARCHITECTURE.md](ARCHITECTURE.md)（11 章详细全栈） |
| 怎么在新机器装 claude-mem？ | [cookbook/docs/INSTALL.md](https://github.com/commiao/claude-mem-integration-cookbook/blob/main/docs/INSTALL.md)（跨 OS） |
| 我用 Cursor / Codex / Qoder，怎么接入？ | [cookbook/docs/ide-setup/](https://github.com/commiao/claude-mem-integration-cookbook/tree/main/docs/ide-setup) |
| kg-hub 客户端怎么接？ | [docs/INTEGRATION-GUIDE.md](docs/INTEGRATION-GUIDE.md) |
| 出问题怎么排查？ | [cookbook/docs/TROUBLESHOOTING.md](https://github.com/commiao/claude-mem-integration-cookbook/blob/main/docs/TROUBLESHOOTING.md)（14 个已知坑） |
| 为什么选 Graphiti 不选 Memgraph？ | [DESIGN.md §3 决策 9](DESIGN.md) |

---

## 当前状态（2026-06-13）

| 维度 | 状态 |
|---|---|
| kg-hub 阶段 | ✅ Phase 2 完成，NAS 化生产运行 |
| 中央 KG 规模 | ~3000 实体 / 多源 ingest（持续增长） |
| 设备覆盖 | MacBook + NAS 24/7（在线） |
| IDE 覆盖 | Claude Code / Cursor / Codex / Qoder（接入） |
| 数据源 | claude-mem obs + OpenClaw 胶囊（双源自动 sync） |
| LLM provider | qwen3.6-plus（阿里百炼 coding plan） |
| 下一步 | Phase 3 — OpenClaw 主动 push + schema cleanup |

---

## 3 句话的取舍

| 选了 | 放弃了 | 为什么 |
|---|---|---|
| Local-First + Central Sync | 远程 worker | 写延迟 + 离线可用 + 故障域隔离 |
| Graphiti + FalkorDB | 自建 KG 服务 | SPIKE 验证：已覆盖 90% 自建工作量 |
| Tailscale 内网 | 公网 HTTPS | 免运维 + 自带身份认证 |

详见 [DESIGN.md §3](DESIGN.md) 的 10 个锁定决策。

---

> 想了解更多？所有 source of truth 都在 [README.md](README.md) 的「资源总索引」表里。
