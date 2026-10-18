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

## 完成标准

与 Claude Code 相同：本 run error 为 0、`reuse_mismatch_per_obs` ≤ 0.2、运行清单有 `ended_at` 与自报成本、
每 500 条有 lessons 并登记。
