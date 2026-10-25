# 提交前质量核验与正式修复

新运行共用 `agent-v5` / `extraction-quality-v1`。本页说明质量表的含义；命令与字段以发布产物的 `--help` 及
`utils/extraction_quality.py` 为准。表只证明证据覆盖、审核对象及版本一致，不证明评审者的语义判断必然正确。

## 默认 peer 只消费冻结批次

默认 frozen-batch consumer 无需服务 API 凭据，不调用 HTTP 预检、next/claim 或 submit；Mac 协调器统一领取源观测、HTTP 能力预检、整批 dry-run、正式提交及权威 registry/log/score。
Mac 交付冻结 batch、完整 prompt SHA256、manifest model/tool/run 身份与最新成功 preflight/capabilities 回执；peer 核对本地 tracking 排除、代码/规则/协议版本和受控产物 hash，再离线 quality-sheet/check、逐条审核并回传 results 与质量 receipt。
回执是受控产物 hash 证据，不是密码学签名，不代表 peer 自己通过 HTTP 预检。peer dryrun-check 还必须使用 Mac 对同一整批结果真实预览成功生成的 payload-bound marker；缺失或结果改变须返回 missing，不能拿能力回执替代预览。
Mac 在同一 campaign 重新核验回传 receipt 和结果后才执行 HTTP 整批预览及正式提交，服务端 v5 质量门继续强制。
peer 独立 API 预检/提交是尚未开通的可选扩展，需另有明确授权和专用 credential；默认路径不签发新 secret、不借 master，不声称独立源领取或本地 bench/score 已验证。

下面 campaign 的联网预览/提交命令由 Mac 协调器执行；peer 只运行明确离线的 agent 文件命令。

## 每批先核质量，再预览，再提交

使用已发布的 `CLI="$KG_HUB_CODE/tools/extract"`，会话目录保持采集排除。

```bash
"$CLI" campaign quality-sheet --id <战役> --batch <批次> --out <质量表.json>
# 评审者逐条读原文、完整lookahead与实际结果，再填写质量表。
"$CLI" campaign quality-check --id <战役> --batch <批次> --review <质量表.json>
"$CLI" campaign submit --id <战役> --batch <批次> --dry-run
# 每个复用候选按语义判读，必要时修改结果并重审、重预览。
"$CLI" campaign submit --id <战役> --batch <批次>
```

战役从 manifest 自动取得该批真实模型、执行工具和 run_id，不手填或替换身份。单模型入口显式固定三者：

```bash
"$CLI" agent quality-sheet --batch <batch.json> --file <results.json> --out <质量表.json> --model <真实模型id> --executor <工具名> --run-id <run_id>
"$CLI" agent quality-check --batch <batch.json> --file <results.json> --review <质量表.json>
"$CLI" agent submit --batch <batch.json> --file <results.json> --review <质量表.json> --model <真实模型id> --executor <工具名> --run-id <run_id> --dry-run
"$CLI" agent dryrun-check --batch <batch.json> --file <results.json> --model <真实模型id> --executor <工具名> --run-id <run_id>
"$CLI" agent submit --batch <batch.json> --file <results.json> --review <质量表.json> --model <真实模型id> --executor <工具名> --run-id <run_id>
```

`--executor` / `--run-id` 可分别由 `KG_HUB_EXECUTOR` / `KG_HUB_RUN_ID` 提供；模型必须显式传 `--model`。
quality-check 读取表内身份，无需重复输入。dryrun-check 只核对预览，不能代替质量核验。
使用额外冻结规则时，上述各步同时传同一 `--prompt-file`。不要手写假的 digest，也不要将待评模板自动填为通过。

## 质量表如何填写

`quality-sheet` 生成的是未通过的模板：顶层 `schema`、`reviewer`、`identity` 与逐条 `items`。
`identity={"extractor_model":"<真实模型id>","extractor_tool":"<工具名>","run_id":"<run_id>"}` 三项必须为非空字符串；
抽取者身份与 reviewer 分开。每条有 `context_digest`、`verdict` 和核对标志。digest 绑定本批原文、lookahead、规则、结果及 identity；
提交身份必须与表内一致。任何相关字节或身份改变都重新生成、重新审阅。

