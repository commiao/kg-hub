# 执行器任务说明：codex

> 粘给 codex 会话的任务说明。四条红线与五步命令见 [_common.md](_common.md)，本文只写 codex 的接入点。

## 任务

你是 kg-hub 会话自抽取路径的执行器，`KG_HUB_EXECUTOR=codex`。按 [_common.md](_common.md) 的五步命令跑，
遵守四条红线：只用 `config/agent_extract_prompt.md` 的 schema、每批先 `--dry-run`、不碰断路器 / 不直连 FalkorDB /
不提交观测正文、每批记自报 token 与耗时。

## 接入点

- **同一 CLI**：`python -m tools.agent_extract` 的五步命令与 Claude Code 完全一样；codex 只负责「读 batch → 写 results」。
- **规则作为 system 内容**：把 `config/agent_extract_prompt.md` 全文放进任务的 system / developer 消息，
  而不是让模型从对话里「记得」规则；规则版本以 `tools/agent_extract.PROMPT_VERSION` 为准。
- **模型**：`--model` 填 codex 实际使用的模型 id（如 gpt-5-codex）；codex 的工具与模型绑定，
  要单独比「模型」请用脚本执行器（框架 §1），要单独比「工具」请让 qoder 配同一模型跑同一样本。
- **提交前必须 dry-run 由工具自己把关**：`agent_extract submit` 正式提交时会核对本批是否 dry-run 过，没预览过的 id
  直接拒绝。要在 codex 的 pre-command 钩子里提前拦，就调 `python -m tools.agent_extract dryrun-check --batch <b> --file <r>`
  （rc 1 = 有没预览的）；仓库里的 `tools/codex_hooks.py` 读的是 codex 原生钩子清单，可用来确认钩子装没装。钩子只做检查，不改 results。
- **自报成本**：codex 会话的 token 看其用量面板；每批记一行，run 结束时汇总进 `run end`。

## 完成标准

与 Claude Code 相同：本 run error 为 0、`reuse_mismatch_per_obs` ≤ 0.2、运行清单有 `ended_at` 与自报成本、
每 500 条有 lessons 并登记。
