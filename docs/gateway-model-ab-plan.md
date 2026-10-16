# 实体抽取模型 A/B 与全量消化方案（2026-10-17）

> 目的：判断把 `kg_hub.entity_extract` 的路由从 qianfan `deepseek-v4-flash-0731` 换成其他模型（含 Claude）
> 是否值得。先用同一批 50 条 live 积压观测做受控 A/B，再按 [DIGEST-RETRO-PROTOCOL.md](DIGEST-RETRO-PROTOCOL.md)
> 的学习闭环做全量消化。现状数字与指标口径见 [gateway-model-quality-20261017.md](gateway-model-quality-20261017.md)。
>
> **本文只是方案，没有执行。** 写作过程没有发起任何模型调用、没有动断路器、没有写主图；
> 唯一的 NAS 操作是只读 claude-mem.db 副本与 refinery 水印做抽样。
> 实验执行要等用户完成 §1.3 的三件事并批准。任务登记：T-0310（T-0195 子任务）。

## 0. 结论先行

1. **三条路由、同一批 50 条、各写独立 group_id，A/B 内不调参**：复盘每条路由跑完 50 条做一次
   （`--every 50`），教训只「记录」不「应用」，否则后跑的路由占了先跑路由的便宜，对比失效。
   教训在全量消化第一轮统一采用（§4.4 说明为什么这样安排才符合协议）。
2. **A/B 用三个新业务 key，不碰生产路由、不碰断路器**：`kg_hub.entity_extract_ab_deepseek` /
   `_ab_qwen` / `_ab_claude`。kg-hub 的断路器只认 `KNOWN_KEYS` 里的两个 key（[breakers.py:37](../breakers.py)），
   清单外的 key 一律放行，所以 A/B 不需要恢复断路器；但这等于绕开了 10-12 跳闸的原因
   （终态未知），**必须由用户明确批准**。全量消化则必须恢复断路器。
3. **评分脚本 `tools/ab_score.py` 已可用**（有单元测试），指标与复盘报告同口径；执行脚本
   `tools/ab_run.py` 的设计在 §3，等批准后再写（它一跑就花钱，不先写）。
4. **全量消化 13–18 天**（14.2k 待处理 + 2.8k live 待核验 + 0.9k backlog 待核验 ≈ 17.9k 条，按
   历史 1,000–1,400 条/天），前提是终态可靠性回到 9 月水平。两个「待实现」旋钮
   （`edge_schema_map_on_ingest`、`entity_name_filter`）的改动点在 §4.5，建议在全量开始前实现，
   否则复盘只能把它们记为「提出但未实践」。

## 1. 三条路由并排

### 1.1 路由表

| 路由标签 | 业务 key（网关 `model` 字段） | provider / model | group_id | FalkorDB 图 | 角色 |
|---|---|---|---|---|---|
| `deepseek` | `kg_hub.entity_extract_ab_deepseek` | qianfan / `deepseek-v4-flash-0731`（与生产 `kg_hub.entity_extract` 同配置） | `kg_hub_ab_deepseek` | `kg_hub_ab` | 现状 |
| `qwen` | `kg_hub.entity_extract_ab_qwen` | bailian / `qwen3.6-plus`（8 月合规率 60% 的那套；若凭证只剩 qwen3.8-flash 则用它并注明） | `kg_hub_ab_qwen` | `kg_hub_ab` | 历史基线 |
| `claude` | `kg_hub.entity_extract_ab_claude` | anthropic / 用户选定的 Claude 型号（建议先用最便宜的一档，抽取是结构化任务） | `kg_hub_ab_claude` | `kg_hub_ab` | 新增 |

- 业务 key 形如 `kg_hub.<name>`，由 [model_gateway_client.py:65](../model_gateway_client.py) 的
  `BUSINESS_MODEL_PATTERN` 校验；三个 key 都是清单外，不受 kg-hub 断路器约束（见 §1.3）。
