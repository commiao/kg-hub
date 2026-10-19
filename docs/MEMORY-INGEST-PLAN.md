# 人写记忆入 kg-hub：度量结论与推送/回流方案（T-0332）

> 状态：**方案稿，未实施**。2026-10-19 写于 T-0332，只读核查与度量的产物。
> 断路器开着期间只做只读与方案；批准后再动代码。本文不改北极星 §9 契约。

## 0. 一句话

四个工具的人写记忆（Claude Code 95 条、Qoder 202 条、Codex 18 文件、DSH 8 条）是 S1 场景里密度最高的知识源，
但线上图对它们的召回接近零。现有 schema **不需要新字段**就能标来源；入库只能走 `/api/ingest_extracted`
（唯一不调模型的写入口），回流复用 `kg_push_hook` 这条已接到四个工具的 canonical PUSH 线。
真相源留在 `~/.local/share/agent-memory/*.git` 跟踪的文件里，kg-hub 只做索引与召回。

## 1. 度量结论（第一轮，2026-10-19）

样本：10 条 Claude Code 记忆 × 2 条自然语言查询 = 20 条，用 `tools/eval_recall.py` 原口径（gold = 正文含关键词的
Episodic；命中 = 返回 fact 的 `episodes` 里有 gold uuid），k=10，对线上 NAS 只读跑。

| 端点 | 查询命中 | literal | nl | 平均 recall@10 |
|---|---|---|---|---|
| `/api/search`（hybrid，默认） | 7/20 | 4/7 | 3/13 | 3% |
| `/api/search_semantic` | 6/20 | 4/7 | 2/13 | 3% |

逐条人工读 top-10 fact 后：

| 分类 | 条数 | 记忆 |
|---|---|---|
| 教训本身完整在图 | 2 | launchctl-bootout-is-async（T-0142 事故当场被抽成 fact）、cerberus-redis-no-xautoclaim（某会话读文件时被 claude-mem 采到） |
| 只有事实碎片，无结论/How-to | 4 | alert-thresholds（只有 mcp-guard swap 判据）、mac-nas-tailscale-flaky（只有 aTrust 一条）、shell-cjk（有「UTF-8 才崩」无准则 31）、plutil（有 plistlib 比对无「-replace 是插入」） |
| 完全不在 | 4 | hard-logic-over-model-discretion、kg-hub-test-runner-false-green、nas-journal-heavy-io、claude-mem-503-local-forwarder-cap |

图内补证（FalkorDB 只读，Episodic 21,679 条）：`**How to apply` / `**Why` / `agent-memory/` 均 0 条——没有任何记忆文件整篇入过图；
文件名只在零星观察里被提到（5 条为 0）；145 条观察提到记忆目录路径但不含内容。

**结论**：claude-mem 线只在事故当场偶尔抽到结论，「方法论 / How to apply」这一层基本不覆盖。「召回不到 = 该推」成立，不存在重复推送。

度量过程的两个坑（已记 T-0332）：`/api/search` 返回前 pop 掉 `episodes`（`kg_hub_server.py` search 末尾），纯 HTTP 做不了 uuid 级判定，
必须直连 FalkorDB（Mac 侧凭据在 `~/.claude-mem/.env`，不在仓库 `.env`）；三个度量脚本并行打线上时 `/api/search` 大面积 502，度量脚本只能串行。

## 2. schema 核查：够用，不加 kind=memory

Episodic 一等字段（北极星 §9，已冻结）：`origin_device` `origin_tool` `origin_project` `kind` `kind_confidence` `durability`
`provenance` `visibility` `verified` `usage_count` `last_used_at` `archived` `scope`（canonical 直推写入）。

| 需求 | 现有机制 | 结论 |
|---|---|---|
| `source=agent-memory/<工具>` | `source_description` 自由文本，且是幂等键的一半；`utils/origin.derive_origin` 按子串派生 `origin_tool`（含 `claude-code` → claude-code，`codex` 开头 → codex） | **不加字段**。约定 `source_description = "agent-memory/<tool> project=<p> type=<t> sha=<8>"`，`project=` 让 `origin_project` 确定性派生；`IngestBody.origin_tool` 可显式覆写兜底 |
| `kind=memory` | `KINDS` 白名单固定 9 值 + unclassified，`IngestBody.kind` 覆写必须在白名单内，否则走派生/分类器 | **不加 kind**（要改 §9 并回填）。按 frontmatter `metadata.type` 确定性映射：feedback→方法论、reference→手册、project→项目事实；覆写 `kind_confidence=1.0`，不过 LLM 分类器 |
| 「这是记忆」的可过滤标记 | `name` 前缀是现有惯例（`kg-hub-canonical-*` 驱动 canonical 注入与 kind=手册） | `name = agent-memory-<tool>-<slug>@<sha8>`；看板 / `/api/search` facet 已能按 `kind` `project` `durability` 过滤，按来源过滤靠 name 前缀 |
| 时效 | `derive_durability` 只扫标题，默认 evergreen | 默认 evergreen；frontmatter 可加 `durability: time-bound`（白名单校验） |
| 来源可信度 | `provenance` 合法值 firsthand / external-* | 人写记忆 = `firsthand` |
| 退休 | `archived` + `POST /dashboard/archive_episode {name}` | 文件改动/删除时归档旧 episode（见 §3.4） |

