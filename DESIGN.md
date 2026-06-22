# kg-hub 设计文档

> 本文档**记录已做决策**与背后理由。新会话开干时**不要重开讨论已锁定的决策**，除非有新证据推翻原假设。

---

## 1. 项目动机

### 当前痛点（来自 claude-mem 实测 + OpenClaw 调研）

1. **单机限制**：claude-mem 每台设备一份本地 SQLite，互不相通
2. **跨 project 散乱**：同一个概念（如 "qwen3.6-plus"）在不同 project 重复出现，没有归一
3. **只有"召回"没有"导航"**：FTS5 能搜关键字，但无法回答"X 因为 Y 导致 Z 最后我们用 A 修复"这种因果链
4. **跨工具碎片化**：Claude Code 写记忆、Cursor 只读、Codex 写记忆，三家数据虽然在同一 SQLite，但语义关联靠人脑
5. **OpenClaw 关系被锁死**：用户在 OpenClaw 已经积累 179 个知识胶囊 + 36 个知识文档 + ~20 个 MEMORY.md 概念，但 OpenClaw 自身的"图谱"只是 trivial 的 Task-Session 快照（4 节点 4 边），实质性关系（fixes / relates_to / diagnosed_by 等）全部以**自然语言隐式存在于 markdown 里**，没有任何工具能跨工具调用

### 目标

构建**个人级 GraphRAG 系统**，让 AI 工具基于一个**多设备聚合的知识图谱**回答工程问题。

### 非目标（明确**不做**的事）

- ❌ 不修改 claude-mem 本身的行为
- ❌ 不解决 Cursor 缺失自动 hook 的问题
- ❌ 不做对话短期记忆（user prompt 历史等）
- ❌ 不做版本控制 / git 替代品
- ❌ 不做团队多用户（先单用户多设备）

---

## 2. 架构总览

### 数据流

```
[设备 A: MacBook]                  [设备 B: NAS / 工作站]
┌────────────────┐                 ┌────────────────┐
│ claude-mem      │                 │ claude-mem      │
│   ↓             │                 │   ↓             │
│ SQLite obs     │                 │ SQLite obs     │
└─────┬──────────┘                 └─────┬──────────┘
      │                                  │
      │ kg-push agent (本项目)           │ kg-push agent
      │ - 读新 obs (watermark)           │
      │ - qwen 抽 entities/relations     │
      │ - 批量 push                       │
      ▼                                  ▼
      └────────────► [中央 KG] ◄─────────┘
                     Memgraph
                     (Docker, 内网)
                          ▲
                          │ MCP RAG 接口
                          │
              ┌───────────┼───────────┐
              │           │           │
        Claude Code   Cursor      Codex
        (任意设备的任意工具，通过 MCP 查询)
```

### 三层结构

| 层 | 角色 | 实现 | 状态 |
|---|---|---|---|
| **L1: 数据采集** | 各设备自动产生 obs | claude-mem（已建） | ✅ 复用 |
| **L2: 抽取 + 传输** | 把 obs 抽成 KG triples 推到中央 | kg-push agent（**待写**） | 📝 待做 |
| **L3: 中央 KG + 查询** | 存图 + 暴露查询 MCP | Memgraph + kg-mcp-server（**待写**） | 📝 待做 |

---

## 3. 关键决策与理由

### 决策 1：Local-First + Central Sync（不是远程 worker）

**选择**：各设备保留**完整本地 worker**（claude-mem 不动）；只在它之上加 push agent 异步推到中央。

**Rejected**：
- ❌ "把 worker 装 NAS，所有设备远程连"——单点故障 + 写延迟受网络影响 + 离线全废

**理由**：写入零延迟、离线可用、故障域隔离。中央挂掉只影响**新数据归集**，不影响本地工作。

### 决策 2：中央存储用 Memgraph

> ⚠️ **此决策已被决策 9 取代**（2026-05-14 post-SPIKE）—— L3 改用 Graphiti + Kuzu。以下内容保留作为决策演进的历史记录。

**选择**：Memgraph 社区版（Docker 部署）

**Rejected**：
- ❌ Neo4j Community：重、JVM、社区版有功能限制
- ❌ KuzuDB：embedded，不适合中央 server 形态
- ❌ PostgreSQL + AGE：SQL+图能力不够专业

**理由**：Cypher 兼容（Neo4j 生态可用）、内存优先性能好、社区版无功能阉割、体积比 Neo4j 小一个数量级。

### 决策 3：本地 KG 副本（Phase 3）按需后做

**选择**：先做"无本地副本"的最简版本——所有图查询走中央。

**Rejected（推迟）**：
- 🟡 Phase 3 才做本地 KG embedded 副本（KuzuDB）——加上后才有完整 Local-First 体验

**理由**：先验证中央 KG 本身有意义。本地副本能减少网络依赖、增加离线能力，但工作量翻倍。**先跑通中央版本，体验有缺再加副本**。

### 决策 4：实体抽取继续用 qwen3.6-plus

**选择**：复用 claude-mem 现有的百炼 coding plan

**Rejected**：
- ❌ 用本地开源小模型：质量不够
- ❌ 用 GPT-4 / Claude：花钱

**理由**：免费、已经接好、qwen 在中文实体抽取上够用、跟 claude-mem 数据流一致避免风格漂移。

### 决策 5：MCP 作为唯一客户端接入协议

**选择**：暴露一组 MCP 工具（`kg_find` / `kg_neighbors` / `kg_path` 等）

**Rejected**：
- ❌ REST API：要每个 IDE 写客户端
- ❌ SDK：同上

**理由**：MCP 是 AI agent 工具调用的事实标准，Claude Code / Cursor / Codex 都已用它接 claude-mem，无缝复用接入路径。

### 决策 6：推送协议 — HTTP + 增量 + 幂等

**选择**：
- 设备端维护 watermark（last_pushed_obs_id）
- 批量 POST 到中央 `/api/ingest`
- 用 obs.id + content_hash 做幂等键
- 失败指数退避

**Rejected**：
- ❌ 实时流（SSE/WebSocket）：复杂度高、对当前规模过度
- ❌ 文件同步（rsync）：丢失结构信息

**理由**：HTTP 简单、易调试、易扩容、对当前数据规模足够。

### 决策 7：网络层走 Tailscale 内网

**选择**：所有设备和中央仓库都在 Tailscale 网络里互通

**Rejected**：
- ❌ 公网 HTTPS：要管证书、防 DDoS、限频
- ❌ 局域网：跨网络不行

**理由**：免运维、自带身份认证、加密走 WireGuard、跨网络穿透、Tailscale 免费版够个人用。

### 决策 8：Phase 0 数据基底从 claude-mem obs 切到 OpenClaw 胶囊

**选择**：Phase 0 用 OpenClaw 导出的 **179 胶囊 + 36 知识文档 + MEMORY.md** 作为图谱构建源头，**不再用 claude-mem 当前那 155 条 obs**。

**Rejected**：
- ❌ claude-mem obs：本机刚开始记录，样本量太少，跑出来的图说服力不足
- ❌ 完全不做 Phase 0 直接做 Phase 1：基础设施先行 = 沉没成本风险