- **隔离方式是「独立图 + 独立 group_id」双保险**：三条路由都写进一个单独的 FalkorDB 图 `kg_hub_ab`
  （`build_graphiti(database="kg_hub_ab")`），同图内再按 group_id 分路由。只用 group_id 不够——
  主服务的 `kg_stats`、`kg_search`、`episode_search`、nightly_prune、normalize_edge_names 都是整图操作，
  不按 group_id 过滤（[utils/indexed_edge_search.py:41](../utils/indexed_edge_search.py) 甚至把
  `group_ids != ["kg_hub"]` 当异常路径）。独立图保证主图的任何读写路径都看不见实验数据。
  `tools/ab_score.py --database kg_hub_ab` 就是为此留的参数。
- `/api/ingest` 不接受 group_id（[kg_hub_server.py:124](../kg_hub_server.py) 把 `GROUP_ID` 写死为
  `kg_hub`），所以 A/B 不走 `/api/ingest`，走 §3 的容器内脚本直接调 `graphiti.add_episode`。

### 1.2 指标（与报告第 3 节同列）

`tools/ab_score.py` 对每条路由算：覆盖率（入图 episode / 50）、实体/条、边/条、零边 episode、
**边名合规率**、**边名种类**、重复三元组、无类型实体、**脏命名**、摘要均长；给账本时再加每条调用数、
每条 wall 中位/p90、阶段分布；每条路由列 schema 外边名 top 8（复盘写证据用）。

人工部分：每条路由随机抽 20 个实体看类型是否正确、10 条边看 fact 是否成立，记进该路由的
lessons.md，口径：错一个算一个，不打分。

### 1.3 执行前需要用户做的三件事

| # | 事 | 在哪做 | 为什么 |
|---|---|---|---|
| 1 | credvault 生产路由表 `config/routes.nas.json` 新增 §1.1 的三条路由 + Claude 凭证 | credvault（用户侧） | kg-hub 不保存任何 provider 凭证；路由表字段照抄现有 `kg_hub.entity_extract`：`provider`、`model`、`credential_ref`、`protocol: anthropic`、`thinking_policy: disabled`、`timeout: 150`、`limits`（建议 `max_concurrency: 1`、`requests_per_minute: 15`、`daily_requests: 1500`、`max_tokens: 8192`）。credvault 已支持 `anthropic` 供应商（bearer / x-api-key 两种凭证形态）。 |
| 2 | 批准调用额度 | 用户决定 | 50 条 × 中位 6（均值 6.8）次调用 ≈ **350 次/路由，三路合计约 1,050 次**；按单次 prompt 4–6k token 估每路 1.5–2M 输入 token、0.2–0.3M 输出 token。本文不替用户算钱；Claude 单价按当下官价换算。 |
| 3 | 断路器处置 | 拓扑页 / `breakers.py` | A/B 用新 key 不需要动它，但**要批准「用新 key 绕开当前断开状态做 150 条实验」**；全量消化前必须恢复 `kg_hub.entity_extract`，恢复由用户决定（10-12 跳闸原因是终态未知，先修可靠性，见报告 §5.2 第 1 条）。 |

另外两条红线不需要用户做事但要知道：实验只写 `kg_hub_ab` 图；实验数据用完由用户决定删图
（`GRAPH.DELETE kg_hub_ab`），不并入主图。

## 2. 样本

### 2.1 抽样方法（已执行，只读）

样本文件：[ab-sample-20261017.json](ab-sample-20261017.json)（50 个 id + 每条的
`source_description` / `source_obs_id`，与 `kg_refinery.to_payload` 完全一致，评分时按账本索引取调用数）。

- 总体：claude-mem.db 副本（`/volume2/4T/kg-hub-data/claude-mem/claude-mem.db`，`mode=ro&immutable=1`）
  `observations.id > 21742`（live 游标），共 27,298 条；剔除 `refinery-state/watermark.json` 里
  ingested / rejected / held / failed 的 13,062 条，剩 **14,236 条真正待处理**。
