# 会话自抽取消化协议：Claude 会话抽取 → 预抽取入库 → 每 500 条复盘 → 多模型对比

> 用户决定（2026-10-17）：消化 live 积压的结果必须进主知识库；期间 `kg_hub.entity_extract`
> 断路器保持断开，refinery 不经网关写图；抽取由 Claude Code 会话自己做、不走网关，会话模型在
> claude-haiku-5-5 / claude-opus-5-5 / claude-fable-5-1 间切换；每 500 条自行总结经验教训并用于
> 下一轮；最终对比旧模型存量与 Claude 新内容，并给出实践过的优化意见。
>
> 相关：[DIGEST-RETRO-PROTOCOL.md](DIGEST-RETRO-PROTOCOL.md)（复盘门禁与报告）、
> [gateway-model-quality-20261017.md](gateway-model-quality-20261017.md)（存量画像与指标口径）、
> `config/agent_extract_prompt.md`（抽取规则）、`tools/agent_extract.py`（执行器）、
> `utils/extracted_ingest.py` + `POST /api/ingest_extracted`（服务端）。

## 当前跨工具入口（agent-v5）

本页保留原实验背景。所有新运行按 [公共执行器契约](executors/_common.md) 与
[质量表/修复协议](executors/quality.md) 执行；默认 agent-v5，旧战役仍读自己的冻结快照，不重标旧准入。
使用 `CLI="$KG_HUB_CODE/tools/extract"` 的稳定发布入口，先 preflight，再逐条质量核验、dry-run 和正式提交。
原文/结果/规则变化后凭据失效，需要重审；已提交错误用正规 repair，不换来源键或直连数据库。

## 1. 数据流

```
claude-mem.db 副本（NAS，只读）
   │ tools/agent_extract.py next   ← 质量闸（与 refinery 同 config）+ /api/ingest_extracted/check 跳过已入库
   ▼
batch-bNNNN.json（20 条观测正文 + 身份）
   │ 会话按 config/agent_extract_prompt.md 抽成 {"items":[{id, entities, edges}]}
   ▼
tools/agent_extract.py submit --model <本会话模型>
   │ POST /api/ingest_extracted（sync）：备份 → IngestedKey → 嵌入/消解/写图 → task_executions 记 succeeded
   ▼
主图 kg_hub：episode/新实体/新边带 extractor_model、extraction_mode=agent、prompt_version
```

身份与正文（episode 名、source_description、source_obs_id、正文拼装）与 refinery 逐字段一致，
所以 refinery 恢复后再提交这些观测会得到 `skipped`，自动记为已处理，不会双写。

## 2. 执行会话的循环

每个执行会话固定一个模型，启动时声明 `MODEL=<自己的模型 id>`、`KG_HUB_EXECUTOR=<工具名>`、`KG_HUB_RUN_ID=<run_id>`、`SESSION=<会话 id>`，然后：

```
loop:
  python -m tools.digest_retro gate --state $RETRO --journal $JOURNAL --every 500   # 在 kg-hub-server 容器内或 NAS 上
  rc==2 → 先复盘（§3），再继续
  "$CLI" agent next --n 20          # 得到 batch 文件；picked=0 且「游标之后没有观测」→ 结束
  读 batch 文件，按 prompt 抽取，写 results-bNNNN.json
  "$CLI" agent quality-sheet --batch <batch> --file <results> --out <质量表> --model "$MODEL" --executor "$KG_HUB_EXECUTOR" --run-id "$KG_HUB_RUN_ID"
  逐条审阅原文/lookahead/结果，填写真实证据与判空覆盖，不自动全选通过
  "$CLI" agent quality-check --batch <batch> --file <results> --review <质量表>
  "$CLI" agent submit --batch <batch> --file <results> --review <质量表> --model "$MODEL" --executor "$KG_HUB_EXECUTOR" --run-id "$KG_HUB_RUN_ID" --dry-run
  判读复用候选；前项改变也影响后项pending实体，改动后重审并整批重预览
  "$CLI" agent dryrun-check --batch <batch> --file <results> --model "$MODEL" --executor "$KG_HUB_EXECUTOR" --run-id "$KG_HUB_RUN_ID"
  "$CLI" agent submit --batch <batch> --file <results> --review <质量表> --model "$MODEL" --executor "$KG_HUB_EXECUTOR" --run-id "$KG_HUB_RUN_ID" --session "$SESSION"
  看输出：error/http_4xx 立即停下查原因；stats 里 offschema/dirty/unknown_type 偏高就回头改自己的抽取
```

