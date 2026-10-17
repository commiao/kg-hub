# 网关抽取链路优化建议：提升 deepseek / qwen 的抽取结果

> 2026-10-17。对象是现有的 refinery → `/api/ingest` → graphiti 0.29 → model-gateway 路径（模型由 credvault 路由表决定，
> 现为 qianfan deepseek-v4-flash-0731；历史为 qwen3.6-plus / qwen3.8-flash）。
> 依据：存量画像 [gateway-model-quality-20261017.md](gateway-model-quality-20261017.md) 第 3 节；三轮会话自抽取里被证明有效的
> 服务端手段（[extraction-model-comparison-20261017.md](extraction-model-comparison-20261017.md) 第 2 节）；代码现状
> `graphiti_client.py` / `kg_hub_server.py` / `schema.py` / credvault `config/routes.nas.json`。

## 0. 先看病根（存量数据说话）

| 症状 | deepseek（10 月） | qwen3.6-plus（8 月） | 根因 |
|---|---|---|---|
| 边名不在 schema | 64% | 40% | graphiti 的 `edge_types` 只对 `EDGE_TYPE_MAP` 里列出的 (源类型, 目标类型) 对生效，其余对子模型自由命名；入图侧没有归一 |
| 自创边名种类 | 15,706 | 3,364 | 同上；deepseek 更「话多」 |
| 无类型实体 | 16% | 17% | 模型没给出 13 类之一时 graphiti 只打 `Entity`；没有兜底类型 |
| commit 哈希 / 绝对路径 / 任务号当实体 | 4.6% | 2.8% | 提示词没禁止；入图侧没有降级 |
| 重复三元组 | 3.6% | 4.2% | 边去重依赖模型判定 + 向量，长观测里同一关系反复出现 |
| 零实体 episode | 1.8% | 0.7% | 长观测在 4096 max_tokens 下被截断（客户端 `LLMConfig(max_tokens=4096)`，路由允许 8192） |
| 摘要变短 | 398 字 | 493 字 | attribute_phase 对 deepseek 产出更短，且这一阶段占 32% 调用 |

结论：**大半问题在链路而不在模型**。会话自抽取三轮证明，同样的 schema 加上「入图侧归一 + 消解守卫」就能把合规率做到 100%、
误并降到 0.15/条；这些手段大部分可以原样搬到网关路径。

## 1. 立刻可做、不碰模型（预计把合规率从 36–60% 拉到 ≥ 90%）

| # | 改动 | 位置 | 效果与证据 | 代价 |
|---|---|---|---|---|
| 1 | **入图侧边名归一**：add_episode 返回后，对本 episode 的边执行「名字不在 15 个 schema 名 → 改为 `relates_to`，原名存 `raw_name`」 | `kg_hub_server._do_extract_inner` 结果处理处；逻辑可直接复用 `utils/extracted_ingest.normalize_extraction` 的边名映射 | 合规率 → 100%（自抽取路径实测）；边检索与 `normalize_edge_names` 立即受益；`raw_name` 保留信息不丢 | 一次 cypher，零模型调用 |
| 2 | **实体名降级**：commit 哈希、绝对路径、纯任务号的实体改为所在 episode 的属性或并入其所属对象，不建节点 | 同上；判据用 `extracted_ingest.DIRTY_NAME_PATTERNS` | 脏命名 4.6% → 0；减少 Project/File 类型的垃圾节点 | 需定义降级目标（建议写进 episode 的 `mentions_raw`） |
| 3 | **兜底类型**：模型没给类型的实体按名字规则兜底（路径/扩展名 → File，`KG_HUB_*`/`.env`/`.json` → Config，其余 → Concept） | 同上 | 无类型实体 16% → 接近 0 | 规则简单，可测 |
| 4 | **消解守卫**：graphiti 的 dedupe 候选在进入模型判定前先过 `_lexically_same` + 类型守卫（已在 `utils/extracted_ingest`），把明显不同对象的候选剔除 | `utils/graphiti_parallel` 的读侧已有挂钩（`node_similarity` 读），在候选返回处过滤 | 自抽取路径实测误并 0.76 → 0.15/条，其中数字差异、跨仓库同名、跨类型三类归零 | 候选变少反而省模型调用 |
| 5 | **max_tokens 对齐路由**：`LLMConfig(max_tokens=4096)` → 8192（路由上限） | `graphiti_client.build_llm` | 零实体/零边 episode（1.8%）主要是长观测截断 | 单次调用更贵，但避免重试与空结果 |

这五条合起来就是「让网关路径拥有会话自抽取路径的服务端纪律」。建议先做 1、3、5（纯增量、可回滚），再做 2、4。

## 2. 提示词层（graphiti 支持，现在没用上）

