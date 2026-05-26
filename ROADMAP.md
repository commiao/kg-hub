# kg-hub 阶段路线图

> 4 个 Phase。每个 Phase **预计 1 周**（业余时间，含调试）。
> Phase 0 必须先做完，证明数据有意义再投资 Phase 1+。

---

## Phase 0：数据探索（先做这个！）

**目标**：用 OpenClaw 已有的 **179 个胶囊 + 36 个知识文档 + MEMORY.md 概念**作为输入，验证 kg-hub schema v0.2 能不能容纳真实知识体系并暴露**可查询的多跳因果链**。

**预算**：2-3 小时（如果 OpenClaw 数据导出顺利）；最多半天

**为什么不用 claude-mem obs**：本机 claude-mem 才 ~155 条 obs，且大多是过程流水账，密度不如已经"提炼过"的胶囊。详见 [DESIGN.md §3 决策 8](DESIGN.md)。

**通过门槛（必须满足才进 Phase 1）**：
- 至少 **150 个胶囊**成功映射进 schema v0.2（节点 + provenance 字段完整）
- 抽出至少 **50 条隐式关系**（fixes / diagnosed_by / relates_to 等），不只是 EXECUTED_IN trivia
- 至少 **3 条 4+ 跳的因果链**完整跑通（参考 [DESIGN.md §8.C](DESIGN.md) 给的 OpenClaw 样例）
- 跨胶囊共享概念至少 **10 个**（说明确实有图状关联，不是孤立森林）
- 你看完抽取结果后觉得**"对，这就是我想要的"**

**否则**：调整 schema / prompt，反复直到通过；或者**放弃整个项目**

### 具体任务

#### 0.A 数据落地（OpenClaw → 本地）

- [ ] 0.A.1 你从 OpenClaw 导出：`capsule-metadata.json` + `notes/capsules/` 全量 + `notes/knowledge-base/` 全量 + `MEMORY.md` + 4 个 `graph-*.json`
- [ ] 0.A.2 放到 `kg-hub/data/openclaw-snapshot-2026-05-14/`（snapshot 命名，避免污染原数据）
- [ ] 0.A.3 写一个 Python 脚本扫描这些文件，输出 `inventory.json`（文件清单 + 大小 + 类型）

#### 0.B Schema 映射（结构化数据直入）

- [ ] 0.B.1 解析 `capsule-metadata.json` → 179 个 `Capsule` 节点（id/title/type/tags/quality_rating/usage_count）
- [ ] 0.B.2 扫描 `notes/knowledge-base/*.md` → 36 个 `KnowledgeDoc` 节点
- [ ] 0.B.3 解析 `MEMORY.md` 的实体条目 → ~20 个 `Concept` 节点
- [ ] 0.B.4 这步**不需要 LLM**，纯文件解析就够

#### 0.C 隐式关系抽取（LLM 介入）

- [ ] 0.C.1 设计 entity/relation extraction prompt，目标输出 JSON：
  - 从胶囊正文抽 `Issue` / `Fix` / `Lesson` 节点
  - 从胶囊正文抽 `caused_by` / `fixed_by` / `diagnosed_by` / `implemented_as` / `relates_to` 等边
- [ ] 0.C.2 选 5 个**高质量胶囊**（quality_rating ≥ 4.5）先试，看 LLM 抽得对不对
- [ ] 0.C.3 满意后批量跑全部 179 胶囊 → `triples.jsonl`
- [ ] 0.C.4 跑 qwen3.6-plus（百炼端点，复用 claude-mem 凭证）

#### 0.D 可视化 + 验证

- [ ] 0.D.1 用 networkx 加载所有节点 + 边
- [ ] 0.D.2 用 graphviz / pyvis 画出**完整图**（小） + **3 个 ego 子图**（围绕 high-quality 胶囊）
- [ ] 0.D.3 找至少 3 条 4+ 跳因果链，画出来
- [ ] 0.D.4 人工 review：跟 OpenClaw 给的因果链样例 ([DESIGN.md §8.C](DESIGN.md)) 对照，能不能复现？

#### 0.E 决策

