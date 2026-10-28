# 已回收、未正式提交批次的质量隔离

提交来源的质量债必须返工。完整公开回收、没有任何正式写入的批次则可由协调器明确
隔离旧错误产物，再允许原会话处理未来公共claim返回的新批次；两者不能混用。

仅支持原native0/完整turn.completed、明确quality_process_violation报告且零提交，
batch状态released、全部原ID已回收、操作记录只有完整成功的非写入dryrun、无原batch
正式ok/empty，以及已启用source-decisions-v1的场景。任何submit记录、未知在途、
审批/权限/NAS75/76、provider错误或不同所有者均拒绝。

协调器先在私有目录保存真实隔离证据JSON，包括原batch/model/slot/session/source_ids、
formal_ok与official_empty均0、public_release实际exit_code/released/reply，
invalid_quality_evidence的entire_batch_quarantined=true及invalid_sheet_accepted_as_pass=false，
以及原worker/native/events/batch/results/错误审核表和隔离证明等完整文件refs。
refs的file/sha256必须指向原证据；历史campaign/log快照与当前受保护状态分别核实。

```sh
extract campaign reconcile-released-quality --id CAMPAIGN --preview --model MODEL \
  --slot SLOT --batch BATCH --quarantine private-quarantine.json --out private-review.json
```

每项失败初始pending。逐项阅读完整命令/输出/辅助脚本，对已随整批回收、不再重试的
原准备失败填写verdict=discarded_with_released_batch及具体note，保留command_sha256。
明确填写reviewer/reason，并填写quarantine_verdict=discarded_invalid_no_formal_write
和quarantine_note。不将旧审核表判为通过，不把失败的写操作归类为本地准备失败。

```sh
extract campaign reconcile-released-quality --id CAMPAIGN --apply private-review.json
```

应用只保存不可变隔离处置证明，不修改原worker/native/失败/质量/结果/成本，不恢复
dispatch，不加配额，不重试旧操作。恢复管理器和原监督器核对证明后，仅允许原SID
继续未来任务。公共claim仍检查全局已入库状态，不能指定或强迫重抽原ID；如果重新
领取某个未入库来源，也必须完整重读并使用新batch和逐来源决定登记，不能复制旧批准。
证明随原native变化而失效，不允许换会话或重复采纳旧回执。
