# business_result_missing 成因只读排查（deepseek 路由时代，2026-10-17）

> 只读排查：没有发起模型调用、没有动断路器、没有写图、没有改线上。
> 数据来源：NAS 账本 `model-attempts.sqlite3`（低峰 02:05–02:40 CST，`task_executions` 小表整读 + `model_attempts` 按 `(source_description, source_obs_id)` 索引逐任务取，无全表扫描）、FalkorDB 只读 Cypher（容器内 `get_status_driver`）、网关 `llm-requests.sqlite3` 的 `events` 表（按 `events_at` 索引取时间窗）、网关 `business-reconciliation.sqlite3`、`/health/reconciliation`、refinery `status.json`、kg-hub 与 credvault 的 git 历史。
> 任务登记：T-0311（T-0195 子任务）。前置报告：`docs/gateway-model-quality-20261017.md` 第 2 节。

## 0. 结论先行

1. **「10-09 起 ~70% 终态是 business_result_missing」是账本口径造成的假象，不是入图成功率。** 图里 `IngestedKey` 自己的终态：10-09 当天 899 条 ok（全部有 episode）、16 条待核验（1.7%）；10-07～10-12 整个 deepseek 时代合计约 2,530 条 ok、25 条待核验（**约 1%**）。`task_executions` 看起来 70% 失败，是因为（a）夜间清理会按 7 天保留期删掉已 ok 任务的执行记录、只留下待核验任务的记录；（b）10-09 15:40 CST 千帆 token plan 额度用尽，2 小时内 389 条任务被 429 拒绝，被记成 uncertain；（c）这些任务 10-09 晚～10-12 重跑成功后，重跑**不会**再写 `task_executions`。三件事叠加，账本里只剩「成功的没记、失败的留着」。
2. **真正的「供应商/网关终态不可用」在 deepseek 时代不到 1%。** 网关队列事件 10-07～10-16：供应商 503 网络错误约 92 次、执行器错误约 40 次、网关自身幂等/配置错误约 46 次，合计约 178 次，对应约 22k 次调用。10-12 跳闸的 6 条就是其中一次 14 秒的网络抖动（T-0285 已查明）。
3. **待核验存量 3,668 条（refinery 水位 3,743）几乎全是 deepseek 切换之前的两次网关事故**：09-30 09:00–15:00 UTC 1,107 条、10-06 03:00–08:00 UTC 2,154 条（切换发生在 10-06 08:55 UTC，之后才是 deepseek）。这两批的账本阶段是 `absent`：kg-hub 发出了 HTTP，网关没有任何幂等记录（`external_calls 0`），即**没有计费、没有结果**。它们之所以还挂着，是因为 refinery 的 held 集合没有任何自动回流路径，只有 `tools/release_held.py` 这条手动路。
4. **kg-hub 侧「超时放弃」不是成因。** deepseek 单次成功调用中位 30.7 s、p90 56 s、p99 157 s（含排队），150 s 路由超时只碰到约 1% 的调用且都拿到了结果；30 分钟 stuck 阈值远大于单条 p90 457 s。
5. **恢复断路器本身没有阻碍。** 10-09/10-10 两类事故在 kg-hub（#106 #108 #109）和 credvault（85bd555 / 5ff2877 / 3589b0e）两侧都已有修复并随 10-14 之后的镜像上线。恢复前要做的是：先把 3.4k 条无计费的 `absent` 存量放回队列、把 held-surge 守卫的阈值从「15 分钟 5 条」改成有比例判断、改掉账本的三处口径问题，否则恢复后同样的统计会再次把「限流/网络抖动」读成「模型不可靠」。

## 1. 证据表

### 1.1 账本 `task_executions` 与图 `IngestedKey` 的终态对照（按任务创建/开始日，UTC）

