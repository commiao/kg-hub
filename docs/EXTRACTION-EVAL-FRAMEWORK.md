# 抽取对比与链路优化框架：跨开发工具、跨模型复用

> 2026-10-18。本文把 10-17～10-18 这两天在 kg-hub 上跑通的做法（Claude 会话自抽取三轮、网关路径两批 50 条验证、
> 每 500 条复盘）抽成一套与执行工具无关的机制，供 Claude Code、codex、qoder、cursor、DSH（DeepSeek Harness）等任何能跑命令行、
> 读写 JSON 的工具复用。已有的协议不重写，只引用：
> 执行循环见 [AGENT-EXTRACT-PROTOCOL.md](AGENT-EXTRACT-PROTOCOL.md)，复盘见 [DIGEST-RETRO-PROTOCOL.md](DIGEST-RETRO-PROTOCOL.md)，
> 指标与基线见 [CLAUDE-EXTRACTION-REPLACEMENT-PLAN.md](CLAUDE-EXTRACTION-REPLACEMENT-PLAN.md) 第二部分，
> 网关侧旋钮见 [GATEWAY-EXTRACTION-OPTIMIZATION.md](GATEWAY-EXTRACTION-OPTIMIZATION.md)。

## 0. 判断

可以复用，而且服务端已经是工具无关的：`POST /api/ingest_extracted` 只认 JSON，来源标记（`extractor_model`、
`extraction_mode`、`prompt_version`）跟着 episode 入图，对比工具按标记分组。**缺的不是接口，是四件事**：
把「执行工具」和「模型」拆成两个独立变量并记进来源标记；一份任何工具都照做的执行契约；一个把三个测量面
合成一张表的报表；一份冻结的金标样本。这四件都是小活（§8 有清单）。

有一条要先说清：**开发工具本身不决定抽取质量，模型和规则决定。** Claude Code 跑 fable 和 codex 跑 fable，
抽出来的东西差别应当来自提示词遵守度和循环执行纪律（有没有 dry-run、有没有按批看误并），不是「工具更强」。
所以框架里把工具当「执行器」，只记录、不当作首要对比维度；先比模型和规则，工具差异放在最后用同模型对照。

## 1. 四个变量，一次只动一个

| 变量 | 取值举例 | 在哪记录 | 混杂风险 |
|---|---|---|---|
| 执行器 `executor` | claude-code / codex / qoder / cursor / dsh / script（直连网关的脚本） | 来源标记 `extractor_tool`（已加，与 `run_id` 一起随 episode/实体/边入图） | 会话型执行器的模型随工具走，必须单独记模型 |
| 模型 `model` | claude-fable-5-1 / claude-opus-5-5 / gpt-5-codex / deepseek-v4-flash / qwen3.6-plus | `extractor_model`（已有） | 换模型时不许同时改规则（10-17 三轮犯过） |
| 规则 `rules` | 提示词版本 + schema + 服务端旋钮（消解阈值、归一、自定义指令、max_tokens、空边守卫） | `prompt_version`（已有）+ 运行清单里的旋钮快照 | 服务端旋钮改了要发布，记 commit |
| 样本 `sample` | 冻结样本集 id（`ab-sample-20261017` 50 条分层）或 id 区间 | 运行清单 | 不同批观测的难度不同，跨样本不比密度 |

会话型执行器（Claude Code、codex、qoder）里工具与模型天然绑定：codex 跑的就是它的模型。要单独比「模型」，
用 **脚本执行器**：同一提示词直接经网关调模型（credvault 的 ab 路由，PR commiao/credvault#117 已备好三条禁用路由），
这样 deepseek、qwen、gpt、claude 都在同一个执行器下比，工具这个变量被消掉。要单独比「工具」，让两个工具
配同一个模型（qoder 可配模型）跑同一样本。

## 2. 执行器契约（任何工具照做）

五步，全部是现成命令，执行器只负责中间那一步「读 batch 写 results」：

