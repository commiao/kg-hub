# 网关模型与 live 积压：消化速率、每条耗时与模型对图质量的影响（2026-10-17 只读核查）

> 本文只做只读核查，没有发起任何模型调用、没有动断路器、没有写图。
> 数据取自 NAS 上的 refinery 状态卷、claude-mem.db 副本、model-attempts.sqlite3（低峰 01:40 CST，只走索引）、gateway-usage/usage.json、FalkorDB 只读 cypher、credvault 生产路由表 git 历史。
> 任务登记：T-0195（积压消化问题排查）。

## 0. 结论先行

1. **「网关模型能力不行」这个判断，一半对一半不对。** 当前 `kg_hub.entity_extract` 路由绑的是 qianfan 的 `deepseek-v4-flash-0731`（10-06 起；DESIGN.md 决策 4 写的 qwen3.6-plus 已过时）。它**抽得更多**（每条 episode 实体 12.6 个、边 9.8 条，比 8 月 qwen 时代多 30–40%），但**纪律更差**：边名合规率 36%（8 月 qwen3.6-plus 时代 60%），自创边名从 3.4k 种涨到 15.7k 种，实体摘要变短（398 字 vs ~500），commit 号/绝对路径/任务号当实体名的比例翻倍。样本里能看到明显的类型错配（`commit 94a9779` 标成 Tool、`origin/main` 标成 Project、`github.com` 标成 File）和重复边。
2. **真正卡住消化的不是模型质量，是终态可靠性。** 10-09 之后账本里 succeeded 只占终态的 ~35%，其余是 `business_result_missing`（结果未知 → 待核验）。断路器 10-12 自动跳闸的原因正是「15 分钟内 5 条结果未知」。模型抽得好不好，在这之前根本到不了图里。
3. **积压不是 5000，是 14,106 条 live 待处理 + 2,790 条 live 待核验 + 927 条 backlog 待核验（≈17.8k）。** 老 backlog 线只剩 2 条。
4. **按历史速率，恢复断路器后全量消化要 13–18 天**（9 月底 10 月初 backlog 窗口实测 1,000–1,400 条/天），前提是终态可靠性回到 9 月水平。
5. **「你（Claude）来消化」在会话里不可行，但可以作为 A/B 实验做**：加一条走 Anthropic 的业务 key，用同一套 graphiti prompt、写到独立 group_id，先跑 50 条和 deepseek 并排比。全量 17.8k 条约 12 万次调用、6–7 亿 token 量级，是否值得要看 A/B 结果和单价。

## 1. 现状数字（2026-10-17 01:37 CST 采样）

| 项 | 值 | 来源 |
|---|---|---|
| 断路器 `kg_hub.entity_extract` | **开（断开）**，10-12 自动跳闸：「15 分钟内 5 条任务结果未知进入待核验」 | refinery status.json |
| 工作窗口 | all_day | refinery-window.json |
| live 待处理 | **14,106** | queue-remaining.sqlite3 最新采样 |
| live 待核验（held） | 2,790 | 同上 |
| backlog 待核验（held） | 927 | 同上 |
| backlog 待处理 | 2 | 同上 |
| 水位 | ingested 17,874 / rejected 13,181 / held 3,717 | watermark.json |
| claude-mem.db 副本 | max id 48,900；游标 21,742 之后共 27,155 条（含会被闸拒的） | sqlite 只读 |
| 图库 | 20,046 episodes / 53,720 entities / 165,403 edges | kg_stats |
| 网关路由 | 全部业务 key → qianfan `deepseek-v4-flash-0731`，timeout 150s，max_tokens 8192 | /run/model-gateway/config/routes.json |

live 待处理 27k 里按项目：kg-hub 6.2k、credvault 5.2k、workspace_claudeCode 3.1k、fleet-ops 2.9k、workspace_codex 2.7k；按类型 discovery 占 54%。

## 2. 消化速率与每条耗时

### 2.1 日吞吐（网关调用数 → 折算观测条数，÷6.8 次/条）