| 日期 | 路由 | task_executions：succeeded / uncertain / failed | IngestedKey：ok / needs_reconciliation / failed | 待核验占比（图） |
|---|---|---|---|---|
| 09-27 | qwen3.8-flash | — / — / 31 | 175 / 30 / 0 | 14.6% |
| 09-28 | qwen | — / — / 18 | 406 / 11 / 0 | 2.6% |
| 09-29 | qwen | — / — / 55 | 743 / 19 / 0 | 2.5% |
| 09-30 | qwen | — / 2 / 1,109 | 952 / **1,108** / 2 | 53.8% |
| 10-01 | qwen | — / 2 / 39 | 1,082 / 38 / 0 | 3.4% |
| 10-02 | qwen | — / 1 / 37 | 866 / 33 / 0 | 3.7% |
| 10-03 | qwen | — / — / 25 | 806 / 20 / 0 | 2.4% |
| 10-04 | qwen | — / 2 / 37 | 731 / 28 / 0 | 3.7% |
| 10-05 | qwen | — / 1 / 56 | 694 / 28 / 0 | 3.9% |
| 10-06 | 08:55Z 切 deepseek | — / 2,895 / 3,407 | 295 / **2,154** / 28 | 87.0% |
| 10-07 | deepseek | — / 4 / — | 241 / 2 / 0 | 0.8% |
| 10-08 | deepseek | — / 28 / 27 | 676 / 177 / 0 | 20.7% |
| 10-09 | deepseek | 155 / 392 / 15 | 899 / 16 / 0 | 1.7% |
| 10-10 | deepseek | 97 / 10 / 9 | 239 / 0 / 1 | 0% |
| 10-12 | deepseek，18:00Z 跳闸 | 12 / 0 / 15 | 173 / 6 / 2 | 3.4% |
| 10-13 | 断开，人工 | 5 / 0 / 9 | 5 / 0 / 1 | — |
| 10-14 | 断开，人工 | 20 / 3 / 31 | 20 / 0 / 2 | — |

读法：
- `task_executions` 从 09-27 就有记录，但 10-09 之前一条 succeeded 都没有，不是当时没成功，是被 `tools.nightly_prune`（`utils/journal_prune.py --apply --retention-days 7`）删掉了：它只删 IngestedKey 为 ok 且 7 天前的任务的账本行，`running`/`uncertain` 执行记录永远保留。所以这张表的 succeeded 只会覆盖最近 7 天，而 uncertain/failed 会无限累积。10-12 16:11Z 手动跑过一次，之后每夜 15:05Z 自动跑（`journal-prune/status.json`）。
- 图的 ok 数和前置报告 2.1 节的网关调用折算基本吻合（10-09 899 条 vs 折算 ~990）。
- 09-30 和 10-06 两根柱子都在 deepseek 之前；10-08 是 429 突发（见 1.3）。

### 1.2 10-09 的 392 条 uncertain：一次 429 突发，而且事后都入图了

| 项 | 值 |
|---|---|
| 时间分布（UTC） | 389 条落在 07:30–09:20（CST 15:30–17:20），每 10 分钟 31–49 条；succeeded 的 155 条全部在 15:50–23:59，两者时段不重叠 |
| 单条执行时长 | 中位 **37.6 s**，p90 47.7 s（succeeded 中位 286 s）——不是跑到一半丢了，是第 1–3 次调用就被拒 |
| 每条调用数 | 中位 3（succeeded 中位 6） |
| 账本行 | 1,216 completed + 407 failed；**281 行 `gateway_http_status=429`**，`provider_call_started` 为空 |
| 429 行的时间 | `created_at` 在执行结束前 19–54 s；`phase=failed` 的 `updated_at` 在执行结束后 5.2–23.3 小时（由对账轮询回填） |
| 网关事件同窗 | 07:30–09:30Z：queued 899、**failed provider_error 429：834**、succeeded 71 |
| 同一天 kg-hub 提交 | e1a5039（#106，10-09 18:09 CST 合并）：「2026-10-09 15:40 千帆 token plan 额度用尽，队列作业全部以 failed/provider_error/429 结束。kg-hub 只看到『已向供应商发起』，把 464 条任务判成待核验」 |
| 这 392 条现在在图里 | **380 条 ok 且有 episode**（IngestedKey 重建于 10-09T18–23Z 的 64 条、10-10 的 35 条、10-12T16–17Z 的 91 条、其余零散）；9 条仍 needs_reconciliation（4 条 10-09T07Z 的 `failed`、4 条 10-12 跳闸时的 `admitted`、1 条 10-09T04Z）；3 条 10-14 人工 resume 成功 |
| 重跑是否记账 | 389 条任务在 `task_executions` 里只有 1 条 uncertain 记录；重跑成功的那次没有记录 |

