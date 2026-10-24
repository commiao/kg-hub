# 执行器任务说明：Claude Code

> 粘给 Claude Code 会话或子代理的任务说明。公共约束与执行循环见 [_common.md](_common.md)，本文只写 Claude Code 的接入点。

> **多模型生产画像（入库、每批复盘；模型准入看 bench）**：用 [campaign.md](campaign.md) 的编排者 + 子代理流程（`tools/extract_campaign.py`），领批加锁、各模型抽不同的新观测。

> **冒烟 / 准入（不入库）**：Claude 模型可以直接走运行器：`extract_bench run --executor claude --models claude-fable-5-1 claude-opus-5-5 ...`。
> 它调用 `claude -p --safe-mode`（关掉 CLAUDE.md、skill、插件、钩子；`--bare` 只认 API key、订阅登录用不了）、不给工具、
> 系统提示整体替换，和 qodercli / codex 同一套外壳，结果可以放进同一张表。claude 不报 credits，只记 token。

所有新战役使用[公共稳定入口与质量门](_common.md)：`CLI="$KG_HUB_CODE/tools/extract_cli.py"`，先 `"$PY" "$CLI" campaign preflight`，
逐批按 quality-sheet → 逐条证据审阅 → quality-check → dry-run → submit 执行。默认 agent-v5；旧冻结战役及旧准入不重标。
质量表绑定真实 model / executor / run_id：战役从 manifest 自动取；单模型 quality-sheet、submit、dryrun-check 均传同一 `--model --executor --run-id`（后两项可用环境默认）。
quality-check 不重复传身份；前项结果变化后整批重新预览，含后项 pending 实体上下文。

## 任务

你是 kg-hub 会话自抽取路径的执行器，`KG_HUB_EXECUTOR=claude-code`。按 [_common.md](_common.md) 的公共循环跑，
遵守四条红线：只用 `config/agent_extract_prompt.md` 的 schema、每批先质量核验、再 `--dry-run`、不碰断路器 / 不直连 FalkorDB /
不提交观测正文、每批记自报 token 与耗时。

## 接入点

- **模型由会话模型决定**：`--model` 填当前会话真实的模型 id（claude-fable-5-1 / claude-opus-5-5 / claude-haiku-5-5），
  不要填「claude」。换模型就是换 run_id，并且不能同时改规则。
- **子代理或会话直接跑**：`"$PY" "$CLI" agent` 的公共命令都是本机命令；读 batch、写 results 由会话本身完成，
  中间不经任何模型网关。
- **先读规则再抽**：开工时 `"$PY" "$CLI" agent prompt` 把规则重读一遍；每批抽完自检悬空边、自创边名。
- **自报成本**：Claude Code 的 token 用量看会话的用量统计（`/cost` 或 app 的用量面板），每批记一行，
  run 结束时汇总进 `run end --input-tokens --output-tokens`。
- **已用名**：`submit` 会把本会话用过的实体名追加到状态目录的 `names-<模型>.tsv`，抽取前翻一眼，避免同物异名。

## 完成标准

`agent_extract status --group-by run_id` 里本 run 的 error 为 0、`reuse_mismatch_per_obs` ≤ 0.2；
运行清单有 `ended_at` 与自报成本；每 500 条有一份 lessons 并经 `retro -- checkpoint` 登记。