| 日期 | kg_hub.entity_extract 调用 | 折算观测 | 当时路由 |
|---|---|---|---|
| 09-29 | 6,637 | ~980 | qwen3.8-flash |
| 09-30 | 8,322 | ~1,220 | qwen3.8-flash |
| 10-01 | 9,665 | ~1,420 | qwen3.8-flash |
| 10-02 | 8,925 | ~1,310 | qwen3.8-flash |
| 10-03 | 7,947 | ~1,170 | qwen3.8-flash |
| 10-04 | 7,817 | ~1,150 | qwen3.8-flash |
| 10-05 | 7,275 | ~1,070 | qwen3.8-flash |
| 10-06 | 2,679 | — | **切 deepseek**；账本当日 3,407 failed + 2,895 uncertain |
| 10-08 | 5,374 | ~790 | deepseek |
| 10-09 | 6,719 | ~990 | deepseek（succeeded 155 / uncertain 392 / failed 15） |
| 10-10 | 1,748 | ~260 | deepseek（succeeded 97 / uncertain 10 / failed 9） |
| 10-12 | 1,153 | ~170 | deepseek；当日断路器跳闸 |
| 10-13 ~ 10-16 | 18 / 251 / 0 / 0 | — | 断开，仅人工补偿 |

（折算系数 6.8 来自 2.2 节；9 月数据只有网关计数没有账本终态，折算是上界。）

### 2.2 每条观测的耗时与调用结构（账本 task_executions 中 289 条 succeeded，10-09 ~ 10-14，deepseek 时代）

| 指标 | 值 |
|---|---|
| 每条 wall time | 中位 **271s**，p90 457s |
| 每条模型调用数 | 中位 6，均值 6.8，最多 43；分布：6 次 32%、5 次 20%、7 次 12%、8 次 7% |
| 单次调用耗时（prepare→complete） | 各阶段都在 **30–32s 中位，p90 43–48s** |
| 调用按阶段 | edge_phase 38%、attribute_phase 32%、node_extraction 15%、node_resolution 14% |
| 耗时按阶段 | edge 34%、node_extraction 30%、attribute 24%、resolution 13% |
| 结果 JSON 中位大小 | ~1.1 KB |

要点：
- 一条观测 ≈ 7 次串行调用 × 30s ≈ 4.5 分钟。`SEMAPHORE_LIMIT=1` 串行是有意为之（避免并发 429），所以单条 wall time 的下限就是模型延迟 × 调用次数。
- **边和属性两个阶段吃掉 70% 的调用、58% 的时间**，但它们正是 deepseek 时代质量最差的部分（边名乱、摘要短）。这是最明显的「换模型/砍阶段」杠杆。
- qwen 时代的单条耗时账本里没有（task_executions 10-09 才开始记 succeeded），只能引用 T-0195 的隔离探针：容器内单次 22–26s，业务段 11–13s。量级与 deepseek 相当，不能据此说哪个更快。

### 2.3 终态分布（10-09 起）

| 日期 | succeeded | uncertain | failed |
|---|---|---|---|
| 10-09 | 155 | 392 | 15 |
| 10-10 | 97 | 10 | 9 |
| 10-12 | 12 | 0 | 15 |
| 10-13 | 5 | 0 | 9 |
| 10-14 | 20 | 3 | 31 |

uncertain/failed 的原因几乎全是 `business_result_missing`（405 + 97）。这是「供应商/网关没给出可用终态」，不是模型抽错。10-12 之后的 failed 中位 0.2s，是断路器 preflight 直接拒绝，不是模型问题。

## 3. 模型时代与图质量画像（FalkorDB 按 episode/edge/entity 的 created_at 月份）

路由时间线（credvault `config/routes.nas.json` 历史 + kg-hub 代码注释）：
- 2026-06 ~ 07：qwen3.6-plus，百炼直连
- 2026-08：qwen3.6-plus，经网关（bailian）
- 2026-09：混合。09-10 前后 qianfan 在用（breakers.py 注释「qianfan 恢复后」），09-20 ~ 09-28 路由表是 qwen3.8-flash
- 2026-10-06 起：qianfan deepseek-v4-flash-0731

