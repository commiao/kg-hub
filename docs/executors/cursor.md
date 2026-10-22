# 执行器任务说明：cursor

> 粘给 cursor（桌面版 agent 或 `cursor-agent` 命令行）会话的任务说明。四条红线与五步命令见 [_common.md](_common.md)，本文只写 cursor 的接入点。

> **多模型生产画像（入库、每批复盘；模型准入看 bench）**：用 [campaign.md](campaign.md) 的编排者 + 子代理流程（`tools/extract_campaign.py`），领批加锁、各模型抽不同的新观测。

## 接入点

- **方式二已验证**（2026-10-22）：cursor 的子代理定义（`<仓库>/.cursor/agents/*.md`，frontmatter 为 name / description / model）
  里的 `model` 生效，派子代理的工具本身也带 `model` 参数。生成定义：
  `python -m tools.extract_campaign agents --id <战役> --format md --dir <仓库>/.cursor/agents`，
  然后在 cursor 里把编排者说明交给主会话，它按名字（`kg-extract-<模型>`）调用各个子代理。
- **每个模型一个会话**：`python -m tools.extract_campaign launch --id <战役> --tool cursor` 打印
  `cursor-agent -p --model <模型> --trust --workspace <仓库> --add-dir <状态目录> --force "<子代理说明>"`。
  非交互模式下不加 `--force` 命令会卡在审批上；`--force` 会放行它提出的所有命令，只在可信的战役说明下用。
- **模型名**：用 `cursor-agent --list-models` 的写法（如 `gemini-3.7-flash-high`、`gpt-5.6-luna-high`）。
- **成本**：cursor 的用量看其账户面板，每批用 `extract_campaign cost` 记进战役。
- **用户级钩子**：cursor 会加载用户级钩子（实测会话开头被注入 kg-hub 上下文），会话本身也会被 claude-mem 记录。