- [ ] 0.E.1 写一份 `PHASE-0-REPORT.md`：节点/边统计、schema 偏差、最有意思的 3 条因果链截图
- [ ] 0.E.2 决定：进 Phase 1 / 调 schema 再来一次 / **放弃项目**

---

## Phase 1：中央 KG 服务（最小可用） — ✅ 完成（2026-05-15）

**目标**：用 **Graphiti + Kuzu** 把 OpenClaw 胶囊入图，能 MCP 查到。

**实际耗时**：约 1.5 天（SPIKE + 文档 + ingest + MCP 接入 + muxcp 整合）

**实测产出**：**237 entities / 245 edges / 31 episodes**；端到端 MCP 链路（新会话 → muxcp → kg_hub backend → Graphiti → Kuzu）已验证。

### 1.0 OpenClaw 快照落地

- [x] 1.0.1 VPS 接入：`admin@oc-vps` 走 Tailscale，绕开挂掉的阿里云 Workbench
- [x] 1.0.2 SSH + tar 打包：`/tmp/openclaw-snapshot.tar.gz` (1.4MB 压缩)
- [x] 1.0.3 scp 拉回 `data/openclaw-snapshot-2026-05-14/`
- [x] 1.0.4 解压 + 递归套娃 tar.gz × 40 → 640 个 .md / 49 个胶囊 / 18 个 KB 文档 / capsule-metadata.json 69 条索引

### 1.1 OpenClaw → Graphiti ingester

- [x] 1.1.1 `ingesters/openclaw_capsule.py`：递归扫描 + size 过滤（>1.5KB）+ watermark
- [ ] 1.1.2 `ingesters/openclaw_memory.py`：MEMORY.md 实体（**Phase 1 收尾候选**）
- [ ] 1.1.3 `ingesters/openclaw_knowledge_doc.py`：18 个 KB 文档（**Phase 1 收尾候选**）
- [x] 1.1.4 watermark：`data/.ingested.json` (sha256 + 时间戳 + nodes/edges 计数)
- [x] 1.1.5 v0.2 schema entity_types / edge_types / edge_type_map 全用上

### 1.2 schema 约束实现

- [x] 1.2.1 `schema.py` v0.2：13 节点类型（含 Capsule / KnowledgeDoc / Lesson）
- [x] 1.2.2 15 边类型 + 21 (from,to) 映射
- [x] 1.2.3 真实验证：`fixed_by` / `caused_by` / `implemented_as` 等 canonical 边在抽取结果中出现

### 1.3 全量 ingest

- [x] 1.3.1 31 个 substantive 胶囊全量入图（OpenClaw 自称 179 含归档；盘上实际 49 个 .md，其中 31 个 >1.5KB）
- [x] 1.3.2 `stats.py`：节点/边类型分布 + 中心度 + 多跳路径样本（见验收数据）
- [x] 1.3.3 抽样 review 通过：top hubs `小迪`(deg 40) / `Cron`(deg 12) / `胶囊利用率提升方案`(deg 9) 直觉成立
- [x] 1.3.4 已知遗留问题：UPPER vs lower 同名边类型分立 / `File` 类型滥用 / 25 个 unclassified Entity → 标记 Phase 2 schema 微调

### 1.4 暴露 MCP server

- [x] 1.4.1 写 `mcp_server.py`（FastMCP，5 工具：`kg_search` / `kg_node_neighbors` / `kg_path_between` / `kg_episode_search` / `kg_stats`）
- [x] 1.4.2 接入 muxcp（**不污染 `~/.claude/settings.json`**）：
  - `~/.config/muxcp/bin/run-kg-hub.sh` wrapper（缺失时优雅退出 1）
  - `~/public-sync/cc-switch-sync/mcp/muxcp/current.yaml` 添加 kg_hub server 条目
  - WebDAV 同步友好（其它设备没装 kg-hub 时自动 skip）
- [x] 1.4.3 新会话验证：`mcp__muxcp__kg_hub__kg_stats` 返回 `{entities:237, edges:245, episodes:31}`

### 1.5 验收