| 月份 | episodes | 实体/episode | 边/episode | 边名合规率（大小写不敏感） | 不同边名数 | 重复三元组 | 实体有类型 | 摘要均长 | 摘要<40 字 | commit/绝对路径/T-号 当实体名 | ≥20 实体的 episode |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 06 | 2,259 | 9.2 | 5.8 | 46.6% | 2,177 | 6.8% | 93.1% | 611 | 1.2% | 1.9% | 5.9% |
| 07 | 1,155 | 8.9 | 5.6 | 38.4% | 1,254 | 2.7% | 86.1% | 494 | 2.4% | 1.9% | 6.8% |
| 08 | 5,064 | 9.6 | 7.1 | **60.0%** | 3,364 | 4.2% | 82.9% | 493 | 0.8% | 2.5% | 7.2% |
| 09 | 4,724 | 11.7 | 9.1 | 30.7% | 10,991 | 2.8% | 83.0% | 510 | 1.5% | 3.9% | 13.1% |
| 10 | 6,621 | 12.6 | 9.8 | 36.3% | **15,706** | 3.6% | 84.1% | **398** | 2.5% | **4.3%** | **16.4%** |

读法：
- **数量上 deepseek 更「能抽」**：实体和边都比 8 月多 30–40%，≥20 实体的 episode 从 7% 涨到 16%。
- **纪律上 deepseek 更差**：schema 里 15 个边名，8 月 60% 的边落在 schema 内，10 月只有 36%；边名种类爆到 1.57 万（样本里见到 `UNREACHABLE_AT`、`HAS_ROUTES_IN`、`IS_DESCENDANT_OF`、`BINDS`、`EXPOSES`、`BELONGS_TO_PROJECT`）。这对检索是实打实的伤害：`kg_search` 的边检索和 `normalize_edge_names` 都按 schema 名工作。
- **实体命名更脏**：commit 短哈希、`/Users/...` 绝对路径、`T-0xxx` 任务号被当成独立实体的比例翻倍；摘要变短；样本里有 `claude-mem worker` 摘要直接是 `... placeholder`。
- 混杂因素：9–10 月 refinery 同时在吃 backlog（老观测），prompt/schema 代码也有过改动，所以这是相关性不是因果。但 8 月→10 月同一套 graphiti 0.29 + 同一 schema，模型是最大的变量。

### 3.1 样本对比（同为 claude-mem 观测，各取 3 条，摘要）

- **08 月（qwen3.6-plus）obs-5361「sd_generate 无 DDL」**：8 个实体（sd-server/Tool、MyBatis/Tool、4 个 mapper 文件/File、sd_generate table/Concept），13 条边全部是 schema 名（belongs_to/references/relates_to）。有一条错边（mapper XML `belongs_to` docs/design/sql/），但结构干净，fact 可读。
- **08 月 obs-5360「免费额度调查」**：12 个实体，14 条边，全部 belongs_to/relates_to；`13.6.0` 被标成 Project 是一处类型错配。
- **10 月（deepseek）obs-29995「T-0167 前提证伪」**：17 个实体、27 条边。边名 13 种里 9 种不在 schema；`IS_ANCESTOR_OF`、`DEFINED_IN` 各出现两次（同一三元组重复）；`commit 94a9779` 标 Tool、`origin/main` 和完整 SHA 标 Project、`github.com` 标 File、`/health/ready` 标 Tool；还有 `github.com -[UNREACHABLE_AT]-> codex` 这种把一次 DNS 失败固化成长期事实的边。
- **10 月 obs-22757「session_summaries 约束」**：4 个实体 0 条边（关系没抽出来）。
- **10 月 obs-22756「forwarder 健康」**：1 个实体，摘要是 `... placeholder`。

## 4. 上游 observation 层（claude-mem 也走同一路由）

claude-mem 生成 observation 的模型同样在网关上被切到 deepseek-v4-flash（`claude_mem.observation`）。claude-mem.db 副本按 `generated_by_model` 统计（id>5000）：

| 模型 | 条数 | 期间 | 畸形 type | 空 facts | 平均 facts | 平均 narrative |
|---|---|---|---|---|---|---|
| qwen3.6-plus | 14,702 | 06-15 ~ 08-24 | 0.0% | 3.7% | 4.5 | 448 字 |
| 网关（qwen3.8-flash → deepseek） | 29,266 | 09-02 ~ 10-16 | 1.5%（9 月 408 条，10 月 0） | 2.1% | 5.9 | 656 字 |

上游层 deepseek **不比 qwen 差**，甚至更饱满；9 月那批 `[ discovery ]` 之类畸形 type 10 月已经没有了（kg-hub 侧 929dab8 也做了归一化兜底）。所以「模型不行」的问题集中在 KG 抽取这一层的**结构化纪律**（边名、类型、去重），不在文本生成能力。