## 3. 推送方案

### 3.1 入口只能是 `/api/ingest_extracted`

| 路径 | 是否调模型 | 结论 |
|---|---|---|
| `POST /api/ingest`（canonical / claude-mem / task-hub 共用） | **是**，graphiti 经网关抽取；断路器开着会被 held | 不走 |
| `tools/ingest_canonical_docs.py` | **是**，NAS 本地 `add_episode`，同样 LLM | 不走 |
| predigest `catalog` 路由（仅存不抽取） | 否，但只对 `openclaw-capsule-` / `openclaw-kb-` 前缀生效 | 不扩前缀（那是 OpenClaw 长文档线的专用门） |
| `POST /api/ingest_extracted`（T-0313） | **否**：实体/边由调用方给，向量用本地 fastembed（`graphiti_client.FastembedEmbedder`），不经断路器、不花钱 | **走这条** |

因此任务原句「复用 canonical PUSH 路径」要拆成两半：**入库**走 `/api/ingest_extracted`；**回流**走 `kg_push_hook` → `/api/canonical_context`，
后者才是 canonical PUSH 线（§4）。

### 3.2 确定性抽取（无 LLM）：记忆文件本身就是预抽取产物

`/api/ingest_extracted` 要求 ≥1 实体；`/api/search` 只返回**边的 fact**，所以召回承重的是 fact 文本，不是 episode 正文。
每条记忆按下面规则机械生成（纯函数，可测）：

| 产物 | 规则 |
|---|---|
| 实体 `Lesson` | `name` = 记忆 `name` slug；`statement` = frontmatter `description` |
| 实体 `Project` | 记忆桶目录 → 项目名（映射表：`-Users-mac-work-ai-kg-hub` → kg-hub …；`-Users-mac-work-ai` 顶层桶 → 无 Project，改挂 `Tool` = 工具名） |
| 实体 `Lesson`（关联） | 正文 `[[name]]` 链接目标（80/95 条有链接），只建实体不复制正文 |
| 边 `belongs_to` Lesson→Project | **fact = `description` 原文**（人写的一句话结论，天然是 fact） |
| 边 `relates_to` Lesson→Project（第二条） | fact = `**How to apply**` 段首句（66/95 条有） |
| 边 `relates_to` Lesson→Lesson | fact = `<slug> 关联 <目标 slug>` |
| `episode_body` | 整篇正文（fulltext 可搜；`/api/episode_search` 兜底） |
| `extractor_model` | 固定 `agent-memory-deterministic`（来源标记，不是模型；已核：符合 `EXTRACTOR_MODEL_PATTERN`，且该路径同样调 `_tag_schema_fields` 打 origin/kind） |

一条记忆通常 2–4 个实体、2–4 条边，远低于 `MAX_ENTITIES/MAX_EDGES`；`normalize_extraction` 的纪律指标（dirty_name 等）照常生效。
先 `dry_run=true` 看消解（`Project`/`Tool` 会并到已有节点，`Lesson` 名是 slug 不会误并）。

### 3.3 范围：按「有没有更合适的主人」分三档（与 T-0302 合并）

T-0302 已于 2026-10-18 关闭：5 条「某样东西在哪、怎么改」的流程知识已搬进所属 skill / README / release.sh 并删记忆。
它留下的规则直接变成推送脚本的**不推清单**：

| 档 | 判定 | 处理 | Claude Code 估量 |
|---|---|---|---|
| A 推 | type=feedback（方法论/教训）、type=reference 里跨项目的坑 | Phase 1 推 | feedback 36 + reference 13 ≈ 49 |
| B 搬 | 「在哪、怎么改」的流程知识，有明确主人（skill / README / 脚本） | 开 PR 搬走后删记忆，不推（T-0302 流程） | project 46 里人工过一遍，预计 10–15 |
| C 不推 | 已是 canonical 文档影子（kg-hub-northstar、cognitive-asset-plan、capsule-ranking 工作日志）；纯个人偏好；含机密待核的 | 跳过，脚本里 `skip:` 清单 | ≈ 10 |

