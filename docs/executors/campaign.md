# 抽取对比战役：一个编排者 + 每个模型一个子代理（任何工具通用）

> 目标：在**给定配额**上比较不同模型的抽取效果，每批复盘、改链路，形成常态可复用的流程。
> 消化积压不是目标；但每个模型抽的都是**不同的新观测、结果全部入库**，花的 token 都变成图谱内容（10-22 三模型 2000 条的做法）。
> 工具：`tools/extract_campaign.py`。规则：`config/agent_extract_prompt.md`（agent-v4）+ 战役补充约定。四条红线见 [_common.md](_common.md)。

## 角色

| 角色 | 做什么 | 不做什么 |
|---|---|---|
| **编排者**（主会话） | 开战役；给每个模型派一个子代理并指定模型；每轮看复盘表、抽查正确性、写补充约定并登记；记成本 | 不自己抽取；不替子代理改结果 |
| **子代理**（每个模型一个） | 领批 → 读规则与观测、抽取 → 自检 → dry-run 看误并、改名 → 正式入库 → 汇报 | 不碰断路器、不直连数据库、不改仓库文件、不 git 提交 |

领批在战役的文件锁里推进同一个游标，所以子代理之间、或多个会话之间永远不会领到同一条观测。

## 编排者：可直接粘贴的说明

```text
你是 kg-hub 抽取对比战役的编排者。在仓库根目录执行命令，解释器用 $HOME/.local/share/kg-hub/venv/bin/python（下称 PY）。
战役 id：<战役 id，如 20261101-qoder-ds-qwen>；执行工具：<qoder / codex / cursor / claude-code>；
参与模型：<模型 id 列表，用工具里的准确模型名>；每个模型配额 <200> 条，每批 <50> 条。

1. 开战役（只做一次）：
   $PY -m tools.extract_campaign start --id <战役> --models <模型...> --executor <工具> --quota <配额> --batch <每批>
2. 每一轮：给每个模型派一个子代理，**子代理必须用那个模型**，任务说明用 `$PY -m tools.extract_campaign brief --id <战役> --model <模型>` 的输出
   （或直接用 `agents` 生成的子代理定义，见下文「各工具怎么派」）。
   子代理并行跑；全部回来后：
   $PY -m tools.extract_campaign report --id <战役>
   看每批：入库数、错误、预览误并候选、照抄标题%、译文边%、边名越界、悬空边、零边、credits/条、秒/条。
   再从每个模型本批结果里各抽 3 条，对照观测原文看事实方向、数字、因果有没有写错（评分表看不出这类错误）。
3. 把本轮发现写成 3–6 条补充约定（只写可执行的规则，附一句理由），存成文件后登记：
   $PY -m tools.extract_campaign chain --id <战役> --file <补充约定文件>
   之后领的批次自动带新版本，复盘表按「模型 × 约定版本」分组，改链路前后可以直接比。
4. 子代理汇报的成本（工具用量面板里的 credits / token / 秒）记进去：
   $PY -m tools.extract_campaign cost --id <战役> --batch <批次> --credits <N> --seconds <N>
5. 重复 2–4，直到所有模型领满配额（claim 返回 done）。最后给出：各模型 × 约定版本的合计表、
   每轮改了什么、改完指标怎么变、性价比结论。
```

## 子代理说明：由命令生成，不手抄

说明模板只有一份，在 `tools/extract_campaign.py` 的 `SUBAGENT_BRIEF`。按战役与模型填好：

```
$PY -m tools.extract_campaign brief  --id <战役> --model <模型 id>      # 打印一份，粘给子代理或某个会话
$PY -m tools.extract_campaign agents --id <战役>                        # 每个模型一个子代理定义（JSON，给 qodercli --agents）
$PY -m tools.extract_campaign agents --id <战役> --format md --dir <目录> # 写成带 frontmatter 的定义文件（含 model 字段）
```

子代理的流程：读 `rules` → `claim` 领一批 → 每 10 条一组读全文抽取 → `lint` 自检 → `submit --dry-run` 看误并、改名 →
`submit` 入库 → 输出汇报 JSON（批次、入库数、误并处理、lessons）。红线同 [_common.md](_common.md)。

## 各工具怎么派「指定模型的子代理」

| 工具 | 方式二（一个编排会话派多个模型的子代理） | 证据（2026-10-22 实测） |
|---|---|---|
| **Claude Code** | ✅ 子代理工具带 `model` 参数（opus / sonnet / haiku） | 10-22 三模型 2000 条实验就是这样跑的 |
| **qoder** | ✅ 子代理定义的 `model` 字段生效。命令行：`qodercli -m <编排者模型> --agents "$($PY -m tools.extract_campaign agents --id <战役>)"`；界面：`agents --format md --dir <仓库>/.qoder/agents` | 主会话 Qwen3.8-Max，子代理分别指定 DeepSeek-Flash / Kimi-K3，输出 `modelUsage` 的内部模型名随之变为 `dfmodel` / `kmodel_latest`，credits 按模型分开记 |
| **cursor** | ✅ 子代理定义的 `model` 字段生效，派子代理的工具本身也带 `model` 参数。`agents --format md --dir <仓库>/.cursor/agents` 生成定义，编排者在 cursor 里按名字调用 | 主会话 gpt-5.2，`taskToolCall` 参数里是 `model: gemini-3.7-flash-high`，子代理回复自报 Gemini 3.7 Flash |
| **codex** | ⚠️ 未确认：派生工具自报有 `model` 参数，但 `codex exec --json` 的事件流里两次都没有派生记录（只有一次空的 `wait`），不能确认子代理真按指定模型跑 | 先用退路 |
| **dskhress** | ❓ 本机未安装，接入方式待定 | — |

做不到方式二（或想并行开多个会话）时，用**每个模型一个会话**，仍是同一个战役：

```
$PY -m tools.extract_campaign launch --id <战役> --tool codex|cursor|qodercli [--model <模型>...]
```

打印每个模型的启动命令：子代理说明由 `brief` 现场生成；都把战役状态目录加成可写（`--add-dir`）、放开网络
（子代理要连 NAS 领批、连 kg-hub 提交）；codex 还要关掉标准输入（`< /dev/null`），否则会一直等输入。
这几条命令只在参数层面核对过，还没端到端跑完一整批，第一次用先跑 1 批。

注意：cursor 与 codex 会加载用户级设置里的钩子（实测 cursor 会话开头被注入了一行 kg-hub 上下文）；
对抽取影响不大，但它们的会话会被 claude-mem 记成观测。qodercli 可以用 `--setting-sources local` 避开。

## 和其他用法的关系

- **冒烟**（新模型进场前，不入库）：`tools/extract_bench.py` 在冻结样本上离线评分，见 [qoder.md](qoder.md) 的「对比模式」。
- **qodercli 运行器**（`extract_bench run`）：把 qoder 当纯模型通道、无人值守地比模型本身；不入库，只用于冒烟。
- **战役**（本文）：正式对比，入库，每批复盘、改链路。
