# Claude 取代网关模型做知识抽取：完整方案 + 可复用的模型对比方案

> 2026-10-17 定稿。基于三轮实测（[extraction-model-comparison-20261017.md](extraction-model-comparison-20261017.md)）。
> 相关：执行协议 [AGENT-EXTRACT-PROTOCOL.md](AGENT-EXTRACT-PROTOCOL.md)、复盘协议 [DIGEST-RETRO-PROTOCOL.md](DIGEST-RETRO-PROTOCOL.md)、
> 存量画像 [gateway-model-quality-20261017.md](gateway-model-quality-20261017.md)。

## 第一部分：Claude 取代网关模型的完整方案

### 1. 两条路径的本质差别

| | 网关路径（现状，refinery → /api/ingest） | 会话自抽取路径（新，Claude Code 会话 → /api/ingest_extracted） |
|---|---|---|
| 谁抽取 | graphiti 0.29 内部 4 个阶段各自调模型（node_extraction / node_resolution / edge_phase / attribute_phase），每条 6–7 次调用 | Claude 会话读观测正文，一次产出 schema v0.2 的实体/关系 JSON |
| 模型 | credvault 路由表决定（现 qianfan deepseek-v4-flash-0731） | 会话本身的模型（haiku / opus / fable），不经网关、不计网关账本 |
| 消解 | graphiti：向量召回 + 模型判定（dedupe_nodes / resolve_edge） | 服务端本地：精确同名 → 向量召回（≥0.95）→ 词面校验 + 类型守卫 |
| 纪律 | 靠提示词，边名合规 31–60% | 规则 + 服务端归一（schema 外边名 → relates_to 并保留原名），合规 100% |
| 每条成本 | 约 7 次付费调用、4–5 分钟（含排队） | 会话 token（每批 20 条约 30–60k 字符输入 + 40–70KB 输出），服务端写图 0.6–0.9 s |
| 治理件 | 备份、IngestedKey、写锁、task_executions、origin/kind 打标、断路器 | 同一套（复用），只是没有模型账本行；断路器不管这条路（它不花网关的钱） |
| 来源标记 | 无（只能按月份猜模型） | episode / 新实体 / 新边带 `extractor_model`、`extraction_mode=agent`、`prompt_version` |

两条路对同一观测互斥：谁先建 IngestedKey 谁入库，后到者得到 `skipped`；refinery 每 10 分钟与图对账，把会话自抽取已入库的观测记为完成。

### 2. 组件清单（已上线，2026-10-17）

| 组件 | 位置 | 作用 |
|---|---|---|
| 预抽取入库接口 | `kg_hub_server.py` `POST /api/ingest_extracted`（`dry_run` 可选） | 校验/归一（`utils/extracted_ingest.normalize_extraction`）→ 备份 → IngestedKey → 写锁 → 嵌入/消解/写图 → origin/kind 打标 → task_executions |
| 批量查键 | `POST /api/ingest_extracted/check`、`POST /api/ingest/check_names` | 执行器跳过已入库；refinery 对账 |
| 写图与消解 | `utils/extracted_ingest.py` | 与 graphiti FalkorDB save 语句一致；消解策略见 §4 |
| 执行器 | `tools/agent_extract.py`（`next` / `submit [--dry-run]` / `status` / `prompt` / `retro`） | 按 refinery 水位取批（`tools/agent_extract_remote.py` 在 NAS 端筛）、提交、汇总、复盘转发 |
| 抽取规则 | `config/agent_extract_prompt.md`（agent-v4） | 13 实体类型、15 边名、命名/关系/复述/前瞻规则 |
| 复盘闭环 | `tools/digest_retro.py`（`gate` / `checkpoint` / `tune` / `final`） | 每 500 条门禁、指标、教训、调参登记、最终对比 |
| 对比工具 | `tools/extractor_compare.py` | 按 `extractor_model` / legacy 月份并排 |
| refinery 对账 | `kg_refinery.sync_graph_ingested` | 每 `KG_HUB_REFINERY_GRAPH_SYNC_SEC`（默认 600s）把图里已 ok 的 live 观测记为 ingested |
| 看板 | `/dashboard/flow` 「会话自抽取」分桶 | 入图 Episode 按来源分色；队列剩余随 refinery 对账下降 |

### 3. 运行手册（一轮 500 条）

1. 前置：断路器状态按需（两条路互斥，建议消化期间保持断开）；`KG_HUB_API_TOKEN` 在执行机的环境或 claude-mem/openclaw 配置里；执行机能 ssh NAS。
2. 启动执行会话（或子代理），固定 `MODEL` 与 `SESSION`，按 [AGENT-EXTRACT-PROTOCOL.md](AGENT-EXTRACT-PROTOCOL.md) 循环：
   `retro -- gate --every 500` → `next --n 20` → 读全文抽取（用 `lookahead`、`names-<model>.tsv`）→ `submit --dry-run` 看 `reuse_mismatch` 改名 → `submit` →（满 500）写 lessons → `retro --lessons-file … -- checkpoint`。
3. 复盘后由主会话决定调参：`retro -- tune --adjust prompt_version=… --adjust …`（旋钮见 `digest_retro.KNOBS`；服务端旋钮要发布）。
4. 轮末：`retro -- final` 得各轮指标与实践过的优化；`extractor_compare` 得与存量的并排表。
5. 回滚/止损：停执行会话即可；已入库数据带 `extractor_model`，可按标记定位（删除/拆分属图手术，需人工）。

### 4. 消解策略（服务端，经三轮收敛）

