# 观测治理循环：规则分诊、冻结集与评分（obs triage）

> 目标（用户 2026-10-24）：诊断新增观测的数量与质量，用规则而不是 LLM 把低质量观测尽量多地挡在图外，
> 并把「怎么检测」沉淀成可重复的东西，形成「采样 → 标注 → 冻结 → 改规则 → 评分 → 影子 → 启用」的循环。
> 目标函数不是「丢得越多越好」，而是**误杀率可控的前提下最大化丢弃**——留了垃圾看得见、能补扫，杀了好东西看不见。

## 0. 一句话

现行入库闸（`utils/ingest_filter.py`）按类型和长度打分，看不见「PR 合了」和「根因是 X」的差别。
分诊（`utils/obs_triage.py`）在闸之上加七条规则：五条扣分（状态报告、过程记录、会话内临时状态、测试快照、例行代码改动）、
一条快照/差异扣分、一条「讲为什么」加分，把闸放行的观测分成 keep / review / drop。
在 223 条盲标样本上：**高价值误杀 8.7% → 4.3%（没多杀一条，还救回 3 条），低价值丢弃 32% → 80%，净丢弃 18.8% → 33.6%**，
两半样本结果一致（dev/holdout）。现在是影子模式：只统计不改判。

## 1. 件在哪

| 件 | 位置 | 说明 |
|---|---|---|
| 规则正文 | `utils/obs_triage.py` `RULES` | 每条有 name / kind / since / doc / 正则；`matches()` 给命中原文，调规则时看它 |
| 权重与开关 | `config/obs_triage.json` | `enabled`（影子 / 启用）、`review_band`、`rescue_max_gap`、`penalty_waived_at_rationale_hits`、每条规则 weight |
| 对比套件 | `tools/obs_bench.py` | sample / sheet / freeze / score / shadow |
| NAS 导出 | `tools/obs_sample_remote.py` | 只依赖标准库，经 ssh 送到 NAS 跑；**30 天带正文约 3 万行，只在 23 点后跑** |
| 冻结集 | `~/.kg-hub/bench/obs-20261024/`（`KG_HUB_BENCH_ROOT` 可改） | items.jsonl（含正文，不进仓库）、labels.jsonl、population.json、manifest.json、runs/ |
| 消费点 | `tools/agent_extract.select_batch` | 闸放行后再分诊；影子只计 `triage_drop` / `triage_review`，启用后 drop 档跳过计 `triage_dropped`。战役 claim 和 next 共用 |
| 测试 | `tests/test_obs_triage.py` | 每条规则一正一反、加减分、三档、闸判定还原、冻结→评分端到端、选批接入 |

## 2. 分诊怎么判

```
base = ingest_filter.evaluate(obs)            # 现行闸，不改
hits = 各规则命中次数（标题段 / 全文段：标题+副标题+正文前 800 字+facts）
adj  = Σ 扣分（每条规则只算一次）+ 加分（rationale 按命中次数封顶 cap）
       扣分时再压掉类型权重超出 change 的部分（feature 多拿 65、bugfix 75：过程记录不该因标签多拿分）
       rationale 命中 ≥ 3 时免扣（标题像状态报告、正文讲清根因的是知识）
score = base.score + adj
```

| 情况 | 档 |
|---|---|
| 闸硬拒（类型不在白名单 / 模型黑名单 / 正文过短且无 facts） | drop，不救 |
| 闸按分数拒、差 ≤ `rescue_max_gap`、加分后够线 | review（救回只到 review） |
| 闸按类型豁免（decision / bugfix / security）且没扣到线下 | keep |
| 扣到线下 | decision / security_* → review（零误杀钉子）；其余 → drop |
| 线上但 < 线 + `review_band` | review |
| 其余 | keep |

review 档是将来「只对边界用最便宜模型复核」的预算，token 只花在这里。

## 3. 第一次评分（triage-v1，2026-10-24）

样本：fork-2 会话 2026-10-24 抽的最近 30 天 223 条（按项目分层、每会话最多 3 条），子代理盲标 H/M/L = 69/79/75，
topic_mismatch 48。总体 28,338 条、现行闸放行 80%。