**理由**：
- 数据规模差一个数量级（179+ vs 155），且**胶囊本身已经是高密度结构化知识**，不是流水账 obs
- 胶囊有 quality_rating、tags、source 字段，天然适合做图谱节点
- OpenClaw 本身就建议这么做：原话 "建议把胶囊的 tags、source、usage 以及 MEMORY.md 中的隐式关系都抽取成正式的节点和边"
- 这条路一旦走通，kg-hub 立刻就有**两个数据源**：OpenClaw 胶囊（成熟知识） + claude-mem obs（增量过程记录），互补

**含义**：
- v0.1 schema 需要新增 `Capsule` 和 `KnowledgeDoc` 节点类型（详见 §4）
- 边类型补充 `extracted_from` / `relates_to` / `documented_in` / `diagnosed_by` / `implemented_as`
- Provenance 设计要比 OpenClaw 强（OpenClaw 自己承认 provenance 弱）

### 决策 9：L3 层（中央 KG + 查询）采用 Graphiti，不再自建

**选择**：用开源库 [Graphiti](https://github.com/getzep/graphiti)（Zep 公司开源、Apache 2.0）+ Kuzu(embedded) 作为 L3 层。Kuzu 在 Phase 1 跑通后，再视规模迁移到 Neo4j 或 FalkorDB（保持图后端可替换）。

**Rejected**：
- ❌ 自建 Memgraph + FastAPI + MCP server：SPIKE 证明 Graphiti 已覆盖所有自建工作，重造轮子
- ❌ Cognee：理论上更适合多源场景，但未验证；当前 ROI 不足以再花 2-3 小时 SPIKE
- ❌ Microsoft GraphRAG：算法强但为静态文档库设计，indexer 跑数小时，不适合 agent 实时场景
- ❌ LightRAG / Mem0 / Letta：定位错位或图能力弱

**理由**：
- **已验证可行**：2026-05-14 SPIKE 用 5 个真实 OpenClaw 样例 + qwen3.6-plus 跑通：**27 实体 / 17 边**；因果链关键节点（CAPSULE-NOTIFICATION-ROUTE-2026 / notify-send.sh / Cron / 投资晚报 / 战略规划群 / 实战验证）全部到位；语义搜索 "Cron 通知失败怎么修的" 第一条返回实战验证 fact。**LLM 自创了边名（PROPOSES_SOLUTION / MISROUTED_TO 等）而非 v0.2 schema 名（implemented_as / caused_by），证实 entity_types/edge_types 约束是 Phase 1 必做项**
- **bi-temporal provenance**：每条边自带 `valid_at` / `invalid_at` / `created_at` / `expired_at`，强过 OpenClaw 的 weak provenance
- **内置 MCP server**：省掉一整层封装工作
- **多后端解耦**：今天用 Kuzu，明天换 Neo4j，业务代码不动
- **L3 可逆性强**：将来 Graphiti 跟不上需求，换 Cognee / 自建 只动 L3，L2 的 ingester 不受影响

**已知局限（Phase 1 要处理）**：
- 默认 LLM 抽取的边类型是自创名字（如 `IMPLEMENTS_ARTIFACT`），不是 v0.2 schema 的 `implemented_as`。**必须通过 `entity_types=` / `edge_types=` 参数约束**
- graphiti-core 0.29 的 Kuzu driver **不自动建 FTS 索引**，要手动 `INSTALL fts; LOAD fts;` + 跑 4 条 `CREATE_FTS_INDEX`
- 调用 qwen3.6-plus 时必须注入 `extra_body={"thinking":{"type":"disabled"}}`，否则强制 tool_choice 被拒
- `load_dotenv` 须传 `override=True`（Claude Code shell 自带 `ANTHROPIC_BASE_URL`）

**含义**：
- 原 Phase 1（自建 Memgraph + FastAPI + MCP，1 周预算）→ 缩减到"接 Graphiti"（1-2 天）
- 原 Phase 0.C（自写 entity extraction prompt）→ 取消，由 Graphiti.add_episode() 替代
- kg-hub 的独有价值收窄到 ingest 层（L2）+ 部署运维 + 多源协调

### 决策 10：三方职责分工 —— claude-mem / OpenClaw / kg-hub 各管一段

**选择**：明确三个工具的角色边界，**OpenClaw 不再自维护图谱子系统**（停掉 `notes/knowledge-graph/graph-*.json`），所有图谱能力集中到 kg-hub。

| 工具 | 角色 | 保留 | 停掉 |
|---|---|---|---|
| **claude-mem** | 低层原始观察捕获 | 工具调用 hook → qwen 生成 obs → 本地 SQLite | — |
| **OpenClaw** | 高层知识提炼（人在回路） | 知识胶囊 markdown 生成 / MEMORY.md 概念维护 / 知识库文档 | 自维护 `graph-*.json`（trivial 4 节点）/ 自建图谱查询接口 |
| **kg-hub** | 统一图谱 + 查询接口 | 多源 ingest / Graphiti / MCP 暴露 | 不生产原始数据 |

**Rejected**：
- ❌ 让 OpenClaw 内部增强自己的 KG：造重复轮子，且它的内部对用户不可见、不可控
- ❌ 让 claude-mem 也做胶囊提炼：claude-mem 的优势是"自动 + 完整捕获"，胶囊是"提炼 + 取舍"，应当由 OpenClaw 那种 agent 场景做
- ❌ 多工具各自维护图谱：碎片化、跨工具语义对不齐，正是 kg-hub 要解决的问题

**理由**：
- OpenClaw 自己承认其 `graph-*.json` 只是 trivial 快照（4 节点 4 边、全部 `EXECUTED_IN` 边），**没有实质图谱价值**。真正的知识密度在胶囊和 MEMORY.md 里
- 多源向中央汇聚是 kg-hub 的核心价值主张，每个源各自留一份图就违背初衷
- 分工后边界清晰：将来加新源（git / Slack / Notion / 其他 AI agent）都按统一协议接入 kg-hub，不需要每个工具自己造 KG

**OpenClaw 端的演进路径**（要跟 OpenClaw 沟通推进）：

| 阶段 | OpenClaw 端动作 | 由谁推动 |
|---|---|---|
| **Phase 2 短期** | 不改 OpenClaw，kg-hub 从 VPS rsync 拉胶囊（pull 模式） | kg-hub 单方面 |
| **Phase 3 中期** | OpenClaw 加 hook：新胶囊产生时 POST 通知 kg-hub（push 模式，秒级入图） | 跟 OpenClaw 对话沟通 |
| **Phase 4 长期** | OpenClaw 弃用 `notes/knowledge-graph/`；它自己要查图时调 kg-hub MCP | 跟 OpenClaw 对话沟通 |

**Phase 3 push 模式的技术可行性已被证实**（2026-05-17 飞书与 OpenClaw 沟通 claude-mem 评估时无意中暴露）：

OpenClaw Gateway 内置一套事件钩子系统，5 个钩子：

| Hook | 触发时机 | kg-hub 候选用途 |
|---|---|---|
| `before_agent_start` | 会话开启 | — |
| `before_prompt_build` | 注入 system prompt 前 | 反向：读 kg-hub 注入上下文（远期） |
| **`tool_result_persist`** | 工具调用结果落盘 | **最相关**：胶囊产生时打点 POST kg-hub |
| **`agent_end`** | 会话结束（总结时机） | **次相关**：批量推会话级胶囊 |
| `gateway_start` | gateway 进程启动 | 初始化检查 |

**已知活体实现作为参考**：claude-mem 插件（不是 MCP，是 OpenClaw 自有插件机制）已经接入上述 hooks 跑通完整 observation 捕获 → 本地 SQLite → SSE 推送链路。kg-hub Phase 3.2 可以照抄它的接入模式，把目标端从 SQLite 换成 `POST /api/ingest`。

**采纳态度**：将这些 hook 作为 Phase 3.2 实施的**首选技术路径**，但**不绑死**——如果 OpenClaw 不开放 hook 给第三方插件，回退方案是：
- Plan B：OpenClaw 写完胶囊文件后由文件系统 watcher（inotify on VPS）触发 POST
- Plan C：继续 Phase 2 rsync pull 模式（已实现，作为最终兜底）

**kg-hub 端的 ingest 协议（开放给任意数据源）**：

| 接入方式 | 适用场景 | 实现 |
|---|---|---|
| **A. HTTP POST `/api/ingest`** | 有 webhook 能力的服务（飞书 / Slack / GitHub） | kg-hub FastAPI 端点（Phase 1） |
| **B. 文件 drop + watcher** | 文件型数据源（Obsidian / Notion 导出 / 邮件存档） | watchdog 监视目录，新文件即推 |
| **C. MCP 工具 `kg_add_episode`** | 任意 AI agent（不限工具）想主动写入 | Graphiti MCP server 自带或扩展 |
| **D. 定时拉**（Phase 2 暂用） | 数据源不能主动 push（如当前的 OpenClaw） | cron / launchd + rsync + diff |

**通用契约**：所有 ingest 路径最终都构造同一个 `add_episode()` 调用，需要三件东西：
1. **自然语言文本**（一段描述事件的文字）
2. **时间戳**（reference_time，事件实际发生时间）
3. **来源标识**（source_description，方便回溯）

任何数据只要能转成这三元组，就能进 kg-hub —— 不需要 schema 对齐、不需要专门写解析器（LLM 自己抽实体和关系）。

**典型新源接入计划（Phase 4+ 候选，按需）**：
- **git commit**：cron 扫 `git log` → 每条 commit 作为一个 episode → `episode_body = commit message + diff summary`
- **飞书群消息**：webhook 接收 → POST `/api/ingest`
- **Obsidian 笔记**：watcher 监视 `~/Obsidian/` → 新增 markdown 即推
- **其他 AI agent**：配置 kg-hub MCP 服务，agent 调 `kg_add_episode` 主动贡献知识

### 决策 11：L3 图后端 Kuzu → FalkorDB（2026-05-17 Phase 2 启动时迁移）

**选择**：把 Phase 1 期间用的 Kuzu (embedded) 切换到 **FalkorDB (Docker, Redis 协议)**。

**Rejected**：
- ❌ 继续用 Kuzu：embedded 单写者锁——Phase 2 引入定时 ingest + MCP 读并发后必然死锁
- ❌ Neo4j Community：镜像 600MB / JVM 1GB+ 内存，NAS 部署成本高 5×；社区版企业级特性用不上
- ❌ Memgraph：graphiti-core 0.29 没有官方 driver（只支持 Kuzu / Neo4j / FalkorDB / Neptune）

**理由**：
- **解锁并发**：FalkorDB 基于 Redis 单线程 command processor，**多客户端并发读写 OK**
- **轻**：镜像 120MB（Neo4j 600MB 的 1/5），内存 ~100MB（Neo4j 1GB+ 的 1/10）——NAS 友好
- **备份简单**：Redis RDB 快照，在线 `BGSAVE` + 拷一个 dump.rdb 文件
- **graphiti-core 官方 driver**：`graphiti_core.driver.falkordb_driver.FalkorDriver`
- **数据量适配**：FalkorDB 适合 1M 节点以下，我们 100k 都到不了，够用

**已知 graphiti-core 0.29 局限（migration 中绕过）**：
- `FalkorDriver.default_group_id = "\\_"` 自家 `validate_group_id` 拒绝反斜杠 → 必须显式传 `group_id`
- group_id 被当作 graph 名 → 设 `database="kg_hub"` 跟 `group_id="kg_hub"` 对齐，保证读写同一 graph
- FalkorDB schema 用**直接边** `(:Entity)-[e:RELATES_TO]->(:Entity)`，不像 Kuzu 把边 reify 成中间节点 → mcp_server.py 的 Cypher 全重写

**含义**：
- 决策 9 的"L3 用 Graphiti+Kuzu"路线已被本决策替代；Kuzu 文件保留为 `data/kg.kuzu.bak-2026-05-15` 回滚保险
- 部署路径：本机 Docker 开发 → 验证通过 → 推 ACR → NAS 拉取部署（pending Phase 3）

### 决策 12：本机所有 writer 走共享 `writer.lock` 串行化

**选择**：所有调用 `graphiti.add_episode()` 的进程在启动时 fcntl flock `~/.kg-hub/locks/writer.lock`，独占持锁直至结束。锁竞争失败默认 fail-fast（exit 0，留给下一个 launchd interval 重试）；手动 backfill 可传 `--wait-seconds N` 耐心等。

**Rejected**：
- ❌ 不加锁，依赖 graphiti 相似度合并：会出现重复实体（两个 writer 同时见"Cron"不存在 → 都创建），事后清理代价比预防大
- ❌ 应用层锁 + watermark 协同：复杂度高，watermark 是文件级 / obs 级的，不解决跨 writer entity dedup
- ❌ DB 级 transaction lock：FalkorDB 没暴露这种原语，且 add_episode 的"读-LLM抽取-写"跨太多步骤，事务包不住

**理由**：
- **race window 真实存在**：graphiti add_episode 流程 = LLM 抽取 (10-30s) → 查重 → 写入；步骤 2→3 之间的窗口够大，两个并发 writer 必撞
- **fcntl flock 是 OS 自动释放**：进程崩 / launchd SIGKILL 都自动释放，无需 stale lock 清理
- **预防优于事后清理**：合并 entity 比创建新 entity 慢得多（要更新所有引用边）

**Scope（重要边界）**：
- ✅ 覆盖：本机所有 ingester（`openclaw_capsule.py` / `claude_mem_obs.py` / 未来手动写）
- ❌ 不覆盖：MCP READ 工具（不写）
- ❌ 不覆盖：跨设备 writer（Phase 3 OpenClaw push 模式需要**服务端 idempotency key** + 唯一约束，文件锁够不着远端）

**实现**：`utils/writer_lock.py` 提供 `writer_lock(owner, timeout_seconds)` context manager + `WriterLockBusy` 异常。

### 决策 13：所有数据源统一用 `group_id="kg_hub"`（不做源级分区）

**选择**：openclaw_capsule / claude_mem_obs / 未来所有 ingester 写入 FalkorDB 时一律 `group_id="kg_hub"`，落入同一个 graph。源信息靠 `source_description` 字段区分。

**Rejected**：
- ❌ 每源一个 graph（如 `group_id="openclaw"` / `"claude_mem"`）：跨源查询必须 union 两个 graph，违背"统一图谱"初衷
- ❌ 加 Entity 属性 `source_kind`：可以做但不必现在做；当前 source_description 已包含信息

**理由**：
- **跨源关联是项目核心价值**：Cron 这个节点同时被 OpenClaw 胶囊和 claude-mem obs 引用——必须在同一 graph 才能合并
- **Phase 2 实测验证**：50 obs claude-mem ingest 后，Cron(63) / 小迪(52) / sync_openclaw(40) 等节点把"OpenClaw 胶囊知识" + "Claude Code 工作记录"自然合并成中心 hub
- **如果后悔可加属性回退**：未来需要源级过滤时，给每个 Entity 加 `source_kind` property 即可

**含义**：
- FalkorDB 里只有一个用户数据 graph：`kg_hub`（`default_db` 在初始化时残留过，已 drop）
- `graphiti_client.py` 的 `FALKORDB_DATABASE` 设为 `"kg_hub"`

### 决策 14：统一接入协议 — `/api/ingest` 单一 HTTP 入口 + 固定 JSON schema

**选择**：Phase 3 起，**所有写入**走 kg-hub HTTP server 的 `/api/ingest`。MCP 写工具 `kg_add_episode` 也是这个端点的**轻量壳**（内部转发 HTTP），不直连 graphiti。

**Rejected**：
- ❌ MCP 直写 + HTTP 直写双路径并存：两套 idempotency / 鉴权 / 限流，容易漂移
- ❌ 每个源一个端点（`/api/ingest_openclaw`、`/api/ingest_claude_mem`）：违背"统一协议"初衷

**理由**：
- 单入口 = 单 auth path + 单 idempotency 表 + 单限流策略
- 任意源（Cursor / OpenClaw / 飞书 / Slack / git / 未来工具）按同一 schema 投递即可
- MCP 写工具退化为"语法糖"，跟 HTTP 写完全等价

**Body schema（v1，所有源必须遵守）**：
```json
{
  "name": "<short identifier, idempotent across retries>",
  "episode_body": "<full natural-language text to extract entities from>",
  "source_description": "<who sent this — e.g. 'openclaw-capsule', 'claude-mem-mac-jingmiao', 'cursor-manual'>",
  "reference_time": "<ISO 8601 timestamp of when the event happened>",
  "source_obs_id": "<unique-within-source ID for idempotency>"
}
```

**响应**：
```json
{ "status": "ok",      "episode_uuid": "<uuid>" }     // 新写入
{ "status": "skipped", "reason": "duplicate" }         // idempotency 命中
{ "status": "error",   "code": "...", "message": "..." }
```

**Idempotency 约束**：
- 服务端用 `(source_description, source_obs_id)` 做唯一键
- 已存在 → 返 200 OK + `status:skipped`（**不**返 4xx，方便客户端傻 retry）
- 服务端用 FalkorDB 一张 `IngestedKey` 节点表持久化此键 + episode_uuid

**鉴权**：见决策 15。

### 决策 15：kg-hub server 鉴权模型 + OpenClaw 接入约定

**选择**：
- **鉴权**：`Authorization: Bearer <KG_HUB_API_TOKEN>` header，token 持久化在 `~/.claude-mem/.env`
- **网络层**：只监听 Tailscale 网段（公网不开）—— 跟决策 7 "网络层走 Tailscale 内网" 一致
- **OpenClaw 接入**：**读优先**（skill kg-query + curl wrapper），**写延后**（保留 rsync Phase 2 路径，写 plugin 到 3.F 再说）

**Rejected**：
- ❌ 公网 HTTPS + 复杂 OAuth：个人项目复杂度过度
- ❌ 无鉴权 + 仅靠 Tailscale：万一 Tailscale 配置错误（曾经在 openclaw 项目踩过）即裸奔
- ❌ Phase 3 一上来就给 OpenClaw 写 TS plugin：rsync pull 已经把胶囊带回来了，先把 "OpenClaw 能查 KG" 这个真阻塞解决

**理由（鉴权）**：
- Bearer token 实现简单（HTTP header 一行）
- Token 复用 `~/.claude-mem/.env` 这个**项目已有的密钥存储**，零新机制
- Tailscale 网段限制是 belt-and-suspenders 第二道防线

**OpenClaw 接入两阶段**：

```
Phase 3.C（现在做）：READ-only 接入                                    ← 解决真痛点
  ~/clawd/skills/kg-query/SKILL.md             告诉 LLM 何时调
  ~/clawd/scripts/kg-query.sh                  curl wrapper
  + ~/.openclaw/env.sh 加 KG_HUB_URL + KG_HUB_TOKEN
  → OpenClaw 不知道 kg-hub 内部，只看到一个 skill 工具

Phase 3.F（延后）：WRITE plugin 接入                                    ← 锦上添花
  ~/.openclaw/extensions/kg-hub/
    ├── openclaw.plugin.json     id="kg-hub", configSchema=...
    ├── package.json             { openclaw: { extensions: ["./dist/index.js"] } }
    └── dist/index.js            Bun-compiled TS, hook tool_result_persist
  + `openclaw plugins install ~/.openclaw/extensions/kg-hub`
  + `openclaw plugins enable kg-hub`
  → 取代 rsync pull
```

**为什么不一次性把 push plugin 也做了**：
- Phase 3 真阻塞是 read（决策 0 用户拍板），push 不阻塞
- TS plugin 要装 Bun 到 VPS + 写 + 测，至少 4 小时
- rsync pull 每 30 min 跑一次已经覆盖 push 需求

**含义**：
- Phase 3.A 写的 FastAPI server 必须支持 `Authorization: Bearer ...` middleware
- `~/.claude-mem/.env` 新增 `KG_HUB_API_TOKEN=<random-32-char>` 一行
- OpenClaw VPS 的 `~/.openclaw/env.sh` 加 `KG_HUB_URL` + `KG_HUB_TOKEN`（同一 token）
- kg-hub server bind 端口 8080，Tailscale IP 上监听（不绑 0.0.0.0 在公网）

### 决策 16：手动写入路径分流（**临时**：保留 `kg_add_episode` 作为过渡）

**选择**：Mac IDE 主动写入（Cursor / Codex 等非自动捕获场景）暂走 `kg_add_episode` MCP 直写 kg-hub（路径 A）。**未来等 claude-mem 升级 server-beta 后**，切换到 `observation_add` 经 claude-mem → ingester → kg-hub（路径 Z，理想态）。

**理想态（Path Z, 目标）**：
```
Mac IDE 任意写 → claude-mem observation_add MCP → claude-mem.db
             → claude_mem_obs.py ingester (15 min)
             → kg-hub
```
理由：
- **职责单一**：claude-mem = Mac 端记忆层 / kg-hub = 中央图谱，不交叉
- **local-first**：kg-hub 挂时写入仍在 claude-mem.db 落盘，恢复后追上
- **会话注入**：写入自动出现在下次 Claude Code 系统提示里（claude-mem 的核心价值之一）

**实测阻塞（2026-05-18）**：
- 本机 claude-mem 当前是 `worker` 运行模式
- `worker` 模式 MCP **仅暴露读工具**（search / timeline / get_observations），**不暴露 observation_add**
- 要开放需切 `CLAUDE_MEM_RUNTIME=server-beta`——动第三方插件运行模式，风险未评估

**过渡态（Path A, 当前）**：
```
Mac IDE 主动写 → mcp_server.py kg_add_episode → POST /api/ingest → kg-hub
```
trade-off：
- ✅ 立即可用，跨工具查询能找到
- ❌ 失去 next-session 自动注入（Claude Code 不会自动看到 Cursor 写的东西）
- ❌ 工具职责轻微交叉（kg-hub MCP 接 Mac IDE 写，本应只接读）

**触发切换到 Path Z 的条件**（满足任一即可）：
1. claude-mem 升级到 `server-beta` 默认模式 → `observation_add` 自然可用
2. 我们自己研究通了 worker → server-beta 的切换路径且验证不破坏现有 Claude Code 自动捕获
3. 切换后**删 `kg_add_episode` MCP 工具**（保留 `/api/ingest` HTTP，给跨设备源用）

**与其它源的关系**（决策 16 只管 Mac IDE 主动写）：
- Claude Code 自动捕获：本来就走 claude-mem hook，跟决策 16 无关
- OpenClaw 远程源：必须走 `/api/ingest` HTTP（VPS 上没 Mac 的 claude-mem.db），跟决策 16 无关
- 飞书 / Slack / git webhook 等：同 OpenClaw 走 HTTP

### 决策 17：跨客户端 MCP 配置改造（native generator 分发，muxcp 降级为 fallback）

**状态**：🟢 **Proposed / Hybrid path PRODUCTION SMOKE PASS (2026-05-22)** —— Codex hybrid 已生产安装并通过三条核心路径冒烟验证，进入 1 周观察期（**仍不是 Locked**：Locked 需观察期 + Phase 3+4 完成）

**真正锁定的内容**（更新于 2026-05-21 Codex 验证后）：
- ✅ Codex native stdio MCP 路径**可行**——多 server 共存 + 工具名展示干净均已实测
- ✅ **muxcp 角色重定义**：从"全聚合运行时网关"→ **"SSE/legacy 协议适配兜底层"**（继续为 Aliyun 这类经典 SSE MCP 服务，不再以淘汰为目标）
- ✅ **Hybrid migration 路径采纳**——stdio MCP 走 Codex native，SSE/经典协议 MCP 保留 muxcp_fallback

**未锁定的内容**：
- ❌ 不锁定"全面迁移 native"（被 SSE 协议错位证伪）
- ❌ 不锁定"删除 muxcp"（muxcp 是协议适配层，长期保留）
- ❌ 不锁定 SSE bridge 具体实现方案（Phase 3 评估后再定）
- ❌ 不锁定 schema 终稿（`schema_version=1` 起步，按需迭代）

**触发场景**：多客户端（Claude Code / Cursor / Codex）使用 muxcp 聚合时工具发现性显著下降——Codex 端工具名被截断+hash（`aliyun_observability__sls_translate_tex_1d19bb73577d`），LLM 静默漏触发，跨客户端体验不一致。即使加了 SessionStart hook 也只能救 Claude Code。

**选择**：把 muxcp 从"运行时网关"降级为"可生成目标"之一。引入**中立 schema** 作为 source of truth，通过 generator 工具按 client 生成原生 MCP 配置。各客户端直连上游 MCP，请求路径不再经过 muxcp。

```
当前：
  Client → muxcp → upstream MCPs    （请求被聚合 + 加 mcp__muxcp__ 长前缀）

目标：
  source.yaml（中立 schema, WebDAV 同步）
   + ~/.config/ai-mcp/local.yaml（本机 secrets / 路径）
       ↓ generator.py
       ├─ generated/cursor.mcp.json
       ├─ generated/claude.mcp.json
       ├─ generated/codex.config.toml
       └─ generated/muxcp/current.yaml（fallback gateway，仅 Claude Code 仍用）

  Client → upstream MCPs            （直连，无中间层）
```

**Rejected**：
- ❌ 继续把所有请求压进 muxcp + 写 SessionStart hook：只救 Claude Code
- ❌ 等 muxcp upstream 加 alias/hide：时间不可控
- ❌ 只缩短 muxcp server name：化妆，没解决"所有工具藏在 muxcp 下面"的认知问题
- ❌ Fork muxcp：维护负担大，且不解决多客户端 namespace 扁平化问题

**理由**：
- **真正修复 discoverability**：客户端看到独立 server（`obs` / `mem` / `pw` / `kg` / `think`），不是 muxcp 大杂烩
- **跨客户端均衡受益**：Codex 不再吃截断 hash，Cursor 不再依赖描述长字符，Claude Code 仍可保留 hook 兜底
- **不依赖 upstream**：muxcp maintainer 加不加 alias 都不影响
- **可逆**：generator 仍输出 `muxcp/current.yaml`，哪天 native 踩坑能切回 muxcp
- **架构 client-agnostic**：中立 schema 让 muxcp 变成"和 cursor/codex 同级的输出目标"，未来加 LM Studio / Continue.dev 都按同一套配置生成

---

**前置验证（Gate to Locked）**：

✋ 投资 generator 之前必须做 Codex native 实测。**三项任一不通过 → 本决策回到 Re-evaluate，不进入实施**：

| # | 验证点 | 通过标准（可观察） |
|---|---|---|
| 1 | **多 server 支持** | Codex 配置文件能同时挂 2-3 个独立 MCP server，所有 server 在工具列表里全部可见 |
| 2 | **transport 混合** | stdio + SSE/HTTP 能在同一份 Codex 配置共存。**若不支持，generator 必须为 SSE/HTTP server 生成本机 stdio→SSE bridge 命令**——工程成本翻一倍，需 Re-evaluate 收益 |
| 3 | **触发率提升** | 多次同类请求（如"用 SLS 查最近错误"）连续触发正确工具，且工具名不再出现 hash 截断或显著减少 |

⚠️ **风险项 2 是最大不确定性**：Claude Code 已知支持 HTTP/SSE，Cursor 与 Codex 的 remote MCP 支持需实测。如果某客户端不支持 SSE/HTTP，generator 必须 emit 本机 stdio bridge 进程，而非直接写 URL——这条逻辑必须写进 generator。

---

**Validation Results（2026-05-21，Phase 1 实测）**：

详细报告：`/Users/mac/workspace_codex/muxcp-codex-native-validation-2026-05-21.md`

| Gate | 结果 | 证据 |
|---|---|---|
| #1 多 server 支持 | ✅ **PASS** | Codex ephemeral 同挂 `kg` + `seq` 两个 stdio MCP，均成功调用（`kg.kg_stats` 返回 `{entities:2734, edges:6407, episodes:1328}`） |
| #2 transport 混合 | ❌ **FAIL（协议错位，已识别根因）** | Codex `url=` 走 **streamable_http**；Aliyun MCP 只懂**经典 SSE**——握手失败 `Method Not Allowed`（错误由 Aliyun 服务端返回）。**这是协议错位，不是 Codex 缺远程 MCP 支持** |
| #3 工具名展示 | ✅ **PASS (stdio path)** | native 调用显示为 `server: kg, tool: kg_stats`，无 muxcp 长前缀、无 hash 截断 |
| Benchmark | ⏸ 未完成 | Gate #2 失败导致 native 模式无法覆盖 aliyun 类请求，Benchmark 因此未跑完整 30 条 prompt 集 |

**关键架构发现**：

`muxcp` 能正常服务 Aliyun，是因为它在内部承担了**协议适配**职责（经典 SSE ↔ MCP 客户端通用协议）。这意味着：

- ❌ 不能简单"绕开 muxcp"——会丢失协议适配能力
- ✅ `muxcp` 角色应**重定义为"SSE/legacy 协议适配兜底层"**——而不是被淘汰
- ✅ 长期方案需要**独立的 SSE bridge 设计**（详见 Phase 3 评估）

**对实施计划的影响**：原计划"一次性 native generator"路径不可行；改为 **hybrid migration**（见下方"实施 Phase"）。

---

**Phase 2 Hybrid Validation Results（2026-05-21 ephemeral 实测）**：

详细报告：`/Users/mac/workspace_codex/muxcp-codex-hybrid-validation-2026-05-21.md`

| 路径 | 结果 | 证据 |
|---|---|---|
| `kg` native stdio | ✅ **PASS** | `{"server":"kg","tool":"kg_stats","status":"completed"}` |
| `seq` native stdio | ✅ **PASS** | `{"server":"seq","tool":"sequentialthinking","status":"completed"}` |
| `muxcp_fallback`（Aliyun SSE） | ✅ **PASS** | `aliyun_observability__sls_get_current_time` 返回 `{"current_time":"2026-05-21 22:50:31","current_timestamp":1779375031}` |
| `pw` native stdio | ⏸ 未单独测 | 同 stdio 协议预期可工作，**正式安装时一并确认** |
| `mcp_search` | N/A | 本期刻意不迁移（避免三路径冲突） |

**结论**：hybrid 架构**端到端验证通过**——native + fallback 在同一 Codex 会话共存且互不干扰。可进入"生产安装"阶段（更新 `~/.codex/config.toml`）。

**Phase 2 Production Smoke Test（2026-05-22 正式配置）**：

详细记录：`/Users/mac/workspace_claudeCode/kg-hub/docs/muxcp-resolution-and-install.md`

| 路径 | 结果 | 证据 |
|---|---|---|
| `kg` native stdio | ✅ **PASS** | 调用 `kg.kg_stats`，返回实体数 `2848` |
| `seq` native stdio | ✅ **PASS** | 调用 `seq.sequentialthinking`，返回 `1+1=2` |
| `muxcp_fallback`（Aliyun SSE） | ✅ **PASS** | 调用 `muxcp_fallback.aliyun_observability__sls_get_current_time`，返回 `2026-05-22 13:56:48` / `1779429408` |
| `mcp-search`（claude-mem plugin） | ⚠️ **INCONCLUSIVE** | 新会话能看见并尝试调用，但返回 `user cancelled MCP tool call`；不计入 hybrid 核心路径失败 |

**结论**：Phase 2 hybrid 核心路径生产冒烟通过，2026-05-22 起进入 1 周观察期。非阻塞噪声（Cloudflare `403` plugin sync、`claude-mem/SKILL.md` frontmatter、非法 UTF-8 env panic）另行跟踪。

---

**中立 Schema 草案**（最小版，遵循 YAGNI）：

```yaml
# /Users/mac/public-sync/cc-switch-sync/mcp/source.yaml （WebDAV 同步）
schema_version: 1
servers:
  - id: obs
    display: Aliyun Observability
    transport: sse
    url_ref: aliyun_observability_url   # 引用 local.yaml 的 key，不存明文
    clients: [codex, cursor, claude]

  - id: kg
    display: kg-hub
    transport: stdio
    command_ref: kg_hub_command
    clients: [claude, codex]

  - id: pw
    display: Playwright
    transport: stdio
    command: npx
    args: ["-y", "@playwright/mcp"]
    clients: [cursor]
```

```yaml
# ~/.config/ai-mcp/local.yaml （不进 WebDAV）
aliyun_observability_url: "http://192.168.10.113:18081/sse"
kg_hub_command: "/Users/mac/.config/muxcp/bin/run-kg-hub.sh"
```

**Scope（明确边界）**：
- ✅ 字段：`id` / `display` / `transport` / `clients` / `url_ref` / `command_ref` / `command` / `args` / `env`
- ❌ **不包含 `tags:`**——当前无消费者，会腐烂。等真有 filter/index 需求再扩
- ❌ **不包含 muxcp 专有字段**——muxcp 是输出目标之一，不是特殊客户端

---

**目录结构（物理隔离 secrets）**：

```
WebDAV 同步根（公开内容）：
  /Users/mac/public-sync/cc-switch-sync/mcp/
  ├── source.yaml           # 中立 schema，全设备共享
  └── generator.py          # 生成器源码，进 WebDAV 同步（多设备一致性 > 冲突风险）

本机私有（不进任何同步）：
  ~/.config/ai-mcp/
  ├── local.yaml            # 本机 URL / 路径 override
  ├── secrets.env           # token / API key（永远不出本机）
  └── generated/            # 生成产物，集中存放后再安装/链接到客户端
      ├── cursor.mcp.json
      ├── claude.mcp.json
      ├── codex.config.toml
      └── muxcp/current.yaml

应用步骤（安装脚本）：
  generated/cursor.mcp.json   → ~/.cursor/mcp.json
  generated/claude.mcp.json   → ~/.claude.json（或 claude mcp add）
  generated/codex.config.toml → ~/.codex/config.toml
```

**关键原则**：
- **靠物理路径隔离 secrets**，不靠 ignore 规则或命名约定（WebDAV / Synology Drive 的 ignore 实现不统一）
- **generator.py 同步、生成产物不同步**——源码一致性 > 产物一致性（产物可重生成，源码不能）
- **generated/ 集中存放后再"安装"到客户端目录**——方便 diff 和回滚，避免直接覆盖客户端配置

---

**Secrets 解析时机**：

| Transport | 解析时机 | 实现 |
|---|---|---|
| stdio | **MCP 进程/wrapper 启动时从本机 env 读** | client config 只负责启动 command，**secrets 不进 client config**。由 wrapper 脚本或 MCP 自身 `os.environ.get(...)` 读取——不依赖客户端 env 替换 |
| SSE/HTTP | **Generator 时硬替换 URL** | URL 必须在配置里；不依赖客户端 env 替换。**生成文件 100% 在本机私有目录** |

---

**实施 Phase**（更新于 2026-05-21 Codex 验证后）：

| Phase | 内容 | 状态 | 预估 |
|---|---|---|---|
| 1 | Codex 前置验证 | ✅ **已完成 2026-05-21**（partial pass，详见 Validation Results） | — |
| 2 | **Hybrid migration**：stdio MCP（`kg` / `seq` / 可能 `playwright`）原生化；aliyun_observability 暂留**精简版 muxcp_fallback**（只挂 SSE servers） | 🟢 **Production installed + smoke tested 2026-05-22，1 周观察中** | 1 周观察 |
| 3 | **SSE bridge 独立设计**：评估四个选项（见下方表），选定后实施 | 待启动 | 0.5-1 工作日（评估） + 实施另算 |
| 4 | 完整 generator（**仅在 hybrid 跑稳后**）：source.yaml + 子命令体系（generate/validate/install/rollback/doctor） | 远期 | 3-5 工作日 |
| 5 | Cursor / Claude Code 迁移 | Phase 4 完成后陆续 | — |

**Phase 2 注意事项**：

- ⚠️ `mcp_search` 是否原生化**要谨慎**——当前已有 `plugin_claude-mem_mcp-search` 和 `muxcp__mcp_search` 两条路径，再加 codex native 会成三条，**容易混乱**。建议 Phase 2 阶段**不动 `mcp_search`**，等路径收敛策略明确后再处理
- aliyun_observability **暂保留 muxcp_fallback**，不直接配 Codex 远程 url（协议错位已确认）
- 落地顺序：**先生成 `codex.hybrid.preview.toml` 预览 → ephemeral `codex exec --ignore-user-config` 再验证 → 最后安装到 `~/.codex/config.toml`**——不要直接改正式配置

**Phase 3 SSE bridge 备选评估**：

| 选项 | 工程成本 | 优势 | 风险 |
|---|---|---|---|
| A. classic SSE → stdio bridge（本地小进程） | 中（自写 / 找现成方案） | 完全 native 化 aliyun，工具名干净 | 多一个进程要维护 |
| B. classic SSE → streamable_http bridge | 高 | 协议向新转型 | Codex/Cursor 未来都向 streamable_http 倾斜，长期收益 |
| C. 继续 muxcp_fallback | 0 | 已有方案 | 永远多一层间接 |
| D. 推动 Aliyun MCP 加 streamable_http endpoint | 不可控 | 根本解决 | 时间不可控，依赖上游 |

---

**升级到 Locked 的条件**（更新于 2026-05-21 Codex 验证后）：

1. 🟢 前置验证三项：#1 ✅ / #2 ❌→✅(hybrid fallback validated) / #3 ✅(stdio path)；**hybrid 路径全通过 2026-05-21**
2. ⏳ Phase 2 hybrid **生产安装后跑稳 1 周**（2026-05-22 已安装并冒烟通过，观察中）
3. ⏳ Phase 3 SSE bridge 选项选定并实施完成
4. ⏳ Phase 4 完整 generator 上线，无 secrets 泄漏事故
5. ⏳ 至少一个其他客户端（Cursor 优先）跟进迁移并跑稳 3 天

任一未达 → 维持 Proposed 状态。**Locked 条件预计 Phase 4 完成后才会全部满足**——这是个跨周决策，不是一次性动作。

**与既有决策的关系**：
- 决策 5（MCP 作为唯一客户端接入协议）：**不影响**，仍用 MCP 协议，只改投递方式
- 决策 7（Tailscale 内网）：**不影响**
- 决策 16（Mac IDE 手动写 `kg_add_episode`）：**间接影响**——本决策让 kg-hub MCP 不再经过 muxcp，对 #16 过渡态无冲突

---

## 4. KG Schema 草案 v0.2（含 OpenClaw 适配）

**注意**：这是**初版**，开干时第一步就是验证它。**预期会迭代**。v0.2 相对 v0.1 的变化：新增 `Capsule` / `KnowledgeDoc` / `Lesson` 节点；新增 5 条边以建模胶囊体系的实质关系。

### 实体类型（节点）

| 类型 | 例子 | 属性 | 来源 |
|---|---|---|---|
| `Person` | jingmiao@liblib.ai | name, org | 通用 |
| `Project` | claude-mem, kg-hub | path, repo | 通用 |
| `File` | HANDOVER.md, worker-service.cjs | path, project_id | 通用 |
| `Tool` | claude-mem worker, cc-switch, launchd | category, version | 通用 |
| `Concept` | qwen3.6-plus, 银行账单记账原则 | description | claude-mem + OpenClaw MEMORY.md |
| `Issue` | search 30s timeout, Cron 通知发送失败 | severity, status | 通用 |
| `Fix` | CLAUDE_MEM_CHROMA_ENABLED=false, notify-send.sh | applied_at | 通用 |
| `Config` | ~/.claude-mem/.env, plist | path, content_hash | 通用 |
| `Session` | claude-mem / OpenClaw 单次会话 | started_at, platform | 通用 |
| `Observation` | claude-mem 数据库里的原 obs | obs_id, source_device | claude-mem |
| `Capsule` 🆕 | CAPSULE-HOOK-SYSTEM-ARCH-2026-03-20 | id, title, type, tags, quality_rating, usage_count, source_session, content | OpenClaw |
| `KnowledgeDoc` 🆕 | feishu-image-upload-complete-guide.md | filename, category, path | OpenClaw |
| `Lesson` 🆕 | "银行数据视为 100% 正确" | summary, derived_at | OpenClaw / claude-mem |

### 关系类型（边）

| 关系 | 例子 | 来源 |
|---|---|---|
| `caused_by` | Issue → Concept (search timeout caused_by ChromaDB cold start) | 通用 |
| `fixed_by` | Issue → Fix | 通用 |
| `verified_by` | Fix → Observation / Capsule | 通用 |
| `references` | Observation → File / Concept | 通用 |
| `modified` | Observation → File | 通用 |
| `depends_on` | Tool → Tool / Config | 通用 |
| `supersedes` | Fix → Fix (新方案取代旧方案) | 通用 |
| `derived_from` | Concept → Observation / Lesson → Issue | 通用 |
| `occurred_in` | Observation → Session | 通用 |
| `belongs_to` | Session → Project / Device | 通用 |
| `extracted_from` 🆕 | Capsule → Session (胶囊从某会话提炼) | OpenClaw |
| `relates_to` 🆕 | Capsule ↔ Capsule (胶囊间关联) | OpenClaw |
| `documented_in` 🆕 | Concept → KnowledgeDoc | OpenClaw |
| `diagnosed_by` 🆕 | Issue → Capsule (问题在某胶囊中被诊断) | OpenClaw |
| `implemented_as` 🆕 | Capsule → Fix (胶囊方案落地成具体修复) | OpenClaw |

### 时间维度

每条节点 / 边都带 `created_at` 和 `source_obs_ids`（哪些原始 obs 推导出来的）。

### Provenance（来源追溯）

每个实体记 `first_seen_device`、`first_seen_obs_id`，便于回溯"这个事实哪台机什么时候学到的"。

---

## 5. 技术栈最终决定（v2，post-SPIKE）

| 组件 | 技术 | 理由 |
|---|---|---|
| KG 引擎（L3 核心） | **Graphiti** (Apache 2.0) | 决策 9（替代决策 2 的 Memgraph 自建路线） |
| 图后端 | **Kuzu**（embedded，Phase 1）→ 视规模迁 Neo4j / FalkorDB | 决策 9；embedded 避免起 Docker 维护成本 |
| 部署 | Mac / NAS 单进程（Phase 1）→ Docker on always-on（Phase 2+） | 决策 1, 7 |
| 网络 | Tailscale | 决策 7 |
| 中央 HTTP API | FastAPI (Python) | 仅用于 ingest `/api/ingest`，查询走 MCP |
| 中央 MCP server | **Graphiti 内置 MCP** (Python) | 决策 9（取消原"Node.js 自写 MCP"计划） |
| 设备端 push agent | Python + launchd / cron | 用户已熟悉 launchd 管理 |
| LLM 调用 | qwen3.6-plus via 百炼 Anthropic 协议端点 | 决策 4 |
| Embedding | fastembed BAAI/bge-small-en-v1.5（local，384-dim） | SPIKE 验证可行，免外部依赖 |
| 数据格式 | episode（自然语言 + 时间戳 + source）+ Cypher 查询 | 决策 10 |

---

## 6. 风险与未决问题

### 高优先级风险

1. **Schema 早期错误代价高**：第一周要把 v0.1 schema 用 100+ 真实 obs 跑一遍，看实体类型/关系是否够用
2. **实体消解质量**：qwen3.6-plus 抽实体时可能不一致（"qwen3.6-plus" vs "Qwen 3.6"），需要 embedding 相似度 / 别名词典兜底
3. **隐私泄露**：obs 含敏感路径，中央必须严格 Tailscale 内网，禁止公网暴露

### 待决策事项（开工后讨论）

- [ ] 多设备 obs 冲突如何解决？（同一事件在 A 和 B 都被观察到）
- [ ] 中央 KG 数据备份策略（Memgraph dump 频率）
- [ ] obs 删除 / 更新如何级联到 KG？
- [ ] schema v0.1 → v0.2 迁移机制
- [ ] 是否需要"撤销"操作（误推数据回滚）

### 已接受的限制

- ✅ 不做版本管理：obs 一旦推到中央，原则上 immutable（除非 obs 在 claude-mem 端被改）
- ✅ 不做精确实时同步：push 延迟可以是分钟级
- ✅ 单用户：不考虑多用户隔离
- ✅ 第一版只支持 Mac：Windows / Linux 后续再说
- ✅ **后台 ingester 调度间隔 (15 min) < 单次任务时长 (10 episodes × ~200s = 33 min)**：当前 `com.kg-hub.claude-mem-ingest.plist` 以 `StartInterval=900` 每 15 分钟尝试启动一次，但每次抓 writer.lock 后跑 LLM 抽取可能持续 30+ 分钟。后果：
    - launchd 在前一次未完成时跳过本次（macOS 默认行为，不会并发执行同一 LaunchAgent）
    - server 自身的 `/api/ingest` 在 ingester 持锁期间排队，超过 `timeout_seconds=180` 就 errored
    - 实测 2026-05-21：手动 `kg_add_episode` 撞上正在跑的 ingester，需换 `source_obs_id` 重投才能成功（详见 README "故障排查"）

  **当时为什么接受**：Phase 2 阶段写入流量低（人肉触发居多），偶尔失败重投即可。**何时重新审视**：写入流量稳定上来后（OpenClaw push 模式上线 / 多设备同步启用），需要从 file lock 改成内存队列或 Redis 队列，让后台任务和 API 写入排同一个队列，不互相阻塞。

---

## 7. 成功标准

**Phase 1 完成定义**：
- ✅ Memgraph 在 NAS 跑起来
- ✅ 能手动通过 HTTP POST 推一条 obs 进去并形成实体 + 关系
- ✅ 用 Cypher 能查到这条数据
- ✅ MCP 工具至少能 `kg_find_entity` 返回结果

**整个项目"足够好"定义**：
- 在任意设备的 Claude Code 里能问"我在所有项目里遇到过 chroma 相关的问题吗？"，KG MCP 返回跨设备跨项目的实体 + 因果链
- push agent 自动运行，3 台设备的 claude-mem 数据自动汇聚
- OpenClaw 胶囊增量同步进 KG，新胶囊产生 → kg-hub 自动入图
- 中央查询响应 < 200ms（P95）

---

## 8. 附录：OpenClaw 调研纪要（2026-05-14）

> 这是 Phase 0 数据源决策的依据，详细回答留存。

### A. OpenClaw 内部数据组成（用户提问 10 题，OpenClaw 自答）

| 数据类型 | 数量 | 字段 / 结构 | 价值评估 |
|---|---|---|---|
| graph-*.json | 4 个文件，8 节点 4 边 | Task + Session + EXECUTED_IN | ❌ trivial，无图谱价值 |
| 知识胶囊 markdown | 179 个（含归档） | id / title / type / tags / quality_rating / usage_count / source_session / content | ⭐⭐⭐⭐⭐ **主要价值源** |
| capsule-metadata.json | 179 索引 | hash / created / data 子对象 | ✅ 可作为入图主入口 |
| 知识库文档 | 36 个 markdown | filename / category | ⭐⭐ 辅助信息源 |
| MEMORY.md 概念条目 | ~20 条 | name / 内容 / 关联 / 确立时间 | ⭐⭐⭐ 富含隐式关系 |

### B. OpenClaw 自身承认的缺陷（kg-hub 要做得比它好）

1. **Provenance 弱**：graph JSON 只有 timestamp，胶囊 source 字段只是会话名，**没有可追溯的原始消息 ID**
2. **关系隐式化**：fixes / relates_to / diagnosed_by 等关系**全部用自然语言写在 markdown 正文里**，未抽出为显式边
3. **无 immutable / 无 versioning**：只用 hash 去重 + timestamp 标时间，胶囊改了就改了，没有历史
4. **无图查询接口**：只能 grep markdown，无法做多跳查询

### C. OpenClaw 提供的真实因果链样例（证明 kg-hub 路径价值）

```
[Issue] Cron 通知发送失败
   ↓ caused_by
[Concept] 飞书 chat_id 硬编码分散 (60 处脚本)
   ↓ leads_to
[Issue] 投资晚报 → 战略规划群 (应为财务管家群)
   ↓ diagnosed_by
[Capsule] CAPSULE-NOTIFICATION-ROUTE-2026 通知路由统一配置系统
   ↓ implemented_as
[Fix] notification-route.db + notify-send.sh
   ↓ verified_by
[Result] 2026-03-20 实战演练通过 (quality 5.0/5)
```

这是 **5 跳因果链 + 6 个节点类型 + 5 种边**，正是 kg-hub 要让 AI 工具能查询的形态。

### D. 导出能力

| 数据 | 导出格式 | 难度 |
|---|---|---|
| 胶囊索引 | `capsule-metadata.json` 直接读 | ⭐ 简单 |
| 胶囊正文 | `notes/capsules/*.md` 全量 | ⭐ 简单 |
| 知识库文档 | `notes/knowledge-base/*.md` 全量 | ⭐ 简单 |
| MEMORY.md | 单文件，但需解析自然语言 | ⭐⭐⭐ 需 LLM |
| 隐式关系（fixes / relates_to） | 全部锁在 markdown 文字里 | ⭐⭐⭐⭐ 需 LLM 抽取 |

**结论**：Phase 0 能直接拿 metadata + markdown 全量，难点在"用 LLM 把胶囊正文里的隐式关系抽成显式三元组"。这恰好就是 Phase 0 要验证的核心能力。

---

## 已知局限（决策记录，2026-06-22）

### L1：`usage_count` 是**曝光代理**，不是**贡献度**

PUSH hook 每次注入胶囊时给 `usage_count +1`。这个数 measure 的是"被注入了多少次（曝光）"，**不是"是否真的帮到了输出（贡献）"**。两者本质不同且当前会被混用：

- **循环自证**：`usage_count` 由注入决定本身 +1，所以"用得多"只等于"排序器一直选它"（如修复前 DESIGN 靠 score=100 巧合霸榜，曝光爆表、贡献可能为 0）。
- **闭环未合**：`注入 → agent 是否读了 → 是否改变输出 → 是否有益`，一个反馈都没采。严格的"贡献度"在数据里**不存在**。
- **后果**：0-usage 不代表无价值（可能只是没被选中，如 INCIDENT-RETRO）；高 usage 不代表高贡献（可能每次被无视）。

**已做的对冲**：探索槽（见排序设计）不赌"高曝光=高价值"，强制给低曝光者试投——工程上已认怂，但**没假装能测真贡献**。

**专项解法**：贡献度的自动测量是一个独立课题，设计见 `docs/CONTRIBUTION-SIGNAL.md`。在它落地前，排序信号继续用"相关性 + 探索"，不要把 `usage_count` 当真贡献读。