四个工具的顺序：
1. **Claude Code**（95 条，结构固定 frontmatter + Why/How）→ Phase 1。
2. **Qoder**（202 条，3 个工作区，目录即分类）→ Phase 2：`common_pitfalls_experience` 28、`important_decision_experience` 7、
   `development_practice_specification` 6 先推（kind 映射：事故 / 决策 / 方法论）；`project_introduction` 37、`project_tech_stack` 28、
   `*_configuration` 等属项目事实，多数已在 canonical 文档或代码里，后议。
3. **Codex**：18 个文件是 `raw_memories.md` + 16 篇 rollout summary，是 Codex 第二阶段整理的**基线输入**，粒度更像观察而非结论；
   且仓库规则是不碰工作树。**不推**，等 Codex 自己整理出 `memory_summary.md` 再按 Phase 1 格式评估。
4. **DSH**：sqlite 8 条，`git show --textconv` 导出后手工转 Phase 1 格式，量小不值得写适配器。

### 3.4 幂等、更新、删除：真相源在文件

- 幂等键沿用服务端的 `(source_description, source_obs_id)`：`source_obs_id = 文件内容 sha256`，`source_description` 含 `sha=<8>`。
  同一内容重推 → `status=skipped`。
- 水位线 `data/.ingested.agent-memory.json`（与 `ingest_canonical_docs.py` 同款：name → sha）。
- 文件改了：推新 episode（name 带新 sha8）→ `POST /dashboard/archive_episode {name: 旧名}`。name 带 sha 后缀就是为了归档不误伤新的。
- 文件删了（git 里消失）：归档。
- 不做反向同步：kg-hub 里的任何面板操作都不回写文件。图挂了一条不丢。
- 排空期（发布）`/api/ingest_extracted` 返回 503 + Retry-After：脚本按头重试，不并行（§1 的 502 教训）。

### 3.5 机密门

README 已标：Claude Code 记忆里 20 个文件提到 password / token / webhook / `.env`。推送前用 fleet-ops 的
`tools/tool-output-redaction` 规则扫正文，命中的进 C 档待人工核，不自动放行。

## 4. 回流方案：让 Qoder / Codex 看到 Claude Code 的教训

现状：四个工具的 PUSH 都已接 `kg_push_hook.py`（Claude/Qoder 走 SessionStart hook，Cursor 写 `.cursor/rules`，Codex 走 AGENTS.md pull），
服务端是 `/api/canonical_context`：pass 1 只取 `name STARTS WITH 'kg-hub-canonical'` 并按 `scope` 打分；pass 2 用 FalkorDB fulltext 填位，
**中文不分词**，所以中文记忆正文只有在含项目关键词（ASCII）时才会被 pass 2 碰到。

| 方案 | 改动 | 效果 | 建议 |
|---|---|---|---|
| B0 零改动 | 无 | 会话里 `kg_search` / kg-use skill 走 hybrid 语义召回能拿到（fact 已向量化）；SessionStart 不会主动注入 | **先做**，推完 Phase 1 用 §5 黄金集量一次 |
| B1 最小改动 | ① `canonical_context` pass 1 的前缀改成列表 `('kg-hub-canonical', 'agent-memory-')`；② `ExtractedIngestBody` 加可选 `scope`（写 `n.scope`，值 `project:<p>` / `global`，白名单校验）；③ 注入预算 1500 字符不变，记忆只注 `description`（≤110 字）不注正文 | 按 cwd 项目在 SessionStart 注入最相关的 1–2 条记忆结论，跨工具生效 | B0 量完若 S1 命中达标仍想要「不问就给」再做；两处改动 + 测试，不碰 schema |

Codex 没有命令钩子，回流只能靠 AGENTS.md 里的 pull 指令，这一点与 canonical 一致，不额外做。

## 5. 度量回路（北极星「先度量再建设」）

- 黄金集：本轮 20 条查询（scratchpad `golden-memory.json`）搬进 `docs/golden-queries.json` 作 S1-memory 段，或单独 `docs/golden-queries-memory.json`。
- `tools/eval_recall.py` 加可选 `gold_name`：推入后 gold 应是「该记忆的 episode」而不是关键词子串，否则 recall 分母被无关观察稀释。
- 验收线（Phase 1 推完后）：hybrid nl 命中 3/13 → ≥ 10/13；按 `gold_name` 的 recall@10 ≥ 80%。不达标先查 fact 写法，不加 LLM。
- 使用回流：`kg_push_hook` 的 `usage_count` bump 已有；两周后看 `agent-memory-*` 的 usage 分布，为 0 的那批回头看是不是 C 档。