| 组 | n | 高 keep/review/drop | 中 | 低 | 误杀 分诊/基线 | 低价值丢弃 分诊/基线 | 净丢弃 分诊/基线 | review |
|---|---|---|---|---|---|---|---|---|
| all | 223 | 56/10/3 | 47/20/12 | 5/10/60 | 4.3% / 8.7% | 80.0% / 32.0% | 33.6% / 18.8% | 17.9% |
| dev（偶数 id） | 106 | 22/2/1 | 28/10/4 | 3/3/33 | 4.0% / 8.0% | 84.6% / 35.9% | 35.8% / 18.9% | 14.2% |
| holdout（奇数 id） | 117 | 34/8/2 | 19/10/8 | 2/7/27 | 4.5% / 9.1% | 75.0% / 27.8% | 31.6% / 18.8% | 21.4% |

| 规则 | 命中 H/M/L | 低价值占比 |
|---|---|---|
| rationale（加分） | 34/26/6 | — |
| status_report | 0/5/14 | 74% |
| snapshot_diff | 1/1/8 | 80%（H 命中 40894，正文 rationale 够多、未扣） |
| process_record | 0/0/10 | 100% |
| routine_code_change | 0/0/7 | 100% |
| session_ephemeral | 0/0/6 | 100% |
| test_snapshot | 0/0/5 | 100% |

高价值落 drop 的 3 条（42970、51331、30345）都是现行闸本来就拒的，分诊一条没多杀；救回 55837 / 32433 / 51556（都是 H）。
剩下 5 条低价值 keep：两条 decision（受保护）、一条 bugfix 清理、两条没命中任何规则——下一版规则从它们和 review 档里找。

估到总体（按 30 天 28,338 条、80% 放行）：影子模式启用后约 **每天多拦 140 条、少花约 140 次抽取的 token**，误杀不升。
这是样本外推，启用前先看影子统计（§4 第 6 步）。

## 4. 循环（每轮都走一遍，缺一步不算数）

1. **采样**：`python -m tools.obs_bench sample --days 30 --n 200 --out sample.jsonl`（23 点后；带现行闸判定与会话-日规模）。
2. **出盲标表**：`python -m tools.obs_bench sheet --sample sample.jsonl --out sheet.json`（只有标题正文，没有分数）。
3. **标注**：交给只拿 sheet 的 Claude 子代理（与 bench 盲评同规矩：不先看对照表），每条给
   `{"id", "value": "H|M|L", "reason": 一句话, "topic_mismatch": bool}` 写成 labels.jsonl。
   H = 三个月后还会用得上的结论/根因/约定；M = 能用但从代码或文档也能查到；L = 状态、过程、一次性结果。
   **取用数据一到位就换标尺**：图里 `usage_count > 0` 的观测直接当 H、长期为 0 的当 L（T-0349 复核取用后接上），人工标注只补边界。
4. **冻结**：`python -m tools.obs_bench freeze --sample sample.jsonl --labels labels.jsonl --bench-id obs-<日期>`（不覆盖旧集）。
5. **改规则 → 评分**：改 `utils/obs_triage.py` 或 `config/obs_triage.json` 后
   `python -m tools.obs_bench score --bench obs-<日期> --label <改了什么>`，看三件事：
   误杀率不升（H 落 drop 只能是闸本来就拒的）、低价值丢弃上升、dev 与 holdout 差距小（差得多就是在背样本）。
   运行记录落在 `<bench>/runs/`，带规则版本和配置摘要；版本号在 config `version`，规则加了就升。
6. **影子**：不用手动看，结果会出现在两处（§4.1）。影子跑够一轮战役后，
   `python -m tools.triage_notify sample --id <战役>` 从判丢档抽 20 条复查，没有 H 才翻 `enabled`。
7. **启用后的护栏**：drop 的观测不删，留在 claude-mem 里、游标越过；每轮战役 report 顺带从 drop 档抽样复查，误杀率有数才算「可控」。

### 4.1 结果在哪看、怎么通知（2026-10-24 起）

用户关心三件事：**高价值保留率、低价值保留率、这一阶段的 token**。这三件事落在两处：

| 位置 | 内容 | 什么时候 |
|---|---|---|
| 战役复盘报告 `extract_campaign report` | 每批与按模型合计多 6 列：闸拒%、分诊判丢、判丢%、分诊判复核、判复核%、分诊已丢；报告 JSON 顶部 `triage` 段有按模型汇总与可省 / 已省 token | 每轮复盘 |
| 飞书（kg-hub webhook） | 一条消息三行：保留率（冻结样本上当前规则最新评分，分诊 / 现行闸）、过滤（闸拒、判丢、判复核）、token（影子：启用可省约 N；启用后：已省约 N） | 战役打满时一次（`extract_campaign submit` 正式提交后检查）；每周日 09:40（launchd `com.kg-hub.triage-digest`），顺带补推漏掉的打满通知 |