- 质量闸：对每条跑 `utils.ingest_filter.evaluate(obs, cfg, quotas=None)`，与 kg_refinery 的用法完全一致
  （不带日配额跟踪器，cfg 是仓库 `config/ingest_filter.json`，shadow_mode=false），取 `would_accept`。
  结果：pass 9,566 / override 2,521 / score 拒 1,743 / hard_gate 拒 406，**接受 12,087 条**。
- 分层：按 project 分层，50 个名额按各层接受条数占比用最大余数法分配，层内
  `random.Random(20261017).sample`，最后按 id 升序。名额：kg-hub 14、credvault 14、fleet-ops 8、
  workspace_codex 5、workspace_claudeCode 4、zhaopin 3、sd-server 1、WeChat 1。
- 样本画像：类型 discovery 21 / bugfix 7 / decision 6 / change 6 / feature 5 / refactor 3 /
  security_alert 1 / `[ discovery ]` 1（归一化后为 discovery）；平台 codex 30 / claude 20；
  id 跨 22,828–47,862（2026-09-10 到 10-16）。
- 脚本：质量闸代码（`utils/ingest_filter.py`、`utils/ops_noise.py`、`config/ingest_filter.json`）打包经
  ssh 标准输入送到 NAS `/tmp`，`python3 -I` 运行，跑完即删。NAS 上是 Python 3.8，`ingest_filter`
  的 `from __future__ import annotations` 让它能跑。

### 2.2 为什么是这 50 条

分层按接受后的项目占比，所以 kg-hub + credvault 占 56%——这就是积压的真实构成，A/B 要回答的是
「换模型对**这批**积压的效果」，不是对均匀分布的理想数据。小项目（sd-server、WeChat）各 1 条
只起信号作用，复盘时不要拿它们的单条指标做结论。

## 3. 执行方式（`tools/ab_run.py`，待批准后实现）

在 kg-hub-server 容器内跑（能 import `graphiti_client`、能连 FalkorDB、账本在 `/backup/model-attempts.sqlite3`）：

```
python -m tools.ab_run --route deepseek=kg_hub.entity_extract_ab_deepseek --sample docs/ab-sample-20261017.json \
    --database kg_hub_ab --state /state/digest-retro-ab.json --journal /backup/model-attempts.sqlite3
```

设计要点（每一条都对应现有代码里的一个口子，不改生产路径）：

1. **按路由建 Graphiti**：`build_graphiti(database="kg_hub_ab", llm_client=build_llm(business_key))`。
   现在 `build_llm()`（[graphiti_client.py:223](../graphiti_client.py)）从 `ANTHROPIC_MODEL` 环境变量取业务 key，
   需要加一个可选参数 `business_key: str | None = None`，默认行为不变。网关客户端把 `model` 字段
   原样当业务 key 送网关（[model_gateway_client.py:733](../model_gateway_client.py)），所以路由切换只是这一个字符串。
2. **正文与身份与生产一致**：用 `kg_refinery.fetch_rows_by_ids` + `to_payload` 生成
   `episode_body` / `source_description` / `source_obs_id`（样本文件里已经存了后两者，可核对）。
   每条包在 `model_business_task(sd, sid)` + `model_operation("ab." + label, stable_operation_id(label, sd, sid, run_id))`
   里，这样账本 `model_attempts` 按 (sd, sid) 索引能查到、按 `business_key` 能分路由，且三条路由的幂等钥匙互不相同。
3. **入图参数与生产一致**：`add_episode(..., group_id=<路由 group_id>, entity_types=ENTITY_TYPES,
   edge_types=EDGE_TYPES, edge_type_map=EDGE_TYPE_MAP)`，`SEMAPHORE_LIMIT=1`、`max_coroutines=1`
   照旧（串行，避免 429 干扰耗时对比）。
4. **让复盘门禁照常工作**：每条跑完用 `ModelAttemptJournal.begin_task_execution / finish_task_execution`
   （[utils/model_attempt_journal.py:329](../utils/model_attempt_journal.py)）记一条 `task_executions`，
   `execution_id = f"ab:{label}:{run_id}"`，状态按 add_episode 是否返回且 episode 落图判 succeeded / failed。
   这样 `digest_retro gate --every 50` 不用改就能在每条路由 50 条后退出码 2。
