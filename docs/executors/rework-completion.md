# 已完成返工的成本备注拒绝交接

`campaign reconcile-rework-completion` 只登记一种窄范围终态：原槽位返工的逐来源质量材料已经齐全，原生调用正常完成，但尝试给原批成本追加备注被返工保护拒绝，公共监督器因此结束为 `coordinator_required / exit2`。

此入口不执行被拒绝的成本命令，不开放成本权限，不修改原成本、原生退出、失败事件或历史 worker，不认定语义正确，也不恢复新领取。协调器仍须独立判断来源质量、共享元数据限制和过程偏差，之后才可另行恢复原配额。其他业务或审批拒绝、NAS 拒绝、未结束调用、不明写入、不同会话和不同所有者均不适用。

## 准备与审核

只在已排除的专用战役目录，使用设备已发布的公共入口。协调器准备私有证据索引；不要在公开仓库保存真实来源正文或会话标识。

索引的字段如下，路径均指向现场已有证据，哈希须真实读取：

```json
{
  "session": "original-synthetic-session",
  "model": "synthetic-model",
  "audit_run": "/private/tmp/example-campaign/audit/rework/audit-run.json",
  "cost_anchor_event": "original-cost-event",
  "cost_anchor_receipt": "/private/tmp/example-campaign/audit/synthetic-model-001-cost.log",
  "cost_count_helper": {"path": "/private/tmp/example-count.py", "sha256": "actual-sha256"},
  "local_helpers": {"/private/tmp/example-local-io.py": "actual-sha256"},
  "quality_helper": {"path": "/private/tmp/example-quality-cli.py", "sha256": "actual-sha256"},
  "failure_receipts": {"original-diagnostic-event": ["/private/tmp/example-campaign/audit/rework/diagnostic.log"]}
}
```

成本锚必须来自返工启动前、本槽位原会话实际成功执行的成本命令。当前窄兼容形态要求完整的 `set -e` 脚本：第一条命令是原公共 `campaign cost --id … --batch … --note …`，随后仅有固定报告、受审本地计数脚本及读取原成本日志的 `COST` 输出。原命令字面备注、实际输出、原回执和当前成本须完全相等。事后另存的 `cost-before` 文件本身不足以证明原成本未改。

被拒命令的 Python 包装器须与有限的完整 AST 形态相符：只读原成本、追加备注、调用一次 `subprocess.run`、保存私有 stdout/stderr，最后返回该命令真实退出码。额外 HTTP/子进程、金额或 token 参数、shell 扩展、伪造输出均拒绝。原日志中的本地校验、路径或只读状态查询失败必须分类并绑定原诊断及后续完成证据；不得因为含有 `quality` 或 `KeyError` 就忽略其实际动作。

当前入口还核对原 `scope/assignment`、唯一原会话、原生 `exit0` 和公共 `exit2`、每条命令的真实终态、完整来源日志、原 batch/results/quality/规则字节、逐来源最终决定、新质量凭据和所有登记的修复终态。引用的材料须保持原哈希；不能用新增文件伪造历史回执。

## 公开预览与登记

```bash
"$CLI" campaign reconcile-rework-completion --id "$CAMPAIGN" \
  --model "$MODEL" --slot 1 --preview --evidence "$PRIVATE_EVIDENCE" \
  --reason "核对已完成返工的成本备注拒绝；不重试成本写回" --out "$PRIVATE_PLAN"
```

逐项阅读预览中的全部原命令、非零诊断及修复回执，确认没有隐藏写入或其他真实拒绝，再用预览实际返回的 SHA 登记：

```bash
"$CLI" campaign reconcile-rework-completion --id "$CAMPAIGN" \
  --apply "$PRIVATE_PLAN" --confirm-plan-sha "$REVIEWED_PLAN_SHA" \
  --reviewer "$ACTUAL_COORDINATOR" --reason "已核对冻结预览全部证据，仅登记原会话交接"
```

预览和登记只要求目标槽位的 supervisor/native 以及涉及批次的 operation 空闲，不要求其他模型停止其互不重叠的返工。登记只追加不可变证明和活动引用；重复登记相同有效预览返回同一证明。旧拒绝仍保留。原会话接续启动后，新的 native attempt 自动消耗旧证明；合同、来源、成本、质量材料或输入哈希变化也会使其失效。新 supervisor 自身的启动回执不覆盖证明保存的原失败监督器快照。

返工使用独立提示，不继承普通生产的领批、提交和记成本步骤。返工成本只写独立 `rework-cost.json`，未知值保持 `null`。原配额的恢复须在质量验收完成后另行执行，不能把登记证明当作验收或新来源配额。
