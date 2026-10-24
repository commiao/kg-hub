# 当前规则下建立新的 bench 参考

旧 `bench-20261022` 的 c8 基线只等价于 agent-v4。它仍可用于历史评分，不能重标为 v5，也不能用 v5 候选直接签发准入。需要新规则准入时，先在旧冻结样本上产生真实的当前规则 Claude 运行，再建立一个新目录：

```bash
"$CLI" bench run --bench <旧冻结样本目录> --executor claude \
  --models claude-opus-5-5 --tag reference-v5 --limit 50 --chunk 10 --parallel 4 --effort medium
"$CLI" bench baseline --bench <旧冻结样本目录> --run <上一步真实运行目录> \
  --bench-id <新的唯一参考名称> --root <本机bench根目录>
```

模型预算应先获用户确认；建立参考只复制本机证据，不再调用模型，也不写知识图。新名称不可覆盖已有目录。`baseline` 只接受至少 30 条完整、由公共运行器实际调用 Claude 且响应身份与所选模型一致的运行；手工 `register`、缺少模型身份、旧规则重标、漏交或证据不一致均拒绝。

新参考保留原始抽样顺序，50 条的前 10 条就是同一冒烟子集；保留原批次、每批 lookahead、原观测和问句、背景向量、固定语料与向量。参考输出的模型标为真实 Claude 模型，同时保留原三种来源的样本组：每组仍独立检查中英命中不能落后参考 5 个点，不能合并成整体放宽门槛。报告显示“真实模型参考｜原样本组 X”，不把 Opus 输出称为 Haiku 或 Sonnet 输出。

然后所有候选使用同一个新目录、同一推理强度和每次调用条数：

```bash
"$CLI" bench run --bench <新参考目录> --executor codex --models <候选模型...> \
  --tag smoke-v5 --limit 10 --chunk 10 --parallel 4 --effort medium
"$CLI" bench admit --bench <新参考目录> --run <冒烟运行目录>
"$CLI" bench run --bench <新参考目录> --executor codex --models <候选模型...> \
  --tag admission-v5 --limit 50 --chunk 10 --parallel 4 --effort medium
"$CLI" bench blind --bench <新参考目录> --run <正式运行目录...> --n 10 --out <评审表>.json
"$CLI" bench admit --bench <新参考目录> --run <正式运行目录> --sheet <评审表>.json
```

评审者只读匿名表，不读 `.key.json`。只填写 `verdict` / `note`；通过外部评审器时让它仅返回 `code` / `verdict` / `note`，再将判定合回原表。不得修改标题、正文、lookahead、实体或关系。正式准入校验代码和观测唯一、判定完整、内容等于冻结输入与原结果；错误判定必须有说明。多模型表和行重排均可使用。配置或评审表不合法会在评分与写记录之前拒绝，不记为候选模型失败；无评审表仅预览，不覆盖既有准入。

运行器在调用前保存规则、完整 system 提示与输入快照，每次尝试（包括失败重试）绑定实际 query 摘要、响应、解析结果和模型身份，结束后封存文件与运行元数据摘要。新参考内携带这些原始证据，即使原运行目录归档仍能核验；固定语料目录仍须保留原文件。准入要求候选、参考和当前公共入口的规则正文、完整提示版本及 system 提示摘要一致；只改 bench 补充提示也要重建参考。参考模型不能给自身签发准入。

这些摘要是受控本机文件的完整性回执，不是服务商签名或远端真实性证明。Claude / Codex 没有实际 token 用量时显示未知，不推算为零；订阅通道不把未知 credits 写成实测零。历史原始结果仍可 `bench score --run` 回放比较，旧准入记录和旧参考不回改。