5. **顺序**：deepseek → qwen → claude，一条路由全部 50 条跑完再跑下一条（§4.4 解释）。每条路由开跑前
   `gate` 必须是 0；跑完 50 条 `gate` 变 2，先 `checkpoint` 再开下一条。
6. **失败处理**：单条 add_episode 异常记 failed 继续，不重试（重试会让该路由的调用数不可比）；
   覆盖率 < 90% 的路由在对比表里标注，其余指标只代表入图的那部分。
7. **红线在代码里**：脚本启动时断言 `--database != "kg_hub"`、每个 `--route` 的 group_id 以
   `kg_hub_ab_` 开头、业务 key 以 `kg_hub.entity_extract_ab_` 开头，否则拒绝启动。

## 4. A/B 的复盘闭环与全量消化方案

### 4.1 A/B 的循环（`--every 50`）

```
for label in deepseek, qwen, claude:
    python -m tools.digest_retro gate --state /state/digest-retro-ab.json --journal ... --every 50   # 必须 rc 0
    python -m tools.ab_run --route label=... (50 条)
    python -m tools.digest_retro gate ...                                                         # rc 2
    写 lessons-<label>.md（观察 / 原因判断（给证据：obs id、边名、样本）/ 下一轮改什么）
    python -m tools.digest_retro checkpoint --state ... --journal ... --lessons-file lessons-<label>.md \
        --applied "A/B 内不调参（§4.4）"            # 不带 --adjust
python -m tools.digest_retro final --state /state/digest-retro-ab.json > ab-final.md
python -m tools.ab_score --route deepseek=kg_hub_ab_deepseek --route qwen=kg_hub_ab_qwen --route claude=kg_hub_ab_claude \
    --database kg_hub_ab --journal /backup/model-attempts.sqlite3 --sample docs/ab-sample-20261017.json \
    --business-key deepseek=kg_hub.entity_extract_ab_deepseek --business-key qwen=... --business-key claude=... \
    --json ab-score.json >> ab-final.md                                                            # 并排表附在 final 后
```

注意 `checkpoint` 不带 `--no-graph` 时查的是 `build_graphiti()` 默认图（主图）的时间窗口，A/B 的图库指标
要从 `ab_score` 取；A/B 阶段的 `checkpoint` 用 `--no-graph`（只记账本指标与教训），并排图库指标由
`ab_score` 补齐。这是 digest_retro 的一个已知缝隙，如果 A/B 获批，给它加 `--database` 参数（10 行改动）。

### 4.2 判定规则（先写下来，免得看了结果再定标准）

| 条件 | 换路由的门槛 |
|---|---|
| 覆盖率 | ≥ 96%（50 条里最多 2 条失败） |
| 边名合规率 | 候选路由 ≥ 现状路由 + 15 个百分点，且 ≥ 50%（复盘阈值 `edge_in_schema_min`） |
| 零边 episode | 候选 ≤ 现状，且 ≤ 15% |
| 脏命名 | 候选 ≤ 现状 |
| 人工类型正确率 | 候选 ≥ 现状 |
| 每条 wall p90 | ≤ 600s（`wall_p90_max_s`） |
| 成本 | 用户按 `calls_mean` × 单价定，本文不设阈值 |

全部满足才建议把 `kg_hub.entity_extract` 切过去；只满足质量不满足成本，走报告 §5.2 第 3 条
「分阶段用不同模型」：node_extraction + edge_phase 用候选模型，attribute_phase 用便宜模型或关掉
（调用占比 edge 38% / attribute 32%，见报告 §2.2）。

### 4.3 全量消化：规模与排期