| # | 改动 | 说明 |
|---|---|---|
| 6 | **`custom_extraction_instructions`**：graphiti 0.29 的 `add_episode` 接受自定义抽取指令，现在没传。把 `config/agent_extract_prompt.md` 的硬规定压缩成一段（只用 15 个边名、不把哈希/路径/任务号当实体、同一事物只出现一次、区分词放最前、关系 3–10 条、被推翻判断不入库）传进去 | 对 deepseek 这种「话多但不守 schema」的模型效果最直接；对 qwen 也适用。两处都要测 thinking 关闭（`thinking_policy: disabled` 已是路由默认，保持） |
| 7 | **补全 `EDGE_TYPE_MAP`**：现在只列了十几对类型组合，其余 (源, 目标) 对子没有约束。补上 `("Entity","Entity"): ["relates_to", "references", "depends_on"]` 兜底，并给高频对子（File→Project `belongs_to`、Tool→Config `depends_on`、Issue→Fix `fixed_by`、Lesson→Observation `derived_from`）补齐 | 这是 graphiti 自己的约束机制，让模型「选不到」自创名 |
| 8 | **实体类型 docstring 加反例**：`schema.py` 每个类型的 docstring 是模型看到的说明，加一句「不要把 commit 哈希 / 绝对路径 / 任务号当作 Project/File」 | 零成本 |

## 3. 成本与吞吐（不降质量的前提下）

| # | 改动 | 说明 |
|---|---|---|
| 9 | **attribute_phase 用小模型或关掉**：这一阶段占 32% 调用、24% 耗时，产出是实体摘要与可选属性；graphiti 对 `ModelSize.small` 走 `LLMConfig.small_model`，可以把 `small_model` 设为另一个业务 key（如 `kg_hub.entity_extract_small` → 更便宜的模型）；或把 `schema.py` 实体类型里的可选属性字段去掉，graphiti 就不再调 `extract_attributes` | 每条少 2 次调用（6.8 → ~4.8），成本约 −30%；摘要质量可用自抽取路径对照 |
| 10 | **并行任务数**：单次调用 30 s 中位、串行 7 次 = 4.5 分钟/条；`KG_HUB_PARALLEL_TASKS` 从 4 提到 6–8，配合网关 RPM 60 | 吞吐接近翻倍；注意 429 由网关队列吸收（T-0230 已有退避） |
| 11 | **源头折叠**：同一会话对同一文件/diff 的连续再读观测占 20–23%（第 3 轮实测），在 refinery 取批阶段按 (session, files, 标题相似度) 折叠成一条合并观测再送抽取 | 少 20% 调用，且减少近义节点；比换模型省得多 |
| 12 | **预拆分阈值**：长观测（> 6k 字符）走 predigest 拆成原子 observation 再抽（`KG_HUB_PREDIGEST` 已有机制，只对胶囊开） | 对 deepseek 的长观测截断与「一条 27 边」的过密抽取都有缓解 |

## 4. 路由与供应商参数

| # | 建议 | 说明 |
|---|---|---|
| 13 | deepseek-v4-flash：`temperature=0`、`top_p` 默认、`thinking_policy: disabled`（必须，forced tool_choice 需要）、`max_tokens 8192`；`max_input_chars 400000` 保持 | 网关路由字段可热切（看板 `APPROVED_ROUTE_SELECTIONS`）；温度在 graphiti `LLMConfig` 里设 |
| 14 | qwen：8 月 qwen3.6-plus 的合规率（60%）明显高于 9 月 qwen3.8-flash 混合期（31%）；若回到 qwen，优先 qwen3.6-plus（或当前套餐里同档的非 flash 型号），flash 档位的 schema 纪律明显差 | 需 bailian 凭证仍可用 |
| 15 | 两条业务 key 分流：`kg_hub.entity_extract`（node/edge 阶段，强模型）与 `kg_hub.entity_extract_small`（attribute 阶段，便宜模型） | 配合 #9；credvault 侧加路由（参考 #117 的做法），kg-hub 侧 `LLMConfig(small_model=...)` |
| 16 | 终态可靠性与模型无关：按 [business-result-missing-20261017.md](business-result-missing-20261017.md) 修账本口径与 held 守卫阈值，恢复断路器前先放回无计费的 absent 存量 | 否则统计会再次把限流/抖动读成「模型不可靠」 |

## 5. 怎么验证（每一条都能量）

- 指标与工具同 [extraction-model-comparison-20261017.md](extraction-model-comparison-20261017.md)：`tools/extractor_compare.py` 按月份/模型并排，
  `tools/digest_retro.py` 的窗口指标；网关路径入图的 episode 没有 `extractor_model`，可以在 #1 落地时顺手给 episode 打 `extractor_model=<路由模型>`
  （从网关 `/health/ready` 的路由投影读），之后新旧数据就能按模型直接分组。
- 顺序：先做第 1 节的 1/3/5（合规率、无类型、零边三个指标应立刻变化），再做 6/7（看自创边名种类是否降到两位数），
  再做 9/10/11（看每条调用数与吞吐），最后做 A/B（[CLAUDE-EXTRACTION-REPLACEMENT-PLAN.md](CLAUDE-EXTRACTION-REPLACEMENT-PLAN.md) 第二部分）
  把 deepseek / qwen / Claude 放到同一套链路纪律下比，那时比的才是模型本身。
- 目标线：边名合规 ≥ 90%、无类型实体 ≤ 2%、脏命名 ≤ 2%、重复三元组 ≤ 1%、零边 ≤ 1%、每条调用 ≤ 5 次。