## 6. 不做 / 风险

- 不动断路器、不批 A/B、不改 NAS 容器、不碰 `~/.codex/memories/.git`。
- 不新建管道、不过 LLM 抽取、不加 kind、不改 §9。
- Mac↔NAS 链路抖动（symmetric NAT，见记忆 mac-nas-tailscale-flaky）：脚本串行、每条独立重试、失败只记日志不中断。
- 实体消解误并：`Lesson` 用 slug 名；`Project`/`Tool` 名走已有节点；dry_run 先看一遍。

## 7. 需用户拍板

1. 范围：Phase 1 只推 A 档 ≈ 49 条，还是 95 条全过一遍人工分档后一起推？
2. 回流：先 B0（零改动）量完再说，还是直接做 B1？
3. kind 映射（feedback→方法论、reference→手册、project→项目事实）与「不加 kind=memory」是否接受？
4. 黄金集放进 `docs/golden-queries.json` 还是单独文件？

## 8. 批准后的实施清单

1. `tools/memory_push.py`：读 git 工作树（只 `git ls-files`，不 checkout）→ 解析 frontmatter → 三档判定（`skip:` 清单可配）→ 机密扫描 →
   确定性抽取 → `dry_run` → 提交 → 水位线 → 归档旧版。纯函数部分配测试（抽取规则、三档判定、name/sd 格式）。
2. B 档清单人工过一遍，按 T-0302 流程逐条开 PR 搬走。
3. 黄金集与 `eval_recall --gold-name` 改动。
4. Phase 1 推送（串行、低峰），跑度量，记 T-0332。
5. 视结果决定 B1 与 Phase 2（Qoder）。

## 9. 实施状态（2026-10-19，用户已批准：A 档 ≈49 条、回流 B0、kind 映射、黄金集并入）

| 项 | 状态 |
|---|---|
| `tools/memory_push.py` + `tests/test_memory_push.py` | 完成（list / dry-run / push / status；20 用例） |
| 黄金集 20 条并入 `docs/golden-queries.json`（`s1-mem-*`，带 `gold_name`）；`eval_recall --grep/--by-name` | 完成 |
| Phase 1 推送 | **已上线**：49 条 A 档按 memory-v1 入图，0 失败；kind 方法论 36 / 手册 13；143 条边有向量；水位线 `~/.kg-hub/agent-memory/` |
| B0 度量（推后，by-name，k=10） | hybrid 命中 5/20；已推 16 条里 literal 5/7、**nl 2/11**，未达验收线 |
| 根因 ① | fact 的英文 slug 前缀在 bge-small-en 下压过中文语义：用 fact 原文检索三条都排第 1，但纯中文 description 当查询召不回自己（alert-thresholds / hard-logic）；description 含 ASCII 词的都能召回。→ **memory-v2**：主 fact 去前缀，规则版本进幂等键（`sha:版本`），旧水位线自动判 changed；代码与测试已就绪 |
| 根因 ② | `utils/extracted_ingest.py` 的 `_FIND_EDGE` 只按 (源, 边名, 目标) 复用边、不比较 fact → 重推的新 fact 写不进去，旧 fact 还被新 episode 背书续命；今后记忆改版也刷新不了。→ 复用条件加 `e.fact = $fact`（同一对实体允许多条不同 fact 的边，与 graphiti 一致），`test_extracted_ingest` 新增用例 |
| v2 重推 | **已完成**（2026-10-19 11:44 CST，线上 079308c 已含 #196）：`push` 计划 新 0 · 改 49 · 不变 0 · 删 0；结果 推送 49 · 失败 0 · 归档 49。FalkorDB 核对：`agent-memory-*` 未归档 49 条全为 memory-v2、已归档 49 条全为 memory-v1；v2 主 fact 无 slug 前缀、有 `fact_embedding`；同一对实体 v1/v2 两条不同 fact 的边并存（根因 ② 修复生效） |
| v2 复测（by-name，k=10，两端点串行） | `/api/search` 与 `/api/search_semantic` 结果完全一致：literal 4/7、nl 2/13、全部 6/20，平均 recall@10 30%。扣掉 4 条 gold=0（kg-hub-test-runner-false-green、claude-mem-503 属 B 档未推）：**literal 4/5、nl 2/11**，与 v1 基线持平，**未达验收线 nl ≥10/13** |
| 复测定位 | 用 v2 fact 原文查 `/api/search` 排第 1，入图与检索链路正常；中文改述查询（如「给监控定告警阈值应该怎么取值才不会误报」「能不能让模型维护状态」）top-10 全是无关短句；命中的两条 nl（tailscale）和带 `mcp-guard`/`swap` 的查询都靠 ASCII 词。结论：去前缀只消除了干扰项，**瓶颈是 bge-small-en 对中文语义的能力**（北极星长尾「muxcp-nl embedding 鸿沟」）；fulltext 腿无 CJK 分词，中文 nl 查询实际只剩向量腿 |
| 独立复测（上游会话，服务恢复后串行重跑同一命令） | 与本轮一致：已推 16 条里 literal 5/5、nl 2/11（本轮 literal 4/5，差在「deploy-standard 准则 31 说的是什么」一条）。判别：8 条已推记忆用自己的 description 当查询全部排第 1（v1 时 alert-thresholds、hard-logic 召不回自己）——去前缀修复生效；nl 改写查询在 top-30 里 tailscale 3/5、cerberus 12、shell-cjk 21、nas-journal 24，其余不在。图内核对同上，另确认 v2 每 slug 唯一、旧前缀边未被 v2 背书 |
| 度量窗口提醒 | 03:47Z 前后另一会话发布 #197（f7c0fcf，含 #196），kg-hub 容器重建，期间 `/api/search` 大面积 502；那一轮数据作废需重跑。本轮两次复测无 search-fail、每条返回 10 个 fact，数据有效 |
| 度量方法坑 | 度量脚本只能串行打线上（并行时 `/api/search` 大面积 502）；FalkorDB 直连凭据在 `~/.claude-mem/.env` |