预览摘要绑定整批输入、整批结果和身份，因为实体复用受前项及 pending 实体上下文影响。前项改变也会使后项旧预览失效，
应重审并整批重新 dry-run，不能只预览修改的那一条。

- `reviewer` 写实际审阅者；逐条 `verdict` 只有核对通过才填 `ok`。发现错误先修结果，不能填 `ok` 同时把问题藏在备注里。
- `translation_complete`：第二条边是否忠实翻译标题，保留数字、专有名词及限定；语言不同不能证明翻译完整；中英混合专名可以保留，不能凭含中文字符自动否定英文译文。与原文完全相同则不能冒充译文。
- `facts_complete`：其余 fact 是否为完整句，方向、数字、条件和历史时点是否保真；禁止截断到固定字符数。
- `core_covered`：核心机制和新增信息是否已覆盖；不能靠删除核心事实让格式指标通过。真正无知识的空项也要明确核对没有漏抽。
- `ownership_checked`：全部归属边和 summary 是否有正文支持；记录标签、来源描述及 `Project:` 元数据本身不是实现归属证据。
- `edges` 为每个实际边的 `index` 提供 `source`（`body` 或 `lookahead`）与原样 `quote`。
  引文必须能在对应输入中找到；标题译文可引用原始标题，后续更正可引用实际 lookahead。
  `belongs_to` 只接受正文归属证据，不能只用 lookahead 或外层记录项目标签。
  第一条 fact 可为原始标题，或 `原始标题（后被推翻：…）`；后一种必须由该边的实际 lookahead 引文支持。
- `summaries` 按实体 `name` 提供原样 `quote`；不能把“实体名出现过”当成 summary 全部断言有据。

自环、重复边、悬空边等导致真实入库归一化丢失结构时，质量核验会拒绝；先改候选，不能只靠服务悄悄丢边。

引文与布尔标志不是自动事实裁判。即使工具能找到引文，评审者仍须判断它是否支持该边/summary 的完整断言。
批量填写相同引文、复制无关段落、机械全选 true 都不能替代逐条审阅。

## 空项不能只凭标题分类

空结果使用 `empty`：`reason` 为 `no_stable_knowledge`、`duplicate` 或 `unreadable`，填写原文 `quote`，
核对 `no_new_information`；非空结果的 `empty` 必须为空。重复还需 `duplicate_of` 指本批前面的非空观测，
`covered_quote` 引用该观测实际结果中已覆盖的事实，而不是只写“以前讲过”。

历史、回放、测试或复盘中有新增数字、实现、规则、事故结论时应抽取，并写清历史时点。
“正文不可见”与“正文没知识”不是同一判据；不得猜测加密内容，也不能用加密元数据推断业务事实。
无法证实重复覆盖时，不把可抽取事实直接丢掉。

## 提交后的检查与报告

每批固定随机抽查 3 条，检查完整原文与 lookahead，说明评审者和是否盲评。
系统性归属错误另做全量归属核验，高空率另做判空核验；`campaign review --kind random|targeted` 明确分类，随机与专项分母分别报告。
旧记录没有 kind 时计入 `legacy_reviewed` / `legacy_review_errors`，单列“旧记录未分类/旧记录错误”，不能默认算入随机；
`reviewed` / `review_errors` 仍是包含三类的总计，随机结论只用 `random_*`。
已提交条目发现事实错误或漏抽，提交成功状态不能掩盖质量失败；如实保留已修/未修及正规修复证据。

最终数量按 `(batch,id)` 最新日志去重、排除 released；ok 与已正式记录的 empty 分列，
claimed、skipped、预览和失败不能冒充成功。模型抽不同新样本只构成产出画像，不构成同题排名。

## 已提交错误走同一正式修复入口