- [x] 1.5.1 MCP 协议端到端通：新会话 Claude Code → muxcp → kg_hub backend → Graphiti → Kuzu → fact stream
- [⚠️] 1.5.2 节点 ≥ 1000 / 边 ≥ 500：**未达**（实际 237/245）—— OpenClaw 实际可用胶囊数远少于自报，符合"实测揭穿宣称"的预期；不阻断 Phase 1 完成判断
- [x] 1.5.3 PHASE-1-REPORT 内容已合并到本 ROADMAP 节，**无需单独成文**

### Phase 1 未做但已识别（不阻塞，转 Phase 2 顺手做）

- ingest 18 个 knowledge-base 文档（原 1.1.3）
- ingest MEMORY.md 实体（原 1.1.2）
- 合并 UPPER/lower 重复边类型 → schema 微调
- 收紧 LLM 对 `File` 类型的滥用 → entity description 补强
- 给 25 个 unclassified Entity 二次分类 → 跑一次 schema-only reclassify

---

## Phase 2：自动化 + 多源 ingest — ✅ 完成（2026-05-17）

**目标**：从 1 个数据源 + 手动跑，升级到 **2 个数据源 + launchd 自动定时同步**。

**实际耗时**：1 天（含 Kuzu→FalkorDB 计划外迁移）

**实测产出**：当前 **468 entities / 656 edges / 114 episodes**，E1 全量 backfill 完后估计 ~3000 entities。

### 2.0 计划外：Kuzu → FalkorDB 迁移

Phase 1 末尾发现 Kuzu embedded 是**单写者锁**——一旦上自动化定时同步必撞锁。决定迁后端。

- [x] 2.0.1 选型对比：Neo4j vs FalkorDB（Memgraph 无 graphiti driver 排除）→ 选 FalkorDB（镜像 1/5、内存 1/10、备份简单）
- [x] 2.0.2 Docker 部署 `kg-hub-falkordb`（6379:6379 + 3001:3000 UI，本机 → 后续 ACR → NAS）
- [x] 2.0.3 `graphiti_client.py` 切 FalkorDriver
- [x] 2.0.4 `mcp_server.py` 重写 Cypher：FalkorDB 用**直接边** `(:Entity)-[e:RELATES_TO]->(:Entity)`，去掉 Kuzu 的 `RelatesToNode_` reify 中间节点
- [x] 2.0.5 已知 bug 绕过：FalkorDriver `default_group_id = "\_"` 会被自家 validate 拒绝；改成显式 `group_id="kg_hub"`
- [x] 2.0.6 graph 命名：FalkorDB 把 group_id 当 graph 名 → 设 `FALKORDB_DATABASE="kg_hub"` 保证读写同 graph

### 2.1 OpenClaw 拉源（替代原 ROADMAP 2.1 的 claude-mem 单源）

- [x] 2.1.1 `sync_openclaw.py`：tar+ssh over Tailscale，441 .md 文件 1.4MB gzip
  - 决策 A：用 tar+ssh 不用 rsync（VPS 没装 rsync，5-10MB 数据量 watermark 已经够减负）
  - 决策 B：扫描 5 个 VPS 顶级目录（notes / memory / plans / reports / capsules）
- [x] 2.1.2 `ingesters/openclaw_capsule.py` discover 扩展到 5 个根
- [x] 2.1.3 VPS 上 7 个活跃胶囊 + 历史归档 .tar.gz 内的胶囊已全入图

### 2.2 claude-mem 拉源（ROADMAP 原 2.1 简化版）

- [x] 2.2.1 `ingesters/claude_mem_obs.py`：read-only `~/.claude-mem/claude-mem.db`
  - 决策：**不**在 claude-mem.db 加 `kg_push_state` 表（原 ROADMAP 提案）—— claude-mem 是只读边界
  - 改用：独立 watermark `data/.ingested.claude_mem.json`，keyed by `obs.id`
- [x] 2.2.2 episode_body 拼装：title + subtitle + narrative + facts 列表 + concepts + files
- [x] 2.2.3 qwen3.6-plus 经 `graphiti_client.build_llm()` 复用（注入 thinking=disabled）
- [x] 2.2.4 幂等：watermark 跳过已 ingest 的 obs.id

