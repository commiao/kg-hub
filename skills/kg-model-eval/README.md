# kg-model-eval 维护说明

给要修改本 skill 的人看；使用方法见 `SKILL.md`。

## 源头在哪

正本在 kg-hub 仓库 `skills/kg-model-eval/`（走 PR、有测试 `tests/test_kg_model_eval_skill.py`：frontmatter 与 manifest 一致、
写到的子命令都存在、写出来的准入门槛与 `tools/extract_bench.THRESHOLDS` 一致）。各工具里的是 skill-sync-sidecar 分发的副本，
不要在副本里改。

触发词统一为「使用kg-hub对比模型」前缀（2026-10-23 用户定）：改描述时 `SKILL.md` 的 frontmatter 与 `manifest.json`
两处一起改（测试逐字对账）；不要再往描述里加不带前缀的说法，太宽泛会在日常讨论 kg-hub 时误触发。

## 改完怎么装

合并后从 sidecar 的已安装 release 安装（会备份再替换，并由 sync-daemon 推到共享库、其他设备随后拉取）：

```bash
cd ~/.local/share/skill-sync-sidecar/current
PYTHONPATH=src python3 -m skill_sync_sidecar local-skill-install --path <kg-hub 检出>/skills/kg-model-eval --dry-run
PYTHONPATH=src python3 -m skill_sync_sidecar local-skill-install --path <kg-hub 检出>/skills/kg-model-eval --yes
```

改了 `tools/extract_bench.py` / `tools/extract_campaign.py` 的子命令或门槛，同步改 `SKILL.md`，测试会把不一致的地方报出来。