```
python -m tools.agent_extract next   --n 20 --out batch.json          # 取待抽取观测（NAS 水位线选）
<执行器按 config/agent_extract_prompt.md 抽取，写 results.json：{items:[{id, entities, edges}]}>
python -m tools.agent_extract submit --batch batch.json --file results.json --dry-run   # 看消解会并到谁
python -m tools.agent_extract submit --batch batch.json --file results.json \
        --model <模型id> --session <会话或运行id> [--executor <工具名> --run-id <run_id>]   # 后两项缺省取 KG_HUB_EXECUTOR / KG_HUB_RUN_ID
python -m tools.agent_extract status                                   # 提交口径：ok/empty/reuse_mismatch/时延
python -m tools.agent_extract retro -- checkpoint --lessons-file lessons.md   # 每 500 条
```

每次实验前后各一条：`agent_extract run start --run-id <id> --executor <工具> --model <模型>`（建运行清单，§3）、
`agent_extract run end --run-id <id> --input-tokens N --output-tokens N`（收尾自报成本）。
各工具可直接粘贴的任务说明在 `docs/executors/`（claude-code / codex / qoder，公共部分 `_common.md`）。
多个模型并行的正式对比走「战役」：`tools/extract_campaign.py` + `docs/executors/campaign.md`（编排者 + 每模型一个子代理，领批加锁、全部入库、每批复盘）。

执行器必须满足的四条（放进每个工具的任务说明里）：
1. 抽取只用提示词文件里的 schema，不自创类型和边名；没有知识就提交空 entities，不凑数。
2. 每批先 `--dry-run`，`reuse_mismatch` 里明显并错的要在 results 里改名或在 lessons 记下，再正式提交。
3. 不碰断路器、不直连 FalkorDB、不把观测正文提交进仓库。
4. 每批把执行器自报的 token 与耗时写进运行清单（会话型执行器账本里没有它的成本，只能自报）。

各工具的接入点：
- **Claude Code**：现状，子代理或会话直接跑；模型由会话模型决定。
- **codex**：同一 CLI；把 `config/agent_extract_prompt.md` 作为任务 system 内容。仓库里已有 `tools/codex_hooks.py`，
  可以把「提交前必须 dry-run」做成钩子检查。
- **qoder**：同上，`tools/qoder_cm_hook.sh` 是现成的钩子入口；qoder 可切模型，是做「同工具不同模型」对照的最佳位置。
- **DSH（DeepSeek Harness）**：见 `docs/executors/dsh.md`。
- **其他工具**：只要能跑 Python CLI 并读写 JSON 就能接；不能跑 CLI 的工具退化为「只产出 results.json，由人或脚本提交」。
- **脚本执行器 `tools/model_extract.py`（待写）**：读 batch，用同一提示词经网关的 ab 路由调指定模型，写 results。
  这是比较非 Claude 模型（deepseek、qwen、gpt）而不受工具干扰的唯一办法。

## 3. 运行清单（run manifest）

每次实验一个 `run_id`，本地 `~/.kg-hub/agent-extract/runs/<run_id>.json`，提交时随来源标记入图：

```json
{"run_id": "20261019-fable-v3-s50", "executor": "claude-code", "executor_version": "2.1.x",
 "model": "claude-fable-5-1", "prompt_version": "agent-v3", "server_commit": "30049c9",
 "knobs": {"type_guard_on_resolution": true, "resolution_strictness": "v3", "submit_dry_run": true,
           "gateway.post_normalize": true, "gateway.custom_extraction": true, "gateway.max_tokens": 8192,
           "gateway.edge_empty_retry": false},
 "sample": "ab-sample-20261017", "started_at": "...", "ended_at": "...",
 "self_reported": {"input_tokens": 0, "output_tokens": 0, "wall_s": 0}}
```

旋钮名沿用 `tools/digest_retro.py` 的 `KNOBS` 登记表；网关侧四个旋钮（归一、自定义指令、max_tokens、空边守卫）
已登记为 `gateway.*`（前三个是服务端环境变量 `KG_HUB_POST_NORMALIZE` / `KG_HUB_CUSTOM_EXTRACTION` / `KG_HUB_LLM_MAX_TOKENS`，
改后需发布；空边守卫未实现，只能记为建议）。没登记的旋钮 `checkpoint` 和 `run start` 都会拒绝，这是防「改了没记」的闸。

