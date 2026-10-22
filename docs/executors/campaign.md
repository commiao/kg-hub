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
2. 每一轮：给每个模型派一个子代理，**子代理必须用那个模型**，任务说明用下面「子代理说明」，填好战役 id 与模型 id。
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

## 子代理：可直接粘贴的说明

```text
你是 kg-hub 抽取对比战役的执行者，战役 <战役 id>，你的模型 id 是 <模型 id>。
在仓库根目录执行命令，解释器用 $HOME/.local/share/kg-hub/venv/bin/python（下称 PY）。

1. 读规则：$PY -m tools.extract_campaign rules --id <战役>（agent-v4 正文 + 战役补充约定），尤其末尾规则 21–28。
2. 领一批：$PY -m tools.extract_campaign claim --id <战役> --model <模型 id>
   返回 batch_file（要抽的观测）与 results_file（你要写的结果）。返回 done 就结束并汇报。
3. 抽取：每次读 batch_file 里 10 条，**读全文再抽**；10 条一组写分片（放在 results_file 同目录、文件名带批次号），
   全部写完用 python 合并成 results_file：{"items": [{"id", "entities": [{"name","type","summary"}],
   "edges": [{"source","name","target","fact"}]}]}。没有知识的观测给 "entities": []。
4. 自检：$PY -m tools.extract_campaign lint --id <战役> --batch <批次>，改到「自检通过」。
5. 预览：$PY -m tools.extract_campaign submit --id <战役> --batch <批次> --dry-run
   逐条看 reuse_mismatch（抽出名 → 会并到的旧名）：并错的（不同的东西、更泛的旧名）按规则 14 改名，再自检、再预览；
   标「（同批新建）」的是本批前面条目会新建的实体，判法相同。
6. 入库：$PY -m tools.extract_campaign submit --id <战役> --batch <批次>（逐条写图，可能超过 2 分钟，给足超时）。
7. 汇报（只输出这个 JSON）：
   {"batch": "<批次>", "model": "<模型 id>", "submitted": {"ok": 0, "empty": 0, "error": 0},
    "dryrun_mismatch_seen": 0, "renamed_after_dryrun": 0, "credits": null, "seconds": null,
    "lessons": ["最多 4 条：规则哪里不好用、哪类观测难抽、你做了什么取舍"]}

红线：不碰断路器、不直连 FalkorDB、不改仓库文件、不 git 提交、不读或打印 .env；提交报错只重试一次，仍失败就停下汇报。
```

## 各工具怎么派「指定模型的子代理」

| 工具 | 方式二（一个编排会话派多个模型的子代理） | 退路（每个模型一个会话） |
|---|---|---|
| **Claude Code** | 已验证：子代理工具带 `model` 参数（opus / sonnet / haiku），10-22 实验就是这样跑的 | — |
| **qoder** | **待验证**：自定义 agent 的定义里能否写 `model:`（`~/.qoder/agents` 的示例只有 name / description / permissionMode / skills / mcpServers）。验证方法：定义一个只回「ok」的 agent 并指定模型，用 `qodercli -p -o json` 让主会话调用它，看输出 `modelUsage` 里是否出现那个模型 | 开多个 qoder 会话（或多个 `qodercli` 进程），每个选一个模型，粘「子代理说明」；编排者只做开战役、复盘、登记约定 |
| **codex** | 待验证：子代理能否单独指定模型 | 每个模型开一个 `codex` 会话（`-m <模型>`），粘「子代理说明」 |
| **cursor** | 待验证：后台 agent / 子代理能否单独指定模型 | 每个模型开一个 agent 窗口并选模型，粘「子代理说明」 |

两种方式用的是同一个战役：领批加锁、复盘表按模型分组，所以混用也没问题（比如两个模型在一个编排会话里，第三个模型另开会话）。
`--executor` 记的是工具名，模型名用工具里的准确写法（来源标记只允许字母数字 `. _ : / -`）。

## 和其他用法的关系

- **冒烟**（新模型进场前，不入库）：`tools/extract_bench.py` 在冻结样本上离线评分，见 [qoder.md](qoder.md) 的「对比模式」。
- **qodercli 运行器**（`extract_bench run`）：把 qoder 当纯模型通道、无人值守地比模型本身；不入库，只用于冒烟。
- **战役**（本文）：正式对比，入库，每批复盘、改链路。