为什么当时被记成 uncertain 而不是 deferred：`utils/task_execution.py` 在 worker 返回时调 `summarize_attempts`，那一刻 429 那行的 `phase` 还是 `prepared`、`provider_call_started` 为空、`http_started_at` 已写，距今不到 180 s → 算 `in_flight` → 整条执行记成 `uncertain / business_result_missing`。#106 之后被拒调用改记 `phase=failed、provider_call_started=0`，这条路径对队列 429 已修；但断路器 preflight 拒绝（`error_kind=breaker_open`）仍然走到 `failed`，见 1.5。

### 1.3 待核验存量 3,668 条的来源（图 `needs_reconciliation` 按创建小时 + 错误后缀）

| 来源窗口（UTC） | 条数 | 账本最后一步的阶段 | 当时的网关 | 解释 |
|---|---|---|---|---|
| 09-30 09:00–15:00 | 1,079（`absent`）+ 28 | 30 条样本：node_extraction/node_resolution `absent`，`http_started_at` 已写，无 HTTP 状态；执行时长 850–6,280 s 后才被判 failed | 旧网关（无持久队列），qwen3.8-flash | kg-hub 发了请求，网关没有幂等记录；任务挂了 15 分钟–1.7 小时才被 stuck 清理改成待核验 |
| 10-06 03:00–08:00 | 2,150（`absent`）+ 4 | 30 条样本：全部只有 1 行 node_extraction `absent`；执行时长 9,000–45,000 s | 旧网关；08:55Z 才切到带持久队列的新网关和 deepseek | 同上，持续 5 小时，每小时 290–530 条。切换后 09–14Z 队列正常跑了 1,575 次（1,540 次成功）；14:30Z 后网关事件为零而 kg-hub 又把 3,370 条里的 2,876 条重跑了一遍（每条 2–200 s 失败，记 uncertain），这 2,876 条的 IngestedKey 现在 2,304 条已不存在（被清掉、回到 live 待处理池），其余在 10-07/08 入图 |
| 10-08 02:00–04:00 | 174（`failed`）+ 3 | 30 条样本：node_extraction `failed`，无 HTTP 状态；配对一行 `absent` | 新网关，deepseek | 网关事件 02–03Z：provider_error 429 共 248 次。#106 之前的 429 → 待核验 |
| 09-27～10-05 零散 | 约 200 | absent / failed / admitted / unknown 混合 | 旧网关 | qwen 时代的日常 2–4% |
| 10-07～10-12（deepseek） | **25** | 10-07 2 条 admitted；10-09 11 failed + 4 absent + 1 unknown；10-12 6 条 admitted（跳闸那 6 条） | 新网关 | deepseek 时代真正留在待核验里的全部 |

网关侧对这些任务的报告（`business-reconciliation.sqlite3` `task_reports`，caller=kg-hub，按模型步骤计）：`manual_retry_available` 4,378（可人工重试）、`model_admission_unknown` 2,173（kg-hub 自己的 `unknown_without_http_evidence`，判为不可重试）、`model_outcome_unknown` 557、`retry_adapter_unavailable` 81、`breaker_open` 67、运行中 66、`task_execution_history_missing` 49。

`tools/release_held.py` 的文档注释记录了 10-08 的同类处理：当时 held 5,194 条，4,563 条「没有任何成功调用，每次失败要么没到网关（absent，external_calls 0）要么被网关判定失败（429）」，重提的成本等于一条新观测。这次存量的构成和那次一样。

### 1.4 网关队列事件里的失败类型（全部业务，按天）

