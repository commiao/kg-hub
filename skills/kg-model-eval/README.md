# kg-model-eval 维护说明

给要修改本 skill 的人看；使用方法见 `SKILL.md`。

## 源头在哪

正本在 kg-hub 仓库 `skills/kg-model-eval/`（走 PR、有测试 `tests/test_kg_model_eval_skill.py`：frontmatter 与 manifest 一致、
写到的子命令都存在、写出来的准入门槛与 `tools/extract_bench.THRESHOLDS` 一致）。各工具里的是 skill-sync-sidecar 分发的副本，
不要在副本里改。

触发词统一为「使用kg-hub对比模型」前缀（2026-10-23 用户定）：改描述时 `SKILL.md` 的 frontmatter 与 `manifest.json`
两处一起改（测试逐字对账）；不要再往描述里加不带前缀的说法，太宽泛会在日常讨论 kg-hub 时误触发。

## 改完怎么装

两步：先装进本机各工具，再保存到共享库。**第一次发布时第二步不能省**：sync-daemon 不会自动推送「本地新增」的 skill
（同步计划里标 `local_new`、`blocked`，原因是 `local new skill requires --allow-new before auto-push`），
不手动保存，其他设备永远拿不到——2026-10-23 核实时 kg-model-eval 就从没上过共享库，此前几版都只在本机。
**只有第一次要手动保存**：发布过之后再改，重装后守护下一轮会自动推送（已验证，2026-10-23：重装新 README 后 4 秒，
守护那一轮 `push 1`，共享快照换成新的，内容哈希与本机一致；随后 `local-skill-publish --dry-run` 显示 0 个文件要上传）。
所以之后改完只做第一步，等一轮（最多 5 分钟），再按下面核对；核对不一致时再手动跑第二步。

```bash
cd ~/.local/share/skill-sync-sidecar/current
# 1. 装进本机各工具（先备份再替换；正本库与 cursor 是副本，codex / Claude Code / qoder / DSH 是链接到正本的，跟着更新）
PYTHONPATH=src python3 -m skill_sync_sidecar local-skill-install --path <kg-hub 检出>/skills/kg-model-eval --dry-run
PYTHONPATH=src python3 -m skill_sync_sidecar local-skill-install --path <kg-hub 检出>/skills/kg-model-eval --yes
# 2. 保存到共享库（只发这一个 skill，不带别的冲突）；前缀必须和 sync-daemon 用的一致，不带前缀工具会拒绝 HTTP 上传
SKILL="$HOME/Library/Application Support/skill-sync-sidecar/skills/kg-model-eval"
PYTHONPATH=src python3 -m skill_sync_sidecar local-skill-publish --webdav-config --prefix skill-sync-sidecar-dev/current-mac --path "$SKILL" --dry-run
PYTHONPATH=src python3 -m skill_sync_sidecar local-skill-publish --webdav-config --prefix skill-sync-sidecar-dev/current-mac --path "$SKILL" --yes
```

覆盖前先确认现装副本没被人改过：正本库与 cursor 两处的 `SKILL.md` / `README.md` / `manifest.json` 应与仓库上一版逐个一致
（zsh 里取旧版要写 `git show "${c}:skills/kg-model-eval/SKILL.md"`——`$c:s…` 会被当成变量修饰符，取到的是提交信息）。

保存后核对：

```bash
PYTHONPATH=src python3 -m skill_sync_sidecar ops-status --json   # sync_plan.blocked_items 里不再有 kg-model-eval
# 共享快照 ~/public-sync/skill-sync-sidecar-dev/current-mac/index.json 的 snapshot_id 变成这次的，kg-model-eval 的 content_hash 等于本机
# 其他设备看它们上报的 ~/public-sync/skill-sync-sidecar-peer-status/<设备>.json：tools[].skill_items 里有没有 kg-model-eval
```

### 其他设备的现状（2026-10-23）

共享快照 `local-skill-publish-20261022T190704.751278Z` 已含本 skill，但两台对端设备都还没装上，原因不同：

| 设备 | 同步方式 | 现状 | 要它装上需要 |
|---|---|---|---|
| oc-vps（OpenClaw） | pull-only，每 5 分钟一轮 | 已认到新快照（计划新拉取 9 个，含本 skill），但整轮被别的 skill 的 4 处冲突挡住（blocked 18，applied 0），一个新 skill 都装不上 | 先处理它那边的冲突；另外 OpenClaw 不跑 kg-hub 的对比工具，本 skill 对它用处不大（manifest 的 targets 列了 openclaw） |
| NAS（DSH） | peer-observer、status-only | 只上报状态，从不从共享库拉取（现有的 deploy-standard 是别的方式装的） | 手工放进它的 skill 目录，或把它的同步方式改成会拉取 |

改了 `tools/extract_bench.py` / `tools/extract_campaign.py` 的子命令或门槛，同步改 `SKILL.md`，测试会把不一致的地方报出来。
