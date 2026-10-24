# 公共质量与修复协议

所有执行器引用同一份[提交前质量核验与正式修复说明](../../skills/kg-model-eval/references/quality.md)。
该文件随 `kg-model-eval` 一起分发，因此各工具读取已安装 skill 时也能拿到质量表契约。

实现正本是 `utils/extraction_quality.py`、`tools/agent_extract.py`、`tools/extract_campaign.py` 及服务端修复入口；
不要在工具专属钩子或提示中再实现一份可绕过的门禁。代码入口见 [_common.md](_common.md)，战役流程见 [campaign.md](campaign.md)。