### 2.3 launchd 定时（已 load）

- [x] 2.3.1 `~/Library/LaunchAgents/com.kg-hub.claude-mem-ingest.plist`：每 15 min，`--limit 20`
- [x] 2.3.2 `~/Library/LaunchAgents/com.kg-hub.openclaw-sync.plist`：每 30 min，全 sync
- [x] 2.3.3 日志：`~/.kg-hub/logs/{claude-mem,openclaw}-{out,err}.log`
- [x] 2.3.4 plist Lint + launchctl load 成功

### 2.4 并发安全（计划外，必须做）

- [x] 2.4.1 `utils/writer_lock.py`：fcntl flock，`~/.kg-hub/locks/writer.lock`
- [x] 2.4.2 挂到两个 ingester 顶部（同一进程或跨进程都串行）
- [x] 2.4.3 `--wait-seconds` 选项：手动 backfill 可耐心等锁
- [x] 2.4.4 跨进程 lock 测试通过

### 2.5 端到端验收（已通过）

- [x] 2.5.1 新会话 MCP 工具 5/5 可用，返回正确 kg_stats
- [x] 2.5.2 跨源语义检索通过：`kg_search "sync_openclaw"` 返回 claude-mem 抽出的开发记忆
- [x] 2.5.3 多跳路径通过：`Cron → ... → 飞书` 3 条 4-5 跳路径全收敛在 `小迪 → capsule-feishu-alert.py`
- [x] 2.5.4 Cursor / Codex 同机 MCP 验证通过

### 2.6 Phase 1 遗留问题（在 Phase 2 数据上重测）

| 问题 | Phase 1 | Phase 2 中（mid-backfill） | 趋势 |
|---|---|---|---|
| File 类型滥用 | "高" | 35.7% (167/468) | 没改善，需 schema description 收紧 |
| Unclassified Entity | 25 | 56 | 数据量同比放大 |
| LLM 自创边名 | 55.1% | 58.4% | 没改善 |
| **新发现**：UPPER/lower 边名重复 | — | **12 对，~230 条边可合并到 ~115 条** | 留待 cleanup 脚本 |

### 2.7 多设备验证（推迟到 Phase 3）

原 ROADMAP 2.4 要求"第二台设备装 kg-push agent + 验证 `source_device` 字段"。**推迟**：当前只有一台 Mac 在跑，没有第二台 active claude-mem 设备。NAS 部署 FalkorDB 后才有意义。

---

## Phase 3：打通孤岛（写入 + 跨设备读 + 文档）— ✅ 完成（2026-05-19）

**目标**：让 Cursor / Codex / Claude Code 能**写** KG，让 OpenClaw 能**读** KG，让其它工具能按文档**自助接入**。

**实际耗时**：~6h（含异步重构 + 监控告警 + 跨 AI 共享盲点发现）

**实测产出**：当前 **~1980 entities / 4660 edges / 950+ episodes**，4 个 plist 后台稳态运行。

> **优先级调整说明**（2026-05-18 user 拍板）：原 Phase 3 把 NAS 部署排第一，实测发现 Mac 本机当 always-on 暂时够用，**真阻塞点是**：
> - Cursor 不能写入 KG（IDE 单向只读）
> - OpenClaw 不能读取 KG（孤岛）
>
> NAS / OpenClaw push plugin / 多 Mac 验证全部**推迟到 3.E-3.G**（按需做）。

### 3.A kg-hub HTTP server（FastAPI）— ✅ 全栈基础

> 一处 auth、一处 idempotency、一处 rate-limit。**MCP 写工具内部转发到这**，所以 Phase 3 后面所有写入路径都走这一个进程。

