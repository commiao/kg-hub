# 执行器任务说明：qoder

> 粘给 qoder（桌面版或 IDEA 插件）会话的任务说明。四条红线与五步命令见 [_common.md](_common.md)，本文只写 qoder 的接入点。

## 任务

你是 kg-hub 会话自抽取路径的执行器，`KG_HUB_EXECUTOR=qoder`。按 [_common.md](_common.md) 的五步命令跑，
遵守四条红线：只用 `config/agent_extract_prompt.md` 的 schema、每批先 `--dry-run`、不碰断路器 / 不直连 FalkorDB /
不提交观测正文、每批记自报 token 与耗时。

## 接入点

- **同一 CLI**：五步命令不变；qoder 在终端里跑 `python -m tools.agent_extract`，自己完成「读 batch → 写 results」。
- **规则作为任务 system 内容**：把 `config/agent_extract_prompt.md` 全文放进任务说明开头。
- **qoder 可切模型，是「同工具不同模型」对照的最佳位置**：同一样本（`--sample ab-sample-20261017`）、同一规则版本，
  只换 qoder 里选的模型，每个模型一个 run_id，`--model` 填真实模型 id。这样图口径的差异才能归因到模型。
- **提交前必须 dry-run 由工具自己把关**：`agent_extract submit` 正式提交时会核对本批是否 dry-run 过，没预览过的 id
  直接拒绝。要在 qoder 钩子里提前拦，调 `python -m tools.agent_extract dryrun-check --batch <b> --file <r>`（rc 1 = 有没预览的）；
  `tools/qoder_cm_hook.sh` 是现成的 qoder 钩子包装（它负责把项目目录传对），可挂在同一入口上。钩子只检查不改 results。
- **项目目录**：IDEA 插件的 `process.cwd()` 指向插件目录，命令要在仓库根目录下跑（`cd` 到 kg-hub 检出再执行），
  否则 `tools/agent_extract` 找不到 `config/`。
- **自报成本**：qoder 的 token 看其用量面板；每批记一行，run 结束时汇总进 `run end`。

## 对比模式：换模型 / 换工具参与对比（不提交、不写图）

想知道「qoder + 某个模型」比 opus / sonnet / haiku 5.5 好还是差，用冻结样本离线比，**不要**走上面的正式提交：
那 200 条观测已经入过主图，再提交会被挡成 skipped；`next` 取到的新观测难度不同，跨样本不能比。

1. 看样本：`python -m tools.extract_bench parts --bench ~/.kg-hub/bench/bench-20261022`
   （200 条，6 个批次文件；样本与基线含观测正文，只在本机，不进仓库）。
2. qoder 里选好模型，任务说明开头放 `python -m tools.agent_extract prompt` 的全文（agent-v4），
   对每个批次文件读全文、按规则抽取，写 `results-<批次>.json` 到你自己的目录（如 `~/.kg-hub/bench/runs/qoder-<模型>/`）。
   每批 50 条较长，分 10 条一组写分片再合并；同时记下 qoder 用量面板里的 token 与耗时。
3. 自检：`python -m tools.extract_bench lint --bench <样本目录> --results <你的目录>`，按提示修到「自检通过」
   （悬空边、schema 外类型/边名、缺 edges 键、第一条边没照抄标题、第二条边不是另一语言）。
4. 评分：`python -m tools.extract_bench score --bench <样本目录> --results <你的目录> --label qoder-<模型>
   --model <真实模型 id> --executor qoder --tokens <总 token> --wall-s <总秒数> --json <输出>.json`。
   输出一张表：全样本一行、三模型基线合计一行，再按基线模型各自那部分逐对比较（同一批观测上的候选 vs 基线）。
   可检索率与报告同一口径（换说法问句 top-10），基线回放实测与报告逐格一致。

四个变量一次只动一个：比**模型**，qoder 换模型、规则与样本不动；比**工具**，qoder 选 claude-sonnet-5-5 与基线里 Claude Code
跑的 sonnet 那部分比。评分要用本机 kg-hub venv（需要 numpy、fastembed 和本机的 MiniLM 缓存），约半分钟。
dry-run 在对比模式下不需要：样本观测的实体已在图里，预览出的「误并候选」大多是它自己上次写的名字，没有比较意义。

## 完成标准

与 Claude Code 相同：本 run error 为 0、`reuse_mismatch_per_obs` ≤ 0.2、运行清单有 `ended_at` 与自报成本、
每 500 条有 lessons 并登记。