一批 20 条的节奏：读 + 抽 + 提交约 5–8 分钟。每 500 条（25 批）触发一次复盘。

## 3. 每 500 条复盘（在 DIGEST-RETRO-PROTOCOL 之上的补充）

复盘输入：`tools/digest_retro checkpoint` 的指标（图口径：边名合规、重复三元组、脏命名、摘要长度、
每条耗时）+ `tools/agent_extract status` 的提交口径（offschema/dirty/unknown_type 比例、空抽取比例、
实体/边数）。两者不一致时以图口径为准（那是真正落库的）。

执行者在 lessons.md 里必须回答三件事：这 500 条里自己抽得最差的三类错误是什么（举 id）、
原因是规则没讲清还是模型没遵守、下一轮改什么（改 prompt 版本 → 新的 `prompt_version`；
改批大小；改消解阈值 → 旋钮 `node_match_min`，需服务端支持）。调参只能落在 `digest_retro` 的
旋钮注册表里，并且在 `--applied` 里声明上一轮的调参真的用上了。

## 4. 模型切换与对比设计

- 原实验顺序（不作为其他战役的强制模型清单）：**haiku → opus → fable**，每个模型至少 1 轮（500 条），同一模型内不换 prompt 版本；
  prompt 的改动放在模型切换的边界，并在 lessons 里注明，否则模型差异与 prompt 差异分不开。
- 三模型都跑完 1 轮后，用同一份 50 条抽样（`docs/ab-sample-20261017.json`）各抽一遍作交叉校验：
  同一观测三份抽取并排，人工抽 20 条判实体类型与关系正确率。
- 最终对比表按 `extractor_model` 分组（图里直接 `MATCH (e:Episodic {extraction_mode:'agent'})`），
  再加一列「旧模型存量」（8 月 qwen、10 月 deepseek 的画像），列：实体/条、边/条、边名合规率、
  重复三元组、脏命名、摘要均长、无类型实体、每条耗时、每条 token（会话侧估）。
- 「最优方式」的判据：合规率与脏命名达标（≥90% / ≤2%）前提下，每条耗时与 token 最低者；
  若 haiku 达标，就没有理由用更贵的。

## 4.1 冒烟（2026-10-17）定下的两条执行细节

- **消解误并要当场看。** `submit` 输出里的 `reuse_mismatch` 列出「抽出的名字 → 复用到的旧实体名」；
  服务端已加词面校验（向量阈值 0.95 + 去分隔符包含或 difflib ≥ 0.72），但仍可能并错。
  每批看一眼，误并的写进 lessons，连续出现就提高 `LEXICAL_MIN_RATIO`（服务端旋钮，需发布）。
  2026-10-25 起 dry-run 另给每个复用的被复用节点类型与 summary 开头（`reused` / `reuse_cross_type`，
  见 [executors/campaign.md](executors/campaign.md)「dry-run 的复用回显」）：同名、跨类型的误并也看得见。
- **门禁按图计数。** 早先失败过的观测在账本里已有执行记录，重跑不再记行；`agent_extract retro`
  默认给 digest_retro 传 `--graph-count`，succeeded 数的是图里 `extraction_mode='agent'` 的 episode。
  账本口径的每条耗时对这条路为空，耗时看 `agent_extract status` 的 `latency_med_s`（服务端写图
  的时延，不含会话抽取时间）。

## 5. 红线

- 断路器不动；refinery 保持不提交；本协议不触发任何网关调用。
- 只写主图 `kg_hub`，只经 `/api/ingest_extracted`；不直连 FalkorDB 写。
- 观测确实没有知识就提交空 entities（工具记为 `empty`，不入库）；不要为了凑数硬抽。
- token 只从环境变量读；batch/results 文件里有观测正文，放在状态目录，不要提交进仓库。