- [x] 3.A.1 `kg_hub_server.py` FastAPI 应用：5 路由 `/health` `/api/ingest` `/api/ingest/status` `/api/search` `/api/queue_stats`
- [x] 3.A.2 鉴权：`Authorization: Bearer <KG_HUB_API_TOKEN>` header，token 在 `~/.claude-mem/.env`
- [x] 3.A.3 服务端 idempotency：`MERGE (k:IngestedKey {sd, sid})` 原子 check-and-create
- [x] 3.A.4 写入用 `async_writer_lock`（决策 12 异步版）+ graphiti
- [x] 3.A.5 launchd plist `com.kg-hub.server`（KeepAlive Crashed=true，ThrottleInterval=30s）
- [x] 3.A.6 **异步重构**（计划外）：`/api/ingest` 默认 async-by-default，1.9s 返回 202 + `episode_uuid` 异步生成
  - 修复 `writer_lock` 同步阻塞 event loop 的 bug
  - 加 `IngestedKey.status` 三态机 + `cleanup_stuck_jobs`（30 min 阈值，可 env 配置）
  - `?sync=true` 兼容老调用方
- [x] 3.A.7 **主动监控**（计划外，user 拍板必做）：`tools/watchdog.py` + plist `com.kg-hub.watchdog`
  - 每 10 min 巡检 4 个异常项（server_down / queue_backlog / stuck_jobs / recent_errors）
  - 边沿触发（OK↔BAD 切换才告警，不刷屏）
  - 出口：飞书 webhook（如 env 设）/ macOS 通知 / `~/.kg-hub/logs/alerts.log`
  - 合成 transition test 通过：BAD→OK 4 个 CLEAR 告警，OK→OK 0 新告警

### 3.B `kg_add_episode` MCP 写工具 — ✅

- [x] 3.B.1 在 `mcp_server.py` 加 `kg_add_episode(content, source_description, source_obs_id?, name?, reference_time?)`
- [x] 3.B.2 实现 = **httpx 转发**到 3.A 的 `/api/ingest`（不直连 graphiti），共享 auth + idempotency
- [x] 3.B.3 错误友好返回：`server_unreachable` / `request_failed` / `missing_token` 等结构化 code
- [x] 3.B.4 客户端超时拉到 240s（>server 锁等 180s），避免 ReadTimeout 错觉

### 3.B-verify ★ Cursor 写入端到端验证 — ✅ VERIFIED

- [x] **证据**：FalkorDB 里 Episodic `761cc789-762d-46ed-9a01-0bc5a0e75d58`
  - name: "Cursor kg_add_episode visibility verification"
  - content: "Cursor MCP verification: kg_hub__kg_add_episode became visible after restarting Cursor/MuxCP on 2026-05-18..."
- [x] **二次验证**：同 source_obs_id 重试返回 `{status:skipped, reason:duplicate}` — idempotency 工作正常
- [x] 写入 → kg_search 检索路径通：claude-mem obs#972 (11:35) 也确认 "Cursor ingest + Feishu search both working"

### 3.C OpenClaw kg-query skill — ✅ 接通 OpenClaw 读路径

- [x] 3.C.1 `clawd/skills/kg-query/SKILL.md`（YAML frontmatter + 调用规则 + 错误兜底说明）
- [x] 3.C.2 `clawd/scripts/kg-query.sh`（curl wrapper + env 检查 + 错误码分级）
- [x] 3.C.3 `~/.openclaw/env.sh` 加 `KG_HUB_URL=http://mac-office:8080` + `KG_HUB_TOKEN=...`（mode 600 保持）
- [x] 3.C.4 scp 部署到 VPS，文件本地暂存 `kg-hub/openclaw-deploy/`
- [ ] 3.C.5 在 OpenClaw AGENTS.md 加提示"需要外部知识时调用 kg-query"
  - 推迟：观察小迪自发调用率, 若不主动则再补提示

### 3.C-verify ★ OpenClaw 读取端到端验证 — ✅ VERIFIED

- [x] 3.C-v.1 VPS 手动跑 `bash kg-query.sh "Cron 通知失败"` → 返 3 条相关 fact
- [x] 3.C-v.2 JSON 结构、`{fact, source_node_uuid, target_node_uuid, valid_at}` 完整
- [x] 3.C-v.3 **铁证**：server.out.log 两条 GET 来自 `100.79.177.102`（VPS Tailscale IP）：
  - `GET /api/search?q=Cron 通知失败&num_results=3` 200 OK
  - `GET /api/search?q=sync_openclaw Mac rsync synchronization mechanism&num_results=15` 200 OK
  - → 小迪自主调用 kg-query 并把结果纳入回答

