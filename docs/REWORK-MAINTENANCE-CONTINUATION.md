# 返工中断后的显式维护接续

`infrastructure_failed` 仍不是普通监督或返工的正常结束。只有原生0、完整结束的原
返工会话，在受控服务发布期间发生已核实的只读传输失败，才可使用本协议。
审批、权限、NAS75/76、失败写入、未知终态或在途操作不适用。

协调器准备私有 evidence JSON：`audit_dir` 为原战役audit子目录，`release` 包含
`batch`、`completed`、`capabilities` 三个真实发布证据文件路径。发布批次包含真实
`authorization`、`operator`、`frozen_commit` 和 `at`；完成记录包含同一 `commit`
和 `at`；能力回执为HTTP200和服务端 `body.revision`。原native结束必须位于该次
明确授权发布开始至完成的时间区间内。文件哈希是受控证据绑定，不能代替真实授权。

```sh
extract campaign rework-maintenance-sheet --id CAMPAIGN --model MODEL --slot SLOT \
  --evidence private-evidence.json --out private-review.json
```

表中全部失败项初始为pending。逐项阅读完整原命令、原输出和冻结辅助脚本，确认
原操作没有发起失败外部写入。填写真实 `reviewer`；每项保留 `event` 和
`command_sha256`，根据实际情况填写 `readonly_transport_failure` 或
`local_failure_no_external_write`，并记录具体 `note`。至少一项必须为原batch内的
公共repair preview传输失败。不能批量填通过或把局部审核当整批语义通过。
审核表保存在冻结audit目录以外，避免自身改变证据集合。

```sh
extract campaign rework --id CAMPAIGN --model MODEL --slot SLOT --batches BATCH \
  --assignment reviewed-assignment.md --reason '具体原owner维护接续理由' \
  --maintenance-review private-review.json
```

启动前重新核对原SID、唯一槽位、已提交batch、配额/运行契约、原native与事件、
全部audit文件和维护证据，再执行新公共preflight，要求提供方revision匹配已完成
维护回执，随后再次检查文件未变。辅助脚本只允许受限本地审核操作；遇到不支持的
程序、路径、动态执行或失败写入时拒绝，交独立工程核实，不能扩成宽松兜底。

新任务保存原worker/native副本与 `maintenance-continuation.json`，保留所有原失败。
仅续原SID，dispatch保持draining，新增来源配额为0；不重放旧命令、旧修复、不改
成本、原抽取结果或随机抽查。原逐源检查点继续使用，原先已验证修复仍需按实际状态
核对。完成后由协调器独立验收完整批次；只有验收通过才能另走公共resume恢复原配额。