| 日期 | 失败/待对账事件 | 说明 |
|---|---|---|
| 10-06 | reconciliation 9（8 条 `configuration_error 503 幂等终态写入保护不一致`，1 条 executor_error） | 队列 08:55Z 才建，之前的事故不在这张表 |
| 10-07 | reconciliation 20（executor_error 9、configuration_error 6、provider 503 网络 3、executor_interrupted 2） | |
| 10-08 | failed **429：248**；reconciliation 15（executor_error 11、503 网络 4） | |
| 10-09 | failed **429：969**；reconciliation 54（idempotency_terminal_write_unavailable 30、configuration_error 10、provider_error 9、executor 3、503 网络 2）；cancelled_by_caller 6 | 429 是千帆 token plan 额度 |
| 10-10 | reconciliation 82（provider_error disposition=reconciliation **65**、executor_error 17） | `utils/held_surge.py` 注释：10-10 11:00 CST 供应商侧 ~15 s 空闲切断，打断约一半长非流式调用，每小时约 24 条可能已计费的失败 |
| 10-12 | reconciliation 6（provider 503 网络，17:59:58–18:00:12Z） | 触发 held-surge 自动跳闸 |
| 10-16 | reconciliation 3（provider 503） | claude_mem 侧 |

网关 `/health/reconciliation`（10-16T18:17Z）：`pending` kg_hub 141、claude_mem 122；`provider_pending` kg_hub 162、claude_mem 152，**全部 phase=armed 且 `http_starts` 表里没有对应行**——见证已布防但 HTTP 从未发起，是重启留下的残留，不是在飞调用。

### 1.5 deepseek 时代 `task_executions` 里 485 条 business_result_missing 的构成（10-09～10-14，按开始日）

| 类别 | 条数 | 占比 | 证据 |
|---|---|---|---|
| 10-09 429 突发，拒绝前无结果，记成 uncertain | 389 | 80% | 1.2 |
| 断路器 preflight 拒绝（task-hub/openclaw 推送），0 次调用、0.2 s，记成 failed | 71 | 15% | 10-12～10-16 共 71 条，71 个不同任务；现在图里 63 个键已不存在，7 个 `failed/reconciliation_model_step_missing`，1 个 `error/breaker_open` |
| 挂了数小时后由对账判 failed（10-09 15 条、10-10 9 条、10-13 1 条） | ≈25 | 5% | 10-09 failed 的 p90 时长 25,150 s |
| 真正的结果未知（admitted/unknown 阶段）：10-10 10 条、10-14 人工 resume 3 条 | ≈13 | 3% | 10-10 的 10 条：phases admitted 4 / unknown 1 / failed 4 |

### 1.6 耗时与超时参数（deepseek 时代 4,298 次当天完成的调用）

| 指标 | 值 |
|---|---|
| 单次调用 created→completed | 中位 30.7 s，p90 56.2 s，p99 157 s，>120 s 89 次（2.1%），>150 s 47 次（1.1%，均已拿到结果，含排队） |
| 路由 timeout | 150 s；kg-hub 客户端 180 s（`MIN_CLIENT_TIMEOUT_SEC`） |
| `KG_HUB_STUCK_THRESHOLD_MIN` | 30 分钟；succeeded 单条 p90 457 s、最长 2,165 s |
| held-surge 守卫 | `KG_HUB_HELD_SURGE_THRESHOLD=5` / `KG_HUB_HELD_SURGE_WINDOW_SEC=900`；10-12 当时每 10 分钟约 170 次调用，6 次 503 即 3.5% 的 15 分钟失败率就跳闸 |
| refinery 当前状态（10-16T18:15Z） | breaker_open=true（auto:held-surge）、rate_limit_hits 34、backoff_pending 27、watermark held 3,743 |

## 2. 根因判断

按用户要的三类分：

| 类别 | 在「70%」里的份额 | 在真实入图口径里的份额 | 判断 |
|---|---|---|---|
| **核对流程缺口（账本口径 + 分类 + 回流）** | **≈95%**（389 条 429 误记 uncertain + 71 条 preflight 误记 failed + 账本只保留失败 + 重跑不记账） | 决定了 3,668 条存量里 ≈3,400 条（`absent`，无计费）为什么至今不回流 | 主因。四个具体缺口：① 夜间清理按状态删账本行，统计口径失真；② `run_task_execution` 结束时把「刚被拒的调用」当 in_flight，拒绝前无结果的 429/断路器都没落到 `deferred`；③ uncertain 之后的重跑不再写 `task_executions`（`begin_task_execution` 遇到非终态记录直接 raise，worker 照常跑但无记录）；④ held 集合没有自动回流，`absent` 类要靠人跑 `release_held.py` |
| **供应商/网关终态不可用** | ≈3%（13 条 admitted/unknown） | deepseek 时代约 178 次调用 / 25 条任务，<1% | 存在但小。两次真实事故（10-10 空闲切断、10-12 网络抖动）网关侧已修（5ff2877 按空闲时间判超时、85bd555 429 暂停+探测、3589b0e 无应答比例告警），10-14 镜像已带。存量里的大头（09-30、10-06）是旧网关事故，和 deepseek 无关 |
| **kg-hub 超时放弃** | ≈5%（挂数小时后判 failed 的 25 条，是对账收尾不是放弃） | ≈0 | 不是成因。150/180 s 超时只碰到 1% 调用且都拿到结果；30 分钟阈值有 4 倍余量 |