| 项 | 数 | 来源 |
|---|---|---|
| live 待处理（过闸前） | 14,236（抽样时），报告采样时 14,106 | 本次只读统计 |
| 其中会被闸接受 | ~12.1k（85%） | §2.1 |
| live 待核验（held） | 2,790 | 报告 §1 |
| backlog 待核验（held） | 927 | 报告 §1 |
| 合计要过模型的 | ≈ 15.8k（接受的 12.1k + 待核验 3.7k 需重跑或核验） | — |
| 历史速率 | 1,000–1,400 条/天（9-29 ~ 10-05 qwen3.8-flash 窗口） | 报告 §2.1 |
| **估时** | **12–16 天**（按 15.8k）；若按 17.9k 全量算 13–18 天 | — |

按每天 1,200 条、每 500 条复盘一次，全程约 **30 轮复盘**，每轮占执行者 15–30 分钟（看指标、写教训、调参）。

### 4.4 全量消化的执行循环（协议原文的落地）

```
准备（一次）：
  1. 用户恢复断路器（或按 A/B 结论先把 kg_hub.entity_extract 切到新路由再恢复）
  2. 实现 §4.5 的两个旋钮并发布（否则它们在复盘里只能是「提出但未实践」）
  3. A/B 的 final 报告里「提出但未实践的建议」作为第 1 轮的初始调参：
     checkpoint 第 0 轮（--force --lessons-file ab-final.md --adjust ...）把起点登记进 digest-retro.json
循环（refinery 自己派发，执行者只管门禁与复盘）：
  gate --state /state/digest-retro.json --journal /backup/model-attempts.sqlite3     # rc 2 → 复盘
  复盘：
    a. status 输出 + 上一轮 round-NNN.md 的「规则发现」
    b. 写 lessons.md：观察 / 原因判断（证据）/ 下一轮改什么（没有问题就写「无越线项，本轮不调参」）
    c. 真的去调：改 compose 环境变量并重启 refinery、跑 normalize_edge_names --apply、请用户改路由……
    d. checkpoint --lessons-file lessons.md --adjust k=v ... --applied "上一轮的 X 已生效"（容器内跑，拿图库指标）
结束：
  final --state /state/digest-retro.json > final.md
  final.md 末尾附 A/B 并排表（ab_score 输出）与「换/不换路由」的最终判定（§4.2 规则）
```

**为什么 A/B 内不调参、全量才调**：协议要求「教训用于下一轮」，A/B 里「下一轮」是另一条路由，
把 deepseek 轮学到的调参用到 qwen 轮，qwen 的指标就混进了调参效果，A/B 回答不了「换模型值不值」。
所以 A/B 三轮都在同一组旋钮下跑，教训全部记录、在 final 里列为「提出但未实践」，全量第一轮统一采用。
如果用户愿意多花一倍调用，可以做「两遍制」：第一遍三路同旋钮，第二遍三路同时采用第一遍的教训，
那样 A/B 自己就能闭环。默认不做。

### 4.5 旋钮：哪些已实现、哪些要先实现

| 旋钮 | 状态 | 在哪生效 |
|---|---|---|
| `live_per_cycle` / `backlog_per_cycle` | 已实现 | refinery 环境变量，改后重启 refinery |
| `parallel_tasks` | 已实现 | 服务端 `KG_HUB_PARALLEL_TASKS` |
| `normalize_edge_names` | 已实现 | 轮末 `python -m tools.normalize_edge_names --apply`（只做大小写归一，不把自创名映射进 schema） |
| `route_model` | 已实现 | credvault 生产路由表，用户改 |
| `edge_schema_map_on_ingest` | **待实现** | 入图后把 schema 外边名映射为 `relates_to`，原名存 `original_name` |
| `entity_name_filter` | **待实现** | 入图后把 commit 哈希 / 绝对路径 / 任务号实体降级为属性并退休 |

两个待实现旋钮的改动点（都在入图路径的**结果后处理**，不改 graphiti 的 prompt、不改抽取本身）：