只修复已授权、已逐条确认的具体边或误判空项，不把准确原文整条归档。
公共入口 `repair` 路由 `tools/extraction_repair.py`，所有工具使用相同协议：

```bash
# selector.json只定位原始来源：source_description与source_obs_id，不换键、不改原文。
"$CLI" repair preview --file <selector.json> --out <snapshot.json> --request-out <pending-request.json>
# 读取完整快照，逐条核对边uuid、原文和其他来源；填写真实reason/reviewer及稳定repair_id。
# reviewed-request.json保留expected_snapshot_sha256，选择remove_edge_uuids及replacement。
"$CLI" repair preview --file <reviewed-request.json> --out <plan.json>
# 对照只读计划确认无误后再应用同一份请求。
"$CLI" repair apply --file <reviewed-request.json> --out <apply.json>
"$CLI" repair status --repair-id <repair_id> --out <audit.json>
```

初次 selector 为 `{"source_description":"<原来源>","source_obs_id":"<原来源键>"}`。
模板尚未批准：填写真实 `repair_id`、`reason`、`reviewer`，保留服务快照给出的 `expected_snapshot_sha256`。
`remove_edge_uuids` 只列已核实的错误边；服务按原观测解除关联，不能把其他来源支持的事实一起删掉。
`replacement` 为完整的非空 `ingest_extracted` 候选请求，或只移除错边时留空。
误判空项升级必须有非空 replacement，保留原来源身份/正文/时间；候选使用 agent-v5 并携带逐条审阅通过的
`quality_context` / `quality_review`，不能把旧结果改个版本号就算新凭据。

新判空来源保存 empty_source_sha256；误判空升级时原文摘要必须一致。历史空项没有摘要时，真实 reviewer 必须在新 v5 质量表中核证恢复来源，
审计会标注 historical_source_attested=true，不能声称服务验证了不存在的历史摘要。

preview 是只读计划，不代表已修复；apply 要写权限。快照变化必须重新读回、重新核证，不自动替换 expected hash 后重试。
网络超时或 pending 先用同一 repair_id 查 status；确认审计状态后需要重放时，仅用原请求和同一 repair_id，不能换ID盲目重放。
报告分别标注候选已备、预览通过、应用成功及审计读回；只有服务核实来源键、纠正事实和episode关联后返回 verified=true，才记修复成功。
确认成功后也保留旧战役原结果/错误记录，修复作为另一条可追溯审计，不回写历史评测分数来抹掉错误。


### 修复计划中的全局事实边复用

`plan.materialization_version=edge-origin-v1` 区分本来源时间 `source_reference_time` 和全局事实边首来源时间。每条 replacement edge 的 `write_mode` 为 `new` 或 `reused`。`reused` 的平铺时间、模型、repair_id 等字段显示实际保留的首来源值；`stored_origin` 保存其原始属性及端点。复用只追加本 episode 的背书，不能用后来源的时间或修复编号覆盖首来源。UUID 丢失、不可变属性变更或原有来源背书丢失会拒绝成功回执。

新协议成功回执的 `replacement_edge_verification` 返回 `version=edge-origin-v1`、本来源时间，以及各事实边的写入模式、首来源元数据、属性摘要和原背书集合摘要（列表最多 120 项，完整集合另有摘要）。新边验证拟写属性和 float32 物化向量，复用边验证原始全部不可变属性（包括向量）及原背书子集。`shared_edge_verification` 继续核验原错误边的其他来源。旧 applied 审计原样保留；旧 pending 计划恢复后明确标 `legacy_plan`，不能当作新协议证明。preview 只呈现当时图状态；正式 apply 在 writer lock 内重新解析并持久化计划，恢复严格复用该冻结计划。

## 跨来源的重复节点走节点级治理

`repair` 只能按单个来源修：删掉这条来源带进来的错边，或换一份抽取结果。同一个文件或概念在图里有两三个名字、
裸名泛名节点混了多个仓库、节点类型标错，这些跨来源、跨战役的问题改走公共入口 `nodes`（`tools/graph_governance.py`，
服务端 `/api/graph/governance`），协议与 repair 相同：只读 preview 出完整计划 → 人工核对 → apply 绑定快照摘要 → status 读回。

