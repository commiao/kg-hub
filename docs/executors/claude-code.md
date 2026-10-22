# 执行器任务说明：Claude Code

> 粘给 Claude Code 会话或子代理的任务说明。四条红线与五步命令见 [_common.md](_common.md)，本文只写 Claude Code 的接入点。

> **多模型正式对比（入库、每批复盘）**：用 [campaign.md](campaign.md) 的编排者 + 子代理流程（`tools/extract_campaign.py`），领批加锁、各模型抽不同的新观测。

## 任务

你是 kg-hub 会话自抽取路径的执行器，`KG_HUB_EXECUTOR=claude-code`。按 [_common.md](_common.md) 的五步命令跑，
遵守四条红线：只用 `config/agent_extract_prompt.md` 的 schema、每批先 `--dry-run`、不碰断路器 / 不直连 FalkorDB /
不提交观测正文、每批记自报 token 与耗时。

## 接入点

- **模型由会话模型决定**：`--model` 填当前会话真实的模型 id（claude-fable-5-1 / claude-opus-5-5 / claude-haiku-5-5），
  不要填「claude」。换模型就是换 run_id，并且不能同时改规则。
- **子代理或会话直接跑**：`python -m tools.agent_extract` 的五步都是本机命令；读 batch、写 results 由会话本身完成，
  中间不经任何模型网关。
- **先读规则再抽**：开工时 `python -m tools.agent_extract prompt` 把规则重读一遍；每批抽完自检悬空边、自创边名。
- **自报成本**：Claude Code 的 token 用量看会话的用量统计（`/cost` 或 app 的用量面板），每批记一行，
  run 结束时汇总进 `run end --input-tokens --output-tokens`。
- **已用名**：`submit` 会把本会话用过的实体名追加到状态目录的 `names-<模型>.tsv`，抽取前翻一眼，避免同物异名。

## 完成标准

`agent_extract status --group-by run_id` 里本 run 的 error 为 0、`reuse_mismatch_per_obs` ≤ 0.2；
运行清单有 `ended_at` 与自报成本；每 500 条有一份 lessons 并经 `retro -- checkpoint` 登记。