## 4. 三个测量面合成一张表

| 测量面 | 来源 | 现成工具 | 关键列 |
|---|---|---|---|
| 图口径（质量） | FalkorDB，按 `extractor_model` / `extractor_tool` / `run_id` 分组 | `tools/extractor_compare.py`、`tools/ab_score.py`、`digest_retro graph_metrics` | 实体/条、边/条、边名合规、边名种类、无类型、脏命名、重复三元组、零边、摘要均长 |
| 提交口径（消解与纪律） | `/api/ingest_extracted` 返回与状态目录 | `agent_extract status` | ok/empty/skipped、reuse_mismatch/条、服务端写图时延 |
| 成本口径 | 网关路径：账本 `/backup/model-attempts.sqlite3`（按 `source_description` 前缀索引取）；会话路径：运行清单自报 | `ab_score --journal`、本文 §4.2 的统计脚本思路 | 每条调用次数、每条输入/输出 token、每条 wall、阶段分布 |

`tools/eval_report.py --run-id A --run-id B ...`（已写）：读三面，按 run（或 `--model` / `--executor`）并排成一张 markdown 表，
列顺序固定为纪律 → 丰富度 → 消解 → 成本 → 检索（`tools/kg_eval.py` 固定问题集的召回，`--recall label=值` 手填）。
图口径在容器外用 `--graph-json`（容器内 `extractor_compare --json --group-by run_id` 的导出）喂入；
成本「自报」（运行清单）与「账本」（`--journal`，复用 ab_score 口径）分列不相加。判据沿用替代方案第二部分 §2 的目标列。

网关路径的成本基线（2026-10-18 第二批 54 条，deepseek）：每条 7.07 次调用、67.6k 输入 token；属性 37%、消解 28%、
抽边 18%、去重 14%。任何「链路优化」的成本影响都按这张分布算增量，不凭感觉。

## 5. 样本与金标

- **冻结样本**：`docs/ab-sample-20261017.json`（50 条分层：项目 × 类型 × 长度）。新样本要同样分层并冻结 id 列表，
  跨实验只比同一样本。
- **对比样本 bench-20261022（离线，现行）**：三模型 2000 条实验 chain-v8 那 6 批共 200 条（haiku 66 / opus 67 / sonnet 67），
  基线就是它们当时的结果，问句是只看标题写的中英换说法各一句。`tools/extract_bench.py` 负责冻结（`freeze`）、
  自检（`lint`）与评分（`score`：纪律、密度、照抄标题率、译文边率、换说法问句 top-10 可检索率，并按基线模型逐对比较）。
  样本、基线、问句含观测正文，只在本机 `~/.kg-hub/bench/`，不进仓库。已入图的观测再提交会被挡成 skipped，
  所以换工具/换模型的对比都走这条离线路，不入主图；执行步骤见 `docs/executors/qoder.md` 的「对比模式」。
  bench 同时是**准入**：`extract_bench blind` + `admit` 按硬性、质量、正确性门槛给出 pass / fail，记录在本机；
  战役（`extract_campaign`）只接受 pass 的模型，见 `docs/executors/campaign.md`。
- **金标（待建）**：从冻结样本里取 20 条，人工给出实体（名、类型）与关系（源、目标、边名）的参考答案，存
  `docs/gold/<sample>.json`。有了它才能算 **正确率**（现在只能算合规率和密度，密度高不等于对）。评分：
  实体名匹配走服务端同一套词面规则（`utils/extracted_ingest._lexically_same`），类型要相等；关系匹配要两端实体
  都匹配且边名相等或同义（同义表写在金标文件里）。
- **检索侧**：`tools/kg_eval.py` 的固定问题集按北极星三场景出题，每个 run 入图后跑一遍，看召回不退化。

## 6. 实验矩阵与决策规则

1. **先规则后模型**：用最便宜的模型把规则调到纪律达标（合规 ≥ 90%、脏命名 ≤ 2%、无类型 0、零边 ≤ 5%），
   再换模型比。规则没达标就换模型，差异归因不了。
