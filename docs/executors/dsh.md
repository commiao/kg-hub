# 执行器任务说明：DSH（DeepSeek Harness）

> 粘给 DSH（`@deepseek-ai/dsh`，网页界面或 `dsh --profile headless` 命令行）会话的任务说明。四条红线与五步命令见
> [_common.md](_common.md)，本文只写 DSH 的接入点。

> **多模型正式对比（入库、每批复盘）**：用 [campaign.md](campaign.md) 的编排者 + 子代理流程（`tools/extract_campaign.py`），领批加锁、各模型抽不同的新观测。

## 前提（与其他工具相同，装在哪台机器上都一样）

- DSH 的工作区里有一份 kg-hub 检出，在它的根目录执行命令；
- 有一个装好 kg-hub 依赖的 Python，用环境变量 `KG_HUB_PYTHON` 指向它（不设就用 `python3`）；
- 能访问 kg-hub 服务（`KG_HUB_URL`）并有 `KG_HUB_API_TOKEN`；领批要能读到观测库（`KG_HUB_NAS_SSH` / `KG_HUB_NAS_CLAUDE_MEM_DB`，
  见 `tools/agent_extract.py` 文件头的「环境」）。

## 接入点

- **打开项目就能跑**：在 DSH 里打开 kg-hub，单模型执行按 [_common.md](_common.md) 的五步命令；多模型对比按
  [campaign.md](campaign.md)，编排者说明直接粘给 DSH 的主会话。
- **模型**：DSH 默认用 DeepSeek 官方的模型（如 deepseek-v4-flash），在 DSH 的模型设置里切换（profile 的 `agent-default-model`）。
  命令行没有模型参数，所以 `--model` / 战役里的模型 id 填你在 DSH 里实际选的那个。
- **方式二（一个编排会话派多个模型的子代理）待验证**：DSH 有子代理插件（`subagent` / `subagent_fork`），能否给子代理单独指定模型，
  要在 DSH 里实测：让主会话派一个指定了模型的子代理只回「ok」，看 DSH 的用量记录按哪个模型计费。验证前用退路。
- **每个模型一个会话**：`$PY -m tools.extract_campaign launch --id <战役> --tool dsh` 打印
  `dsh --profile headless "<子代理说明>"`；每个会话开跑前先在 DSH 里把模型切到对应的那个。
- **成本**：DSH 的用量看其用量记录，每批用 `extract_campaign cost` 记进战役。