## 5. 不同模型对知识库数据的影响：分析与建议

### 5.1 影响机制

| 维度 | 影响 | 当前证据 |
|---|---|---|
| 召回量 | 强模型/「话多」模型抽更多实体和边，图更密 | deepseek 实体 +31%、边 +38% |
| 检索可用性 | 边名不落 schema → 边检索、别名归一、kg_eval 的边路径全部打折 | 合规率 60% → 36%，边名种类 ×4.7 |
| 实体消解 | 类型标错、哈希/路径当实体 → 同一事物多节点，跨项目归一失效（DESIGN 痛点 2） | 脏命名 2.5% → 4.3%，样本类型错配多处 |
| 事实时效 | 把瞬时状态固化成边（DNS 失败、队列深度 5） | 样本 `UNREACHABLE_AT` |
| 成本/时延 | 调用次数由 graphiti 阶段决定，与模型无关；单次延迟决定每条 4–5 分钟 | 各阶段 30s 中位 |
| 可靠性 | 供应商终态不可用 → 待核验堆积 → 断路器跳闸 | 10-09 uncertain 392 vs succeeded 155 |

### 5.2 建议（按优先级）

1. **先修可靠性，再谈模型。** 断路器恢复前，先搞清 `business_result_missing` 在 deepseek 路由下为什么占到 ~70%（是 150s 路由超时、是 qianfan 流式中断、还是网关幂等记录）。这一步归 T-0285/T-0242 的既有流程，本文不重复。
2. **做一次受控 A/B 再决定换不换模型。** 用同一批 50 条 live 积压观测（按项目分层抽样），三条路由并排：deepseek-v4-flash（现状）、qwen3.6-plus/3.8（历史基线）、Claude（新增业务 key，例如 `kg_hub.entity_extract_ab`，写独立 group_id 不污染主图）。指标就用本文第 3 节那几列：边名合规率、不同边名数、重复三元组、实体类型正确率（人工抽 20 条）、脏命名率、摘要长度、每条调用数与耗时。50 条 × 7 次 ≈ 350 次调用，任一模型都在日额度内。
3. **分阶段用不同模型。** 调用 70% 在 edge/attribute 两阶段，质量问题也集中在这两阶段。可以 node_extraction + edge 用强模型，attribute/summary 用便宜模型，或干脆把 attribute_phase 关掉（摘要本来就在变短变水）。graphiti 的 `ModelSize` 已经有 small/medium 分路的口子。
4. **不管换不换模型，入图侧加硬约束。** 边名不在 schema 的直接映射到 `relates_to` 并记录原名（`normalize_edge_names` 已有逻辑，改成入图时做而不是事后清）；实体名匹配 commit 哈希/绝对路径/任务号的降级为属性而不是节点。这一步与模型无关，对存量和增量都有效。
5. **全量用 Claude 消化的成本量级**：17.8k 条 × 6.8 次 ≈ 12 万次调用，按单次 prompt 4–6k token 估 5–7 亿输入 token、1 亿输出 token。是否划算取决于 A/B 结果和当下单价，本文不替用户算钱。

### 5.3 对「你来消化」的直接回答

在本会话里逐条消化 1.78 万条不可行（上下文和时间都不够，也绕过了治理写入通道）。可行路径是上面第 2 条：Claude 作为网关的一条路由参加 A/B，用同一套 graphiti prompt、同一条 `/api/ingest` 通道，先 50 条，有结论再决定要不要把 live 线切过去。

## 6. 方法与局限

- 每条耗时只有 10-09 之后的数据（task_executions 从那天起记 succeeded），qwen 时代没有可比的逐条账本。
- 图质量按 `created_at` 月份分桶，9–10 月混有 backlog 老观测；模型切换日期来自路由表 git 历史，09-20 之前只有代码注释佐证。
- 「边名合规率」按 schema 15 个名大小写不敏感匹配；「实体有类型」按 schema 13 个标签；「脏命名」用前缀规则（`commit `、`/Users/`、`T-0`），是下界。
- 样本对比各 3 条，是定性印证不是统计。
- 没有读取 7.3 GB 账本的非索引列，没有全表扫描；FalkorDB 查询均为聚合只读。
