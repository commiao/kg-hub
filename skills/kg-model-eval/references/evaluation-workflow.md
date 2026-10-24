# 新版评测：探测、定向旧错、出站范围与汇总

通过本机已发布 `tools/extract` 执行。以下操作只生成或检查本地回执；真正调用模型的只有 `runtime probe` 和 `bench run`。真实套件、原文、匿名表和模型映射留在私有目录；公开仓库只放合成测试。

## 真实可调用性与准入

1. 在原预算与用户选择的模型范围内，以 `runtime probe --executor codex|claude|qodercli --model <准确id> --out <全新路径>` 做无私有正文的 nonce 调用。`runtime inspect` 只检查路由。CLI 采用已有进程代理，Mac 可发现已有系统代理，不改系统设置或写全局变量。认证、身份不符等终止性错误立即停止；短暂 429、5xx、传输故障按有限次数重试。不把服务故障变成模型质量失败。
2. 选择与完整 prompt/hash 相匹配的真实 native reference 后正常执行 `bench run`。它自动先探测所有模型，通过后再送冻结观测；成功目录产生 `run.json`、`execution-state.json` 和 `evaluation-index.json`。基础设施失败只有 abort 回执，没有可用于准入的完整 run。
3. 首次为参考登记私有固定旧错套件：

```bash
"$CLI" regression create --bench "$V5_BENCH" --spec <私有固定旧错spec.json> --out <私有suite.json>
"$CLI" regression activate --suite <私有suite.json> --reviewer <操作者名称> --confirm-suite-sha <实际suite_sha256>
"$CLI" regression sheet --bench "$V5_BENCH" --run <原封存run> --out <待评表.json>
# 独立评审逐条读原文/lookahead/结果，填写证据、实体/关系、否定条件及判定；不批量补true。
"$CLI" regression check --bench "$V5_BENCH" --run <原封存run> --review <已填表.json> --out <semantic-receipt.json>
"$CLI" bench admit --bench "$V5_BENCH" --run <原封存run> --sheet <已填随机盲表.json> --semantic-review <semantic-receipt.json>
```

suite 的实际 schema 和逐项语义要求见源码 `utils/semantic_regression.py` 与 [语义协议](../../../docs/semantic-regression-admission-v2.md)。suite 激活需真实核对内容和 SHA，不能拿公开合成例子作为真实旧错已修证明。随机盲评与固定套件分列分母；缺审返回 pending，定向真实错返回 fail，二者不混。现有 native run 可补审，历史成绩不改写。新 `campaign start --model-preflight <每模型新回执...>` 要求新版准入和 15 分钟内相同模型、executor、effort 的探测；旧战役冻结规则继续保留。

## 出站范围清单

`bench blind --review-destination <已核实入口>` 自动生成匿名表、私有映射及 `.scope.json`。映射不交给评审。省略目的地会留下待补状态；不能擅自声称某个 CLI 路由到官方服务。匿名仅隐藏模型标签，完整正文未脱敏。

需关联真实人类消息或允许本轮后续聚合归档时，写 scope spec，例如以下**占位结构**，用实际本地材料和目的地替换后运行：

```json
{
  "artifacts": ["/private/local/blind-input.json"],
  "destination": {"provider": "实际服务", "endpoint": "已核实入口"},
  "purpose": "本轮事实盲评",
  "records": 20,
  "data_classes": ["observation_body", "lookahead", "entities", "edges"],
  "anonymization": "model_identity_only; source body is not redacted",
  "human_message_refs": ["实际授权消息标识"],
  "followup_scope": ["仅同一已批准私有仓库的本轮后续聚合报告；不含全文、盲表或映射"]
}
```

```bash
"$CLI" eval scope --spec <scope-spec.json> --out <scope.json>
"$CLI" eval scope-check --scope <scope.json>
```

输出始终 `approval_status=pending`、`permission_granted=false`。填了人类消息引用也不是机器签发批准。协调器保留真实授权上下文，核对人类批准的模型/目的地/材料数量/用途/聚合范围后才采取外发操作；同一明确范围可复用授权。材料、目的地或用途变化会使旧范围指纹失效。平台批准检查仍有最终约束，拒绝后不换工具或子代理绕过。judge 只获得匿名材料及评审规则，绝不为了“让它有权限”交出完整会话。