与 10-06 路由切换的关系：切换本身没有引入新的终态问题。10-06 当天 87% 的待核验发生在切换前 5 小时（旧网关无响应），切换后 09–14Z 的 1,575 次队列调用成功 1,540 次。deepseek 时代日常待核验率 0–3.4%，与 qwen 时代（2.4–3.9%）相当或更低；两边各有一次 429 突发（10-08、10-09）和 qwen 时代的两次网关事故（09-30、10-06）。

## 3. 修复建议

### 3.1 kg-hub 侧

1. **终态统计改用图口径。** 看板/周报/`digest_retro` 的「成功率」按 `IngestedKey.status` 按天统计，不要用 `task_executions`（7 天保留期会把成功记录删掉）。如果一定要用账本，`nightly_prune` 删行前先把每日 succeeded 计数写进一张小汇总表。
2. **拒绝前无结果 → `deferred`，不再 uncertain/failed。** `utils/task_execution.py` 收尾时：若 IngestedKey `error_kind ∈ REFUSED_BEFORE_RESULT` 或 `breaker_open`，直接 `deferred`，不看 `summarize_attempts.in_flight`（刚被拒的那行此时 phase 还没回填）。把 `breaker_open`、`gateway_unavailable` 加进 `REFUSED_BEFORE_RESULT`，断开期间 task-hub/openclaw 的推送不再变成 `failed/reconciliation_model_step_missing`。
3. **重跑要记账。** `begin_task_execution` 在「前一条执行是 uncertain 且 IngestedKey 已被重建（`created_by_request` 不同）」时，把旧记录改成 `superseded`（或直接允许新 ordinal），让 `task_executions` 能反映重跑结果；否则 1.2 那种 380 条「账本 uncertain、图里 ok」会一直存在，`journal_prune` 也因为 uncertain 记录永远不删它们的账本行（这批正是 7.3 GB 里删不掉的部分）。
4. **held 自动回流。** 对账轮询已经会向网关查 attempt-status；当一个任务的所有未决步骤都是 `absent/failed/rejected` 且网关 `external_calls=0`，自动走 `release_held.py` 的 reset 路径（删 IngestedKey + 从 held 移回），不需要人。保留「有成功步骤的任务走人工 resume 复用付费结果」这条线。
5. **held-surge 守卫加比例。** 现在是「15 分钟内 5 条」绝对数；在 170 次/10 分钟的吞吐下 3.5% 就停产。改成「≥5 条 且 ≥ 窗口内完成任务数的 X%（建议 20%）」，或把窗口里的同一网关错误码合并计数，避免 14 秒的网络抖动停掉 5 天。跳闸后的恢复仍然留给人。
6. `KG_HUB_STUCK_THRESHOLD_MIN=30` 与 180 s 客户端超时不用改。

### 3.2 网关侧（credvault）

1. **清理 `provider_pending` 里 `armed` 但无 `http_starts` 的残留**（kg_hub 162、claude_mem 152）：它们来自进程重启，不是在飞调用，却让 `/health/reconciliation` 一直显示 300 多条未决。给 armed 加过期（例如超过路由 timeout 的 2 倍且无 HTTP 发起即判 failed/absent）。
2. **确认 10-10 的「空闲切断」修复覆盖 qianfan 路径。** 5ff2877 的提交说明写的是 upstream-stream 调用；10-10 的 65 次 provider_error 发生在 deepseek（OpenAI 兼容）路径。恢复后前两小时看 `events` 里 `provider_error disposition=reconciliation` 的数量即可验证。
3. **09-30 / 10-06 那种「收到请求但没有幂等记录」的模式**在持久队列之后没有再出现（10-06 08:55Z 起全部请求都有 queued 事件）。保留 3589b0e 的「无应答比例告警」即可，无需新改动。
4. `model_admission_unknown`（2,173 步骤）在网关报告里被标为不可重试，但网关自己的 attempt-status 对它们答的是 `absent`（无记录、无计费）。两边口径对一下：kg-hub 的 `unknown_without_http_evidence` 判定应把「网关已答 absent」当作可安全重提的证据。