1. 同 group 内名字大小写/空白无关精确匹配 → 复用。
2. 否则 name_embedding 余弦 ≥ 0.95 召回前 5（按分数、uuid 定序），逐个过：
   - 词面校验：代码类名字（标识符/路径）与 File/Config 只认完全相等，例外是全路径与子路径在 `/` 边界上的后缀；只在数字上不同的不复用；
     其余要求去分隔符后一方包含另一方且占比 ≥ 0.8，或 difflib ≥ 0.85；
   - 类型守卫：候选必须带抽出的类型标签。
3. 都不过 → 新建节点（带 `extractor_model`）。
4. 边：同 (源, 边名, 目标) 已有 → 只追加 episode；否则新建（schema 外边名已在归一阶段映射为 relates_to，原名存 `raw_name`）。
5. 执行者侧 `submit --dry-run` 预览消解结果，并错先改名（区分词放最前、`仓库:路径`、事件带描述）。

### 5. 推广路线（建议）

| 阶段 | 内容 | 判据 |
|---|---|---|
| A 已完成 | 三模型各 500 条，规则 v1→v3，服务端消解三次收紧 | 合规 100%、误并率 0.15、入库误并 1 对 |
| B 受控对比 | 同一 100 条同源观测，用 v3 工具链分别跑 opus / haiku / fable（不换规则） | 把模型效应与规则效应分开；选成本最低且误并 ≤ fable 的 |
| C 主力切换 | 选定模型跑剩余 live 积压（约 12.5k）；每 500 条复盘；refinery 保持断开 | 每轮误并 ≤ 2 对、零边 ≤ 1%、复述折叠率可见 |
| D 常态化 | 新观测每日一轮（≈ 500–900 条/天）；或把执行器挂成定时作业 | 看板「会话自抽取」线稳定；队列剩余不增长 |
| E 网关路径定位 | 网关路径保留给非会话场景（OpenClaw 胶囊、task-hub 结晶、预拆分）并按第二部分优化 | 两路合规率都 ≥ 90% |

### 6. 风险与边界

- 会话自抽取的质量随模型与规则版本变化，必须保留 `prompt_version` 并在模型切换边界改规则，否则无法归因。
- 跨批被推翻的判断只能靠前瞻（下一批标题）与追加 supersedes 边；没有「修改已入库节点」的接口（建议项）。
- 同一会话对同一文件的反复再读占观测的 20%+，是密度与成本的主要浪费；折叠应在 claude-mem 侧或取批阶段做。
- 执行器用 ssh 读 NAS 上的 claude-mem.db 副本，依赖 Mac→NAS 15 分钟同步；写入只经 kg-hub 接口。

## 第二部分：可复用的模型对比方案

### 1. 设计原则

- **一次只变一个因素**：换模型时不改规则/服务端；改规则时不换模型。本次三轮同时换了两样，只有服务端改动能单独归因（教训）。
- **同源样本**：用同一批观测（如 `docs/ab-sample-20261017.json` 的 50 条分层样本，或任意 100 条 id 区间）跑各模型，写进主图时用 `extractor_model` 区分，或写进独立图（`tools/ab_score.py --database kg_hub_ab`）。
- **指标口径固定**：图口径（`extractor_compare` / `digest_retro`）与提交口径（`agent_extract status`）分开报；两者的「脏命名」定义不同，报表要注明。

### 2. 指标

| 维度 | 指标 | 工具 | 目标 |
|---|---|---|---|
| 纪律 | 边名合规率、边名种类、无类型实体、脏命名、重复三元组、零边 | extractor_compare / digest_retro | ≥ 90% / 越少越好 / 0 / ≤ 2% / 0 / ≤ 1% |
| 丰富度 | 实体/条、边/条、摘要均长 | 同上 | 不低于存量一半（≥ 6 / ≥ 6 / ≥ 200 字） |
| 消解 | reuse_mismatch/条、确定误并数（人工抽 20 条） | agent_extract status + 执行者抽查 | ≤ 0.2 / ≤ 2 对每 500 条 |
| 一致性 | 跨批被推翻判断入库数、同一对象矛盾 fact 数 | 执行者 lessons | 0 |
| 成本 | 每条 token（会话侧估）、服务端写图时延 | 执行者估 + status | 越低越好 |
| 可检索性 | kg_eval 召回（`tools/kg_eval.py`，固定问题集） | kg_eval | 不低于存量 |

### 3. 步骤

1. 固定规则版本与服务端版本（记 commit）；选样本（同源、分层）。
2. 每个模型一个执行会话/子代理，固定 `MODEL`、`SESSION`，`--every` 设为样本数；全部走 dry-run → 正式提交。
3. 跑 `extractor_compare`（按 `extractor_model` 并排）、`agent_extract status`、`retro -- final`；人工抽 20 条判实体类型与关系正确率。
4. 写对比报告：并排表 + 每模型最差三类错误 + 可单独归因的改动；判据按 §2 的目标列。
5. 决策：合规与误并达标的前提下取成本最低者；不达标的先改规则/服务端再比，不急着换模型。

### 4. 本次实测基线（供下次对照）

| 模型 | 规则 | 实体/条 | 边/条 | 合规 | 误并率 | 摘要均长 |
|---|---|---|---|---|---|---|
| claude-haiku-5-5 | v1 | 4.0 | 2.8 | 100% | 0.76 | 33 |
| claude-opus-5-5 | v2 + 类型守卫 | 4.1 | 3.4 | 100% | 0.35 | 75 |
| claude-fable-5-1 | v3 + 消解收紧 + dry-run | 5.6 | 4.3 | 100% | 0.15 | 166 |
| deepseek-v4-flash（网关，10 月存量） | graphiti 提示词 | 12.4 | 12.7 | 36% | — | 398 |
| qwen3.6-plus（网关，8 月存量） | graphiti 提示词 | 9.5 | 9.8 | 60% | — | 493 |