### 复测后的下一步（待用户定）

- **B1 回流可以先做**：按 `agent-memory-*` 名前缀在 SessionStart 注入（canonical_context 前缀列表 + `ExtractedIngestBody` 加 scope），不经向量检索，不受 embedding 瓶颈影响；「Qoder / Codex 看到 Claude Code 的教训」这个目标能先达成。
- **nl 召回要靠换 embedding 模型**：多语种模型（bge-m3 / multilingual-e5 一类）才能让中文改述命中；需全图重算 `fact_embedding` 并改 NAS 容器，是独立任务，不在本任务范围。换模型后用同一黄金集复测：`eval_recall --grep s1-mem --by-name`（两端点串行），验收线不变 nl ≥10/13。
- Phase 2（Qoder 202 条）在 embedding 未换之前只有 literal 与 B1 回流两条路可用，建议与 B1 一起评估，不单独推。
- 不建议再在 fact 文本上加英文关键词之类的补丁：v1 的 slug 前缀实验已证明这类改动只影响 literal 命中，对 nl 无益。

## 附：第一轮 10 条样本与查询

| 记忆 | 查询（nl） | hybrid 命中 | 判定 |
|---|---|---|---|
| alert-thresholds-need-real-distribution | 给监控定告警阈值应该怎么取值才不会误报 / mcp-guard 的 swap 告警一直红但没有真故障 | 0 / 8% | 碎片 |
| launchctl-bootout-is-async | bootout 后马上 bootstrap 报 I/O error / 批量重装 launchd 服务怎么避免全停 | 5% / 0 | 完整 |
| mac-nas-tailscale-flaky | 直连几分钟掉回 DERP / ping ssh 不通但 tailscale ping 通 | 0 / 3% | 碎片 |
| hard-logic-over-model-discretion | 能不能让模型维护状态 / task-hub 为何不靠模型自觉维护租约 | 0 / 3%（无关 fact） | 不在 |
| kg-hub-test-runner-false-green | kg-hub 怎么跑测试 / discover 0 用例 rc=0 算不算过 | 0 / 0 | 不在 |
| nas-journal-heavy-io | NAS 账本库跑统计要注意什么 / 全表扫描影响入库吗 | 0 / 14%（只有文件位置） | 不在 |
| shell-cjk-expansion-utf8-not-c | 变量紧跟中文标点报 unbound / 准则 31 是什么 | 0 / 0 | 碎片 |
| plutil-array-replace-inserts | -replace 后多出旧参数 / 怎么安全改 plist 参数 | 10% / 0 | 碎片 |
| cerberus-redis-no-xautoclaim | 报 unknown command XAUTOCLAIM / Redis<6.2 怎么认领孤儿消息 | 18% / 0 | 完整 |
| claude-mem-503-local-forwarder-cap | 一簇 503 但 readiness 正常 / 对 503 重试能不能兜底 | 0 / 0 | 不在 |