### 3.3 路由参数

- `timeout 150s`：保持。p99 157 s 含排队，>150 s 仅 1.1% 且都有结果。
- `requests_per_minute 60 / max_concurrency 8`：保持。两次 429 突发都来自千帆 token plan 日额度，网关本地准入没有触发。
- 共享日配额 `qwen.token_plan 100,000`：是网关侧计数，供应商侧额度用尽网关看不到；靠 85bd555 的「持续 429 则暂停该供应商+模型并用真实请求探测」兜底，恢复后注意 `rate_limited`/`provider_wait` 两个状态位。

## 4. 恢复断路器前的检查清单

- [ ] **先放回无计费的存量。** 在 kg-hub-server 容器内跑 `tools/release_held.py plan`（只读），确认 `reset` 候选 ≈3,400 条（09-30、10-06、10-08 三批 + 零散），`resume`/`unknown_outcome` 的数量（应 ≈ 25 + 10-12 的 6 条）。用户批准后 `reset-keys --apply` + `request-unhold`。不做这一步，恢复后 watermark held 仍是 3,743，看板会继续显示「待核验 2,790 + 927」。
- [ ] **10-12 跳闸的 6 条 `admitted`**：先查网关 attempt-status 是否已回答 failed；是则随上一步 reset，否则走人工 resume。
- [ ] **调守卫参数再恢复**：至少把 `KG_HUB_HELD_SURGE_THRESHOLD` 提到与吞吐匹配的值（170 次/10 分钟时 5 条 = 3.5%），或先按 3.1-5 改代码。否则下一次 10 秒级网络抖动会再停。
- [ ] **确认线上镜像包含修复**：kg-hub `a310bb43de24`（10-17 01:46 发布，main 含 #106/#108/#109）；model-gateway `src-39c5643…`（10-14 14:25 构建，credvault main 10-13 含 85bd555/5ff2877/3589b0e/a2a7e85）。
- [ ] **refinery 状态位**：`provider_wait`、`rate_limited`、`quota_paused` 都为 false，`backoff_pending`（当前 27）会自然消化。
- [ ] **恢复后前 2 小时的验收口径**（用图，不用账本）：每小时 `IngestedKey` ok / needs_reconciliation（期望待核验 <3%）；网关 `events` 里 `failed 429` 与 `reconciliation provider_error` 计数；`/health/reconciliation` 的 kg_hub pending 不再增长；T-0285 的 `[edge_invalidation_shadow]` 日志开始出现。
- [ ] **恢复后 24 小时**：确认 `nightly_prune` 没把当天 succeeded 删掉（保留期 7 天，应无影响），`task_executions` 的 succeeded 开始出现在新日期；若 3.1-3 未改，账本 uncertain 数会与图的 ok 数继续背离，看板不要再引用它。

## 5. 方法与局限

- 账本只取了 `task_executions` 全表（8,696 行）和 924 条执行对应任务的 `model_attempts` 索引行，以及三批存量各 30 条样本；没有读 `result_json` 正文，没有扫 `created_at`。
- 网关 `events` 表按 `at` 索引取窗口，不带 business_key，10-08/10-09 的 429 计数是全部业务之和（claude_mem 同期也在跑）；10-06 08:55Z 之前的网关行为只能从 kg-hub 账本的 `absent` 和执行时长反推。
- 「10-06 14:30Z 后网关事件为零」是从 events 表缺失推断的，没有当时的容器日志（model-gateway 容器 10-14 重建，`docker logs` 不覆盖 10-09 之前）。
- 10-10 04:00Z 之后吞吐骤降的直接原因（人工断开还是自动暂停）本次没有查到记录，不影响结论。
- 所有百分比按任务计，不按模型步骤计；网关 `task_reports` 按步骤计，两者不能直接相加。