## 单一索引报告

每个阶段只指向真实封存文件；尚未生成的阶段可以显式列计划。示例为路径结构，不含真实私有内容：

```json
{
  "entries": [
    {"phase": "smoke", "run": "/private/local/smoke-A", "score": "/private/local/smoke-score.json"},
    {"phase": "admission", "run": "/private/local/admission-A", "score": "/private/local/admission-score.json", "semantic": "/private/local/semantic-A.json", "suite": "/private/local/frozen-suite.json", "admission": "/private/local/admission-A.json"},
    {"phase": "admission", "model": "尚未运行模型", "tag": "下一轮"}
  ],
  "overhead": ["/private/local/orchestrator-cost.json"]
}
```

score 来自 `bench run/score --json <路径>` 的实际输出，新版绑定 run SHA、样本 IDs 与 prompt SHA。admission 可以是输出的独立判定或只有唯一匹配运行的历史容器；一个运行多条记录时，选定目标记录另存私有文件，避免默默挑错。overhead 为实测列表，例如 `[{"tokens":120,"credits":null,"seconds":30}]`，或者含 `overhead` 列表的对象。没测的字段填 null，不把订阅模型 credits 默认当零。

```bash
"$CLI" eval index --spec <index-spec.json> --out <evaluation-index.json>
"$CLI" eval report --index <evaluation-index.json> --json <report.json> --markdown <report.md>
```

索引保留 phase/model/tag/run，不用模型名当唯一字典键。run、输入快照、rules/system、score 和 receipt 的哈希随索引保存；读取报告重建摘要并重验源证据。旧无 run SHA 的 score 标 `legacy_identity_only`，不能被写成新回执完整；可只重做本地评分，不再调用抽取模型。formal/admission 阶段需 score、独立语义回执、当时冻结的 `suite` 路径及绑定它的 admission-v2；缺任何一项为 pending，声明了语义回执却缺冻结 suite 则拒绝验证。报告完整表示证据齐全，`verdict=fail` 仍然可能是完整评测，不能把它翻成通过。

报告在原 `reviewed_at` / `decided_at` 时刻复验完整案例、逐项判定、原文和输出锚点、suite/context/run 及身份绑定，不读取今天激活的政策来重判历史。原准入时已经过期的回执拒绝；当时完整有效、今天已过期的回执保留历史完整状态，在报告的当前有效期列另标 `expired`，不能据此开启新战役。只留下自哈希或 `review={synthetic:true}` 的不完整材料无法通过。

成本按 extraction、preflight 组件计量，同一运行跨阶段重复引用只计一次；不同真实运行即使模型相同也分别计。provider 总 token 已含缓存/思考子项，不再叠加。每项输出已知值与覆盖率，未知为 null；没有编排测量时单列未知。排队未启动、已调用后服务失败（可能 provider 报 0 token）和生成失败分别保留。汇总不新造“当前完成时间”掩盖旧资料；收据日期与原终态保持可见。

本地索引含源文件路径和哈希，不含观测全文。归档前仍按实际目的地范围核对，只向公开仓库提交已审公开聚合材料；完整盲表、映射、suite、正文与私人路径清单不随代码发布。

## 宿主编排

原生运行从 `execution-state.json`/abort/run 回执确认状态，不因编排文字声称开始就算 running。宿主 Codex 子代理 idle/completed 时以 `followup_task` 触发后续工作，running 时 `send_message`；repo CLI 不替代 App 的宿主控制。DSH/Cursor 等通道无工具侧真实 canary 回执时报告未验证，公共包同步、binary 或 capabilities 读通都不能代替。

正式原生启动也必须继承探测时冻结的 effort 和代理模式；`campaign launch` 读取 `start_contract` 并生成公共子进程环境包装命令。直接运行原生 CLI 时可用 `"$CLI" runtime exec --proxy-mode auto -- <native CLI argv...>`；它只设置该子进程环境，不改全局代理、不经 shell 重解释参数。手工宿主 agent API 没有相应强制字段时，只能报告未验证，不能因为探测过就宣称正式任务使用了同一环境或强度。