**公共挂点**：`kg_hub_server.py` 里 `add_episode` 返回 `AddEpisodeResults(episode, nodes, edges)` 的两处——
整篇路径 `_do_extract_inner`（[kg_hub_server.py:1321](../kg_hub_server.py) 拿到 `result` 之后、写
IngestedKey 终态之前）和预拆子条路径 `_predigest_extract`（[kg_hub_server.py:1171](../kg_hub_server.py)
`_locked_add_episode` 返回之后）。在这两处调一个新模块 `utils/ingest_postfix.py` 的
`await apply_postfix(graphiti.driver, result)`，内部按环境变量开关分别做两件事；在 writer lock 内、
同一次抽取里完成，所以对检索来说新数据从来没有以「脏」的形态出现过。并行抽取路径
（`utils/graphiti_stage_adapter.py`）返回的也是 `AddEpisodeResults`（[graphiti_stage_adapter.py:674](../utils/graphiti_stage_adapter.py)），同一个挂点覆盖。

**`edge_schema_map_on_ingest`**（`KG_HUB_EDGE_SCHEMA_MAP_ON_INGEST=1`）：

```
MATCH ()-[r:RELATES_TO]->() WHERE r.uuid IN $uuids AND NOT toLower(r.name) IN $schema
SET r.original_name = r.name, r.name = 'relates_to', r.schema_mapped_at = $now
```

`$uuids` 取 `result.edges` 的 uuid，`$schema` 取 `schema.EDGE_TYPES` 的小写名。FalkorDB 不支持 `=~`，
所以用 IN。保留 `original_name` 是为了以后要是扩 schema 能回填。指标 `edge_in_schema` 立刻会被这个旋钮
抬高，所以复盘看这个旋钮生效后要**同时看 `edge_distinct_names` 的原名口径**（ab_score / digest_retro
加一个 `--raw-edge-names` 开关按 `coalesce(r.original_name, r.name)` 统计，否则看不出模型到底有没有变规矩）。
存量 16.5 万条边不在这个旋钮范围内，另走 `normalize_edge_names` 扩展（加 `--map-offschema`）。

**`entity_name_filter`**（`KG_HUB_ENTITY_NAME_FILTER=1`）：对 `result.nodes` 里名字命中
`DIRTY_PREFIXES`（`commit `、`/Users/`、`/volume`、`T-0`，与 digest_retro 同一份常量，应挪到 `schema.py`
共用）或匹配 7–40 位十六进制的节点：

```
MATCH (n:Entity {uuid: $u})
OPTIONAL MATCH (n)-[r:RELATES_TO]-(m:Entity)
SET m.artifacts = coalesce(m.artifacts, []) + n.name          # 降级为邻居的属性
SET n.retired = true, n.retired_reason = 'artifact_name', n.retired_at = $now
```

不删节点、不删边（删了就没法回滚），靠 `retired` 标记让检索侧过滤（`tests/test_search_retired_filter.py`
已经有 retired 过滤的契约，复用它）。`dirty_name_share` 指标要改成只数 `NOT coalesce(n.retired,false)`
的实体，否则旋钮生效了指标也不动。路径类名字（`/Users/...`）更好的归宿是 `File` 类型的 `path` 属性，
这是第二步，第一步先退休。

两个旋钮都要：在 `tools/digest_retro.py` 的 `KNOBS` 里把 `implemented` 改为 True、写明环境变量；
协议文档表格同步；各加单元测试（给假 driver 断言 cypher 与参数；给 AddEpisodeResults 空结果断言不发查询）。

## 5. 红线（再说一遍）

- 不发起任何付费模型调用：本文、抽样、评分脚本、单元测试都没有调模型。`ab_run.py` 等批准后再写。
- 不动断路器：A/B 用新 key 绕开的是 kg-hub 侧开关，这件事本身要用户批准；全量前恢复由用户决定。
- 不写主图：A/B 只写 `kg_hub_ab` 图；`ab_score` 只读；`ab_run` 启动时断言图名与 group_id 前缀。
- 账本 7.3 GB：`ab_score` 只按 (source_description, source_obs_id) 索引逐条取 50 条，不扫全表；低峰跑。