- 推送工具：`tools/triage_notify.py`（digest / weekly / check-full / sample）。推送失败只记 stderr，不影响提交；
  打满通知每条战役只推一次，推成功才记在 `~/.kg-hub/agent-extract/triage-notify.json`。
- token 估算 = 判丢条数 × 该模型本战役自报的 token/条（`cost --tokens`）；没记 token 的战役显示「—」。
- 保留率没有逐条标签可算，用冻结样本的评分代替；换了规则版本还没评分时，消息里会直说「还没评过分」。
- 判丢 / 判复核的条目在批次文件里带 `triage_tier`，`sample` 就从这里抽。10-24 13:56 Mac 发布切到含 #238 的版本之前领的批没有分诊计数，报告显示「—」，摘要里合并成一行。
- 分母口径：闸拒% = 闸拒 / 过闸前看过的；判丢% 与判复核% = 各档 / 分诊看过的（领到 + 已入库 + 别处在抽 + 启用后跳过）。合计比例从相加后的计数重算。

## 5. 加一条规则的规矩

- 在 `RULES` 里加 `Rule(...)`：名字、`since` 写版本、`doc` 写它判什么和为什么是垃圾（或为什么是知识）；中英文正则分开。
- `config/obs_triage.json` 给权重；不给或 0 = 不启用。
- `tests/test_obs_triage.py` 加一正一反。
- 跑 §4 第 5 步，把运行记录的数字写进本文 §3（替换，不堆叠）。
- 词表别从标注理由里抄：标注员写「例行清理」，观测正文里不会出现这四个字；v1 调试时因此误加过几条零命中的中文模式。

## 6. 变异验证（2026-10-24，14 项全部转红）

逐条把实现改坏、先断言变异真的改上了、跑 `tests/test_obs_triage.py` 必须转红、再还原（deploy-standard 准则 17）。
脚本在会话暂存区，不进仓库；下面是清单，改了实现就照着重做一遍：

| 改坏什么 | 接住它的断言 |
|---|---|
| 去掉扣分时的类型权重压制 | feature 状态报告的分数必须比基础分低 90 以上 |
| 去掉 rationale ≥ 3 免扣 | 状态标题 + 3 次 rationale 的调整分 = +15 |
| 去掉救回 | 差 8 分、正文讲根因的观测必须到 review |
| decision 不再受保护 | 被扣到线下的 decision 必须是 review 不是 drop |
| `health=\d+` 退回 `\d` | 「Dashboard health=200 …」必须命中 status_report |
| 去掉 review 带 | 66 分（60 ≤ x < 75）必须是 review |
| 扣分按命中次数累加 | 命中 3 次仍只扣 30 |
| `enabled` 判断写死 | 随仓库发布的配置必须是影子模式 |
| 启用后不跳过 drop 档 | enabled=true 时 select_batch 只剩那条知识 |
| 影子模式不计数 | triage_drop 必须是 2 |
| 冻结不校验样本与标签 id | 标签少一条必须 SystemExit |
| dev/holdout 分组失效 | 偶数 3 条 / 奇数 2 条 |
| 误杀率写死 0 | 硬闸拒的 H 必须算成 50% |
| 闸判定还原丢分数 | 还原出的 score 必须等于记录的 51.8 |

三项是第一轮没转红后补的断言（原断言被别的路径满足了：`deployed at` 顺带命中了 `health=`、
两组总数 4 对「全归 dev」也成立、误杀率本来就期望 0）——变异验证同时在验断言选得对不对。

## 7. 没做、下一步

- **近重复规则**：同项目短时间内标题高度相似的观测只留第一条，需要流式窗口，先在 shadow 里统计再定权重。
- **会话洪峰**：会话-日 p99 = 298 条、最大 2,881。fork-2 模拟过「每会话每天限额」会把高价值压掉，不用硬限额；
  候选是「同会话内按分诊分排序，超出名额的进 review」。
- **review 档的 LLM 复核**：只对 review 档、用最便宜的模型、一次只判「是知识还是过程」，token 预算 = review 占比 × 领批量。
- **取用标尺**：T-0349 复核后把 `usage_count` 接进 §4 第 3 步，标注从人工变成自动。
