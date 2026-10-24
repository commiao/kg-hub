# 执行器任务说明：codex

> 粘给 codex 会话的任务说明。公共约束与执行循环见 [_common.md](_common.md)，本文只写 codex 的接入点。

> **多模型生产画像（入库、每批复盘；模型准入看 bench）**：用 [campaign.md](campaign.md) 的编排者 + 子代理流程（`tools/extract_campaign.py`），领批加锁、各模型抽不同的新观测。

所有新战役使用[公共稳定入口与质量门](_common.md)：`CLI="$KG_HUB_CODE/tools/extract"`，先 `"$CLI" campaign preflight`，
逐批按 quality-sheet → 逐条证据审阅 → quality-check → dry-run → submit 执行。默认 agent-v5；旧冻结战役及旧准入不重标。
质量表绑定真实 model / executor / run_id：战役从 manifest 自动取；单模型 quality-sheet、submit、dryrun-check 均传同一 `--model --executor --run-id`（后两项可用环境默认）。
quality-check 不重复传身份；前项结果变化后整批重新预览，含后项 pending 实体上下文。

## 任务

你是 kg-hub 会话自抽取路径的执行器，`KG_HUB_EXECUTOR=codex`。按 [_common.md](_common.md) 的公共循环跑，
遵守四条红线：只用 `config/agent_extract_prompt.md` 的 schema、每批先质量核验、再 `--dry-run`、不碰断路器 / 不直连 FalkorDB /
不提交观测正文、每批记自报 token 与耗时。

## 接入点

- **同一 CLI**：`"$CLI" agent` 的公共循环与 Claude Code 完全一样；codex 只负责「读 batch → 写 results」。
- **规则作为 system 内容**：把 `config/agent_extract_prompt.md` 全文放进任务的 system / developer 消息，
  而不是让模型从对话里「记得」规则；规则版本以 `tools/agent_extract.PROMPT_VERSION` 为准。
- **模型**：`--model` 填 codex 实际使用的模型 id（如 gpt-5-codex）；codex 的工具与模型绑定，
  要单独比「模型」请用脚本执行器（框架 §1），要单独比「工具」请让 qoder 配同一模型跑同一样本。
- **公共门禁**：先逐条填写质量证据并通过 `quality-check`，再 dry-run 判读、正式提交；修改结果会使旧质量与预览记录失效。
  `agent dryrun-check --batch <b> --file <r> --model <真实模型id> --executor codex --run-id <run_id>` 只检查预览，不替代语义质量表；钩子检查不能代替公共 CLI 的强制检查。
- **自报成本**：codex 会话的 token 看其用量面板；每批记一行，run 结束时汇总进 `run end`。

## 冒烟与准入（不入库）

codex 的模型直接走运行器，**不要自己写脚本**：
`"$CLI" bench run --bench "$V5_BENCH" --executor codex --models gpt-6-luna gpt-6-sol --tag <标签> --limit 10`；`V5_BENCH` 先按 [当前参考流程](bench-reference.md) 验证完整 prompt/hash。
运行器用 `codex exec --ignore-user-config --ephemeral -s read-only` 单轮调用：关 shell 工具与钩子、不读项目文档，
系统提示经 `developer_instructions` 传入（与 qodercli 的逐字相同），观测走标准输入；推理强度用 `--effort`（缺省 medium）。
codex 不报 credits，复盘表只显示 token/条。2026-10-23 那次 codex 会话自写脚本，每个模型一次发 10 条、推理强度 medium、
系统提示多一句，和 qoder 的运行器不一致，结果不能直接和 qoder 的模型放一张表——现在统一由运行器定死。

## 多模型战役（campaign.md）

- **方式二未确认**（2026-10-22）：codex 的派生工具自报有 `model` 参数，但 `codex exec --json` 两次实测事件流里都没有派生记录，
  不能确认子代理真按指定模型跑。先用「每个模型一个会话」：
  `"$CLI" campaign launch --id <战役> --tool codex` 打印
  具体命令由已发布的 `campaign launch` 生成，不复制旧的 `codex exec` 参数。会话在采集排除目录打开；代码从稳定入口加载。
- 子代理必须能写战役状态和领取登记目录、访问 NAS/kg-hub；保留真实工具错误，不能把沙箱网络失败当服务端已经处理成功。
- 已有宿主子代理 idle/completed 时用 `followup_task` 触发后续任务；running 才用 `send_message`。repo campaign 状态不等于 App 的宿主任务状态，新开原生调用仍需真实探测回执。
- 模型名用 `codex debug models` 里 `visibility=list` 的那些（如 gpt-6-astra、gpt-6-luna、gpt-5.6-sol）。

## 完成标准

与 Claude Code 相同：本 run error 为 0、`reuse_mismatch_per_obs` ≤ 0.2、运行清单有 `ended_at` 与自报成本、
每 500 条有 lessons 并登记。

公共命令通过 `tools/extract` 执行：显式 `KG_HUB_PYTHON` 优先，否则当前 HOME 的合格 `~/.local/share/kg-hub/venv/bin/python` 优先于系统 `python3`。
