# kg-model-eval 维护与发布

正本在 kg-hub 仓库 `skills/kg-model-eval/`，各工具读取的是 skill-sync-sidecar 分发的安装副本。
改正本、走 PR 与发布，不就地修改安装副本。`tests/test_kg_model_eval_skill.py` 核对触发描述、命令和准入门槛。
触发边界保持“使用kg-hub对比模型”前缀；`SKILL.md` frontmatter 与 `manifest.json` 的 description 必须一致。

## 三个发布面与顺序

本次公共链路同时涉及服务端能力、公共 CLI/规则产物、各工具的 skill。不能只更新其中一面就宣称全部生效。

1. **提供方先发布**：按 kg-hub 受控 NAS 发布流程发布已合入主干且通过完整测试的同一 SHA。
   核对运行镜像 ID、健康和 `/api/extraction/capabilities`，包含 `agent-v5` / `extraction-quality-v1` 与正式修复能力。
2. **本机代码产物后发布**：用 fleet-ops 的 `mac-release.py kg-hub --repo <Git仓库> --ref <同一SHA>` 更新稳定 `current/`。
   比对发布内容和 SHA；每个新 CLI 进程使用 `"$KG_HUB_CODE/tools/extract"`，不靠旧工作区或 `PYTHONPATH` 抢导入。
   其他设备使用当地已发布的产物路径与依赖环境。若实际改动了常驻 MCP 模块，还须按 `deploy/mac/README.md` 重载并直接验证服务；
   CLI 文件更新不能证明常驻进程已重新导入，反过来也一样。
3. **安装并发布 skill**：先核对安装副本未被他人修改，再用下面的正式安装/共享发布命令。
   工具缓存的 skill 清单可能要新会话才刷新；验收下一次战役实际加载的版本，不宣称旧会话已热更新。

任何一面失败都停止依赖它的后续步骤。回滚顺序相反：先回退调用方和 skill，再按受控流程回退提供方，不能在新客户端仍运行时先撤服务能力。
旧战役快照和准入记录不升级、不覆盖；新正文哈希必须重新准入，或记录本轮用户明确授权的例外。

## 本机安装与共享发布

使用已安装的 sidecar 产物，不从开发工作树运行安装器。源路径要指向本次已批准发布的 skill 正本。
安装前比较正本库、Cursor 副本和各链接目标与上一已发布版的 `SKILL.md` / `README.md` / `manifest.json` 及 `references/`；
存在本地独有修改时先合并核验，不直接覆盖。安装器保存备份与应用记录。

```bash
cd ~/.local/share/skill-sync-sidecar/current
PYTHONPATH=src python3 -m skill_sync_sidecar local-skill-install --path <已批准产物>/skills/kg-model-eval --dry-run
PYTHONPATH=src python3 -m skill_sync_sidecar local-skill-install --path <已批准产物>/skills/kg-model-eval --yes
```

Codex、Claude Code、Qoder、DSH 等可链接到本机正本库，Cursor 可能是副本；两种都要按实际内容核验。
共享库第一次发布须显式保存；之后守护可推送更新，但必须等实际内容哈希一致，不能把“计划要推”当“已发布”。
只发布这个 skill，不夹带别的冲突：

```bash
SKILL="$HOME/Library/Application Support/skill-sync-sidecar/skills/kg-model-eval"
PYTHONPATH=src python3 -m skill_sync_sidecar local-skill-publish --webdav-config --prefix skill-sync-sidecar-dev/current-mac --path "$SKILL" --dry-run
PYTHONPATH=src python3 -m skill_sync_sidecar local-skill-publish --webdav-config --prefix skill-sync-sidecar-dev/current-mac --path "$SKILL" --yes
PYTHONPATH=src python3 -m skill_sync_sidecar ops-status --json
```

共享前缀必须与该设备守护实际配置一致；上面是本机既有前缀，不是其他设备应照抄的全局值。
共享 `index.json` 的 skill 内容哈希应等于本机，安装后再次 dry-run 应没有待写差异。

## 各工具的实际验收

| 层 | 验什么 | 不能拿什么代替 |
|---|---|---|
| 本机工具 | Codex、Claude Code、Qoder、Cursor、DSH 的实际 skill 路径/链接、完整包内容哈希 | 只看正本文件或 manifest targets |
| 公共运行入口 | 新会话从稳定 bootstrap 运行 `campaign preflight`，核对 loaded root、代码 SHA、prompt 哈希和协议 | 从任意旧检出跑 `python -m`、只设 PYTHONPATH |
| 服务提供方 | 真实 capabilities、容器镜像 ID及健康、源码漂移 | 仅本地单测或 compose 文件 |
| 对端设备 | 最新 peer 状态中实际安装路径、包哈希与测量时间；在执行设备上通过 preflight | 共享库上传成功、旧缓存的 peer 状态 |

NAS DSH 的 observer-only 代理只上报状态，不负责安装；OpenClaw 的 pull-only 同步若被其他技能冲突挡住，也不能证明这个包已落地。
需要时按该设备正式的定向安装路径只更新 `kg-model-eval`，保留备份、校验包哈希、重新发布 peer 状态；
不为安装一个 skill 放宽全局写策略、覆盖不相关冲突或伪造 peer 回执。安装 skill 也不等于该工具已具备执行 Python、指定模型或访问服务的能力，须在当地核验。

完整包包含 `references/quality.md`，它是公共质量表与修复说明的可分发版本。
任何子命令、质量字段或默认规则变更，都同步改此引用及执行器说明，再跑 skill 契约测试与 `quick_validate.py`。