```bash
# scope.json 用名字或 uuid 指定节点，名字按入图规则（忽略大小写的精确匹配）解析，撞名会列出候选并拒绝。
"$CLI" nodes preview --file <scope.json> --out <plan.json> --request-out <pending-request.json> --report <plan.md>
# 逐条核对 plan.md：每条边迁到哪、哪些边去重并入、哪些自环被删、受影响的来源；填 governance_id / reason / reviewer。
"$CLI" nodes preview --file <reviewed-request.json> --out <plan-reviewed.json>
"$CLI" nodes apply --file <reviewed-request.json> --out <apply.json>
"$CLI" nodes status --governance-id <governance_id> --out <audit.json>
```

四种操作（scope 的 `operation`）：

| 操作 | 必填 | 效果 |
|---|---|---|
| `merge` | `canonical`、`merged`（1–20 个） | 被合并节点的全部边（不论来源、战役）迁到规范节点；可同时 `rename_to` / `retype_to` 规范节点 |
| `rename` | `node`、`rename_to` | 改名并重算名字向量，旧名记进 `previous_names`；新名已被别的节点占用时拒绝（应改用 merge） |
| `retype` | `node`、`retype_to` | 换类型标签（只接受 schema 里的类型），旧类型记进 `previous_types` |
| `split` | `node`、`partitions` | 把混了多个仓库的泛名节点按来源拆开：每个分区并入已有节点（`target`）或新建节点（`new_node`），用 `episode_uuids` 按来源圈边、或用 `edge_uuids` 逐条指定；两边都背书的边必须用 `edge_uuids` 指定或放进 `keep_edge_uuids` 留在原节点；原节点可 `rename_to`，或在所有边都分走后 `retire_original` |

不带 `partitions` 的 split preview 只列出原节点每条边及其来源，供设计分区，不能 apply。

规则：

- 迁移的边保留原 uuid 与全部属性（含向量、首来源时间/模型/run_id），只在单条查询内删旧建新，因为 FalkorDB 的关系不能换端点。
- 迁移后端点、边名、fact、`invalid_at`、`expired_at` 都相同的边算重复：最早创建的那条存活、保留首来源元数据并获得所有来源背书的并集，
  其余并入（absorb）后离开图；episode 的 `entity_edges` 同步改指存活边。有效期不同的同句事实不合并。
- 迁移后两端变成同一节点的边（如「X 就是 Y」）是自环，删除；与被并入的边一样，完整属性（含向量）存进审计节点 `GraphGovernance.deleted_json`。
- 被合并节点不删除：去掉 `Entity` 与类型标签、改为 `MergedEntity`，保留原属性并写 `merged_into` / `merged_by`；入图名字解析与检索都不再命中它。
  规范节点记 `merged_from`、`merged_names`、`governance_ids`。MENTIONS 改指规范节点，已有提及的不重复。
- 节点上若有 RELATES_TO / MENTIONS 以外的关系，或已有未完成的治理，preview 直接拒绝。单次最多 1500 条边、3000 个来源 episode。
- preview 只读（只发 `_READ_TAGS` 里的查询），read scope token 可调；apply 只认管理员 token，持与入图共用的写锁，
  在锁内重读快照，摘要不一致返回 `stale_snapshot`，不自动替换摘要重试。
- apply 先把请求、快照、计划、名字向量与将删除的边全文持久化为 pending 审计，再逐步写图；每步按 uuid、端点和 fact 加守卫，
  中断后用同一份请求与同一 `governance_id` 重放会沿用冻结计划续做；换内容复用同一 ID 返回 `governance_id_conflict`。
- 成功回执的 `plan_sha256` 应等于核对时 preview 的值；`verification` 逐项读回边端点、不可变属性、提及、episode 引用和节点标签后才记 `verified=true`。
