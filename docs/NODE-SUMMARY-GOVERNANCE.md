# 共享节点摘要修正

`extract nodes` 的 `summary` 操作修正已存 Entity 的摘要。先发布支持该操作的
NAS provider，再发布相同协议的 Mac 客户端；旧 provider 不支持这个操作。
模型 worker 环境禁止发起摘要治理，交由协调器复核共享节点的全部来源。

1. 建立 scope：`operation: "summary"`、`node: {"uuid": "原节点UUID"}`、
   `summary_to: "拟修正摘要"`，先运行 `extract nodes preview --file scope.json
   --out listing.json --report listing.md`。没有来源复核记录时，预览不可 apply。
2. 阅读 `plan.summary_sources` 中每份完整来源，同时检查旧摘要、所有关系和提及。
   每份来源记录 `uuid`、该项 `sha256` 和具体 `note`，写入 scope 的
   `reviewed_episodes`。只审当前批次的来源不够；共享节点的其他来源也必须覆盖。
   哈希和说明用于固定复核证据，不能自动证明摘要的语义正确。
3. 再次 preview，使用 `--request-out reviewed.json --out preview.json
   --report review.md` 固定全部来源、旧摘要、新摘要和快照。补齐唯一
   `governance_id`、真实 `reviewer` 和 `reason`；检查 `plan.applicable`。
4. 复核后运行 `extract nodes apply --file reviewed.json --out receipt.json`，
   再运行 `extract nodes status --governance-id 原ID --out status.json`。
   客户端不会刷新或替换请求中的 `expected_snapshot_sha256`；来源、节点或边变化时
   必须重新预览和复核。保留完整请求、预览、回执及独立前后快照作为私有证据。

更新仅写原节点 `summary` 并追加 `governance_ids`。UUID、名称、标签、创建时间、
名称向量及其他属性不变，关系的事实、向量和来源，MENTIONS、Episode 原文与引用均
不变。不创建副本、不删边、不重抽、不调用模型或 embedding。
共享 writer lock 覆盖检查和写入；写后完整读回核验。不符合原状态时保留 pending
审计并拒绝报成功。中断重放只接受相同治理请求的精确旧状态或精确已写状态。
每次最多完整复核 50 份来源，缺失原文或超过上限时拒绝部分覆盖。

回滚代码不撤销已完成的数据治理。若摘要需要再次纠正，使用新的治理 ID 对当时
最新状态重新复核，不能直接写库或借 rename/split 绕过摘要协议。