2. **冒烟 50、正式 500**：每格先 50 条冒烟（同冻结样本），过线再跑 500 条正式（每 500 条复盘一次）。
3. **一格一变量**：矩阵的行是模型，列是执行器，每格固定规则版本与服务端 commit；格内任何改动都开新 run_id。
4. **决策**：纪律与误并达标的前提下，取 **每条成本最低** 者；两者接近时看金标正确率和 kg_eval 召回；
   不看密度（密度高可能是过抽）。
5. **工具对照放最后**：同一模型在两个工具下跑同一样本，差异只应出现在提交口径（dry-run 执行率、reuse_mismatch 处理）
   和自报成本；图口径若差很多，先查提示词有没有被工具改写。

## 7. 链路优化循环

每 500 条一轮复盘（DIGEST-RETRO-PROTOCOL），每条优化意见必须满足：
- 落到一个 **登记过的旋钮**，改之前记旧值、改之后开新 run_id；
- 有 **对照批**：同样本、只改这一个旋钮；
- 报告里写 **成本增量**（按 §4 的分布算）和 **质量增量**（三面各一行）。

已经实践并可直接复用的优化（按生效位置）：
- 服务端消解：向量 0.95 + 词面校验 + 类型守卫 + 代码名精确匹配（误并率 0.76 → 0.15）；
- 入图后归一：边名归 schema 保留原名、无类型兜底、脏命名降级（弱模型合规 36% → 100%）；
- 提示词：schema 纪律写进 `custom_extraction_instructions`（模型层合规 36% → 95%）；max_tokens 8192（长观测截断）；
- 执行纪律：每批 dry-run、每 500 条复盘并把教训改进下一轮提示。

未验证、只作实验开关的：抽边空结果守卫（零边 20% 的成因是模型单次返回空；见对比报告 §4.2，成本上限 +7% 调用）。

## 8. 要补的工作（按依赖顺序）

| # | 事项 | 位置 | 大小 |
|---|---|---|---|
| 1 | 来源标记加 `extractor_tool`、`run_id`；`agent_extract submit` 加 `--executor`、`--run-id`；`extractor_compare` 支持按这两列分组 | utils/extracted_ingest.py、tools/agent_extract.py、tools/extractor_compare.py | 已做（PR #188）；服务端部分需发布后才入图 |
| 2 | `KNOBS` 登记网关侧四个旋钮；运行清单读写（`agent_extract run start/end`） | tools/digest_retro.py、tools/agent_extract.py | 已做（PR #188） |
| 3 | 脚本执行器 `tools/model_extract.py`：同一提示词经网关 ab 路由调任意模型 | 依赖 credvault#117 启用路由 + 凭据（用户侧） | 中 |
| 4 | `tools/eval_report.py`：三面合一张表 | 新文件，复用 ab_score / extractor_compare / status | 已做（PR #188） |
| 5 | 金标 20 条 + 评分器 `tools/gold_score.py` | docs/gold/、新文件 | 中，人工部分由用户或执行者判 |
| 6 | 各工具的任务说明模板（claude-code / codex / qoder 各一份 + 公共部分，内容就是 §2 的四条 + 五步命令） | docs/executors/ | 已做（PR #188）；cursor、DSH 随战役补齐（docs/executors/cursor.md、dsh.md） |

1、2、4、6 已在 PR #188 做完（其中 1 的服务端改动要发布 kg-hub-server 才会写进图；发布前 `--executor` / `--run-id`
只记在本地 log 与运行清单里，服务端按 pydantic 默认忽略多余字段不报错）；3 依赖网关路由与凭据；5 的人工判定需要有人花半天。

## 9. 风险与边界

- 会话型执行器的成本只能自报，和账本口径不可直接相加；报表里分列，不合并。
- 同一观测被两个 run 处理时后到者 `skipped`，所以多 run 并行必须用不同样本或独立图（`--database kg_hub_ab`）。
- 金标会过时（schema 改版要重判）；每次 schema 变更给金标打版本。
- 断路器与 held 放回仍是用户决定；框架里所有网关路径实验都以「用户恢复断路器跑 N 条」为前提。