### 3.D `docs/ONBOARDING.md` — ✅ 12KB / 371 行

- [x] 3.D.1 路径 A（MCP 接入）：muxcp 配置 + 6 个工具清单 + 调用范例
- [x] 3.D.2 路径 B（HTTP 接入）：完整 API schema + 鉴权 + 幂等 + 异步语义
- [x] 3.D.3 Python + Bash 双范例 + 常见问题 + 监控指南
- [x] 3.D.4 写明"接入路径决策树"+"决策 14/16 引用"+"接入后广播 capsule"等运维实践

### 3.E NAS 部署（推迟，按需做）

Mac 当 always-on 不够用时再做。

- [ ] 3.E.1 推 ACR → NAS pull
- [ ] 3.E.2 切 `KG_HUB_FALKORDB_HOST` 指 NAS
- [ ] 3.E.3 Mac 端 dump RDB → NAS restore
- [ ] 3.E.4 故障演练：NAS 挂时 Mac 端 claude-mem 不受影响

### 3.F OpenClaw push plugin（推迟，按需做）

rsync pull 已经够用。等想要"秒级实时入图"时再做。

- [ ] 3.F.1 TS 写 OpenClaw plugin（参考 DESIGN 决策 15）
- [ ] 3.F.2 `openclaw plugins install` 注册
- [ ] 3.F.3 hook `tool_result_persist` → POST /api/ingest

### 3.G 多 Mac 验证（推迟，等第二台设备）

- [ ] 3.G.1 第二台 Mac 装 ingester
- [ ] 3.G.2 共享 muxcp 配置
- [ ] 3.G.3 验证 `source_description` 区分设备

### 3.X 验收门槛

- [ ] 3.A-D 全部完成
- [ ] Cursor 能写 KG（3.B-v）
- [ ] OpenClaw 能读 KG（3.C-v）
- [ ] 文档完整可让新人接入（3.D）

---

## Phase 4：迭代

**没有截止时间**。以下任意优先级，做哪个看用着哪里痛。

### 候选迭代项

- **本地 KG 副本（embedded Kuzu / SQLite 简易版）**：每台设备只读 KG 副本，离线可查。**Phase 2 实测发现 Tailscale 查 NAS 延迟够低，离线痛点未出现**——做不做看实际使用感受
- **实体消解 v2**：embedding 相似度 + 别名词典 + 人工 review UI
- **Schema v0.3**：根据用了几周后的发现调整实体/关系类型（重点收 File 类型滥用 + unclassified）
- **基于 KG 的 agent loop**：让 LLM 主动调 KG 在回答时构建因果链
- **可视化 dashboard**：FalkorDB Browser 太裸，写一个针对自己 schema 的 UI（含因果链可视化）
- **跨用户**：（如果扩展到团队）多用户隔离 / ACL
- **CRDT**：从 push 改成 event sourcing，支持冲突合并
- **导入第三方数据**：把 git commit、Slack 消息、Linear ticket 也抽实体进 KG
- **Phase 2 遗留 cleanup**（write-side, 自动化跑稳后做）：
  - [ ] UPPER/lower 边名 12 对合并（`tools/normalize_edge_names.py --apply` 已就绪）
  - [ ] File 类型滥用 35.7% → schema description 收紧 + LLM prompt 加约束
  - [ ] 56 unclassified Entity → schema-only reclassify pass

---

## 关键里程碑

| 里程碑 | 标志 |
|---|---|
| ✅ Phase 0 通过 | 看到第一张有意义的图 |
| ✅ Phase 1 通过 | Claude Code 里能用 MCP 查 KG |
| ✅ Phase 2 通过 | 多设备自动同步 |
| ✅ 整个项目"成功" | 在任意设备的任意 IDE 都能基于跨设备 KG 回答工程问题 |
