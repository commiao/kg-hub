# kg-hub 北极星 — 知识库总纲(初心锚)

> **这是本倡议的宪法。任何会话/子代理开工前必读本文;任何改动不得与「不变量」冲突。**
> 状态:骨架草案 2026-07-23,待用户改定。标 `【待定】` 处需拍板。
> 配套落地文档:治理见 [KNOWLEDGE-GOVERNANCE.md](KNOWLEDGE-GOVERNANCE.md);本文管"为什么/存什么/查什么",不写实现细节。

---

## 0. 一句话目标

**一个跨设备、跨工具的中央知识图**,服务三类真实查询(AI 会话召回 / 公众号素材供给 / 项目生命周期记忆),靠**持续治理**成长——而非靠囤积。

设计顺序铁律:**数据流是 采集→治理→召回,但设计顺序必须是 召回→采集→治理**。元数据存什么由查询场景倒推;治理的唯一目的是让元数据可信到召回能用。

---

## 1. 查询场景(第一性,驱动一切)

这三个场景**定义**了 schema 要存什么、召回要怎么排。脱离它们的字段一律不加。

### S1 · AI 会话召回(跨工具/跨设备)
各工具(Claude Code / Cursor / Codex / Qoder / OpenClaw)在与 AI 会话时,能取到**相关信息、方法论、手册**。
- **过滤**:按当前 project(±跨项目);`kind∈{方法论,手册,决策,事故,项目事实}`;`durability=长期` 或仍在有效期;优先 `provenance=firsthand`
- **排序**:相关性(语义) × 权威(verified/visibility) × 时效 × 使用量(Lindy 先验)
- **匹配**:混合召回(语义向量 + 图遍历 + 关键词),**不是**当前的子串 grep

### S2 · 公众号素材供给
公众号自动化时,给出满足选题诉求的**素材**,支撑素材库。
- **过滤**:`visibility∈{可公开,方法}` 或 `kind∈{素材,公开故事}`;优先 `verified=true`;**允许 external**(素材不是断言,二手可用)
- **排序**:与选题/角度的相关性 × verified × 素材新鲜度
- **匹配**:按选题语义 + 实体检索

### S3 · 项目关键信息 / 生命周期 / 历史演进
同步各项目的关键信息、生命周期、历史演进。
- **过滤**:`origin_project = P`;`kind∈{决策,生命周期事件,项目事实,事故}`
- **排序/组织**:按 `reference_time` 排成**时间线**(不是按相关性)
- **匹配**:facet 分面查询,不是搜索

> 三个场景的 (匹配×排序×过滤) 画像各不相同——这正是为什么 device/tool/project/kind/durability 这些维度必须存在。

---

## 2. Schema(每个字段 = 一个可信生产者 + 至少一个消费场景)

### 铁律:自报 ≠ 分类;作者不给自己打标
被禁的是**生成方给自己的产物打标**(kb-001 给自家胶囊评价值等级 = 裁判兼运动员,已证 57% 自评"高"、来源行三次漂移)。**可信元数据来自四种生产者**:
- ① **确定性派生**(代码从环境/来源行算出)
- ② **人工确认**(待办一键)
- ③ **使用派生**(usage_count/last_used)
- ④ **专职分类器 pass**(独立一次 LLM 调用,只干分类;强制输出**置信度**;低置信**弃权→转 ② 人工一键**;定期拿人工标注样本校准准确率)

④ 与"作者自报"的区别:分类器不评判内容好坏、与生成无利益关系、且有弃权+校准两道闸。填不出这四种生产者之一的字段——不加。

### 来源维度(你的第 1 点:区分 来源/设备/工具/项目)
| 字段 | 取值示例 | 生产者(可信来源) | 消费场景 |
|---|---|---|---|
| `origin_device` | mac / oc-vps / nas / … | ① 摄入客户端固定盖戳 | 跨设备归属、S3 |
| `origin_tool` | claude-code / cursor / codex / qoder / openclaw / claude-mem / kb-001 | ① 客户端 / PUSH hook 盖戳 | S1(哪个工具的会话)、跨工具 |
| `origin_project` | workspace_claudeCode / kg-hub / openclaw / investment / finance / … | ① 由 cwd/配置派生 | S1 过滤、S3 时间线 |
| `provenance` | firsthand / external-article / external-community | ① 来源行派生(已上线) | 排序(external 沉底)、S1 偏亲历 / S2 允许 external |

> 现状:device/tool/project 现在挤在 `source_description` 的自由文本里(`type=`/`project=`)。**本纲要求提升为一等字段**,新摄入直接写;**存量 2302 节点全部回填(2026-07-23 定)**——能从现有 `source_description`/`name`/来源行确定性派生的直接派生,派生不出的标 `unknown` 而非留空(治理赛道执行,先备份)。

### 内容维度(驱动三场景的分流)
| 字段 | 取值 | 生产者 | 消费场景 |
|---|---|---|---|
| `kind` | 方法论 / 手册 / 决策 / 事故 / 项目事实 / 生命周期事件 / 素材 / 公开故事 | **④ 分类器 pass(LLM 优先)+ 低置信 ② 人工一键**;`kind_confidence` 一并存 | S1(方法论/手册)、S2(素材/公开故事)、S3(决策/生命周期/项目事实) |
| `durability` | 长期(evergreen) / 时效(time-bound,+可选 valid_until) | ① 启发式派生(行情/日报/快照→时效)+ ② 人工覆写 | S1/S3(不返回过期内容) |
| `reference_time` | ISO 时间(已存) | ① 派生 | S3 时间线排序 |

> **`kind` 已定(2026-07-23)**:专职分类器 pass,LLM 优先;`kind_confidence < 阈值`(初定 0.7,可调)则弃权,置 `kind=unclassified` 并进「反馈待办」等人工一键。分类器是**独立 pass**(非胶囊生成方 kb-001),须定期用人工标注小样本校准——校准样本由治理赛道维护,呼应"先度量再建设"。

### 治理维度(已有)
`visibility`(internal-note/professional-guide/public-story) · `verified`(bool) · `usage_count` · `last_used` · `archived`。生产者:人工确认 + 使用派生。

---

## 3. 三条赛道与边界

| 赛道 | 范围(做) | 不做(边界) | 依赖 |
|---|---|---|---|
| **① 采集/Schema** | 提升 device/tool/project/kind/durability 为一等字段;所有摄入路径(claude-mem / openclaw 直推 / canonical)统一盖来源戳;存量回填 | 不改召回算法;不设自报字段 | 无(地基,先做) |
| **② 治理/质量** | 入口去重(预防式)、退休/衰减回路(缺口)、人工一键回路、存量回填 | 不做自动贡献度打分(不可识别,已封存) | 依赖①的 schema 冻结 |
| **③ 召回/检索** | 混合召回(语义+图+关键词)、多因子排序(复用 canonical 排序)、分面过滤、**黄金查询集**(先建,否则调不动) | 不改 schema | 依赖①冻结 + ②提供干净数据 |

**排期(2026-07-23 定)**:① schema 先单独落定并冻结 §2 契约 → 然后 **②治理 与 ③召回 并行**(两会话/两 worktree,各动不同代码面;共改 `kg_hub_server.py` 时守部署前 sha 对账 + 提交纪律,防漂移)。

---

## 4. 跨设备 / 跨工具(你的第 3 点)

既是**生产者要求**(每条摄入都盖 `origin_device/tool/project`),也是**消费者要求**(查询可按设备/工具归属与过滤)。单一事实源 = NAS 图为共享层;各设备/工具的**原始素材留本地**,只导出策展后的知识进图(联邦不合并)。PUSH hook 注入时已上报 tool(ToolStat),摄入侧要对齐同一套 origin 盖戳。

---

## 5. 自进化的定义(你的第 4 点:= 持续治理,不是自动打分)

**"自进化"不是让系统自动猜哪条知识有贡献(本领域不可识别,已封存 tools/experimental)。** 它是一个由诚实信号驱动的治理闭环:

```
入口门(格式/去重/盖戳)→ 人工一键确认(visibility/verified/kind)→
使用回流(usage_count/last_used + 文章表现 ArticleFeedback)→
排序据此加权(用得多/被验证/有产出的浮起)→ 退休/衰减(过期/长期不读的沉降归档)
                                  ↑___________持续循环___________|
```

图变好,是因为治理持续把"被验证/被使用/有产出"的知识提上来、把"过期/无人问津"的沉下去——靠的是 usage、文章表现、人工确认这些**诚实信号**,不是自动臆测。

---

## 6. 不变量(初心守卫,不得违反)

1. **自报元数据 = 噪音**;可信元数据只来自 确定性派生 / 人工确认 / 使用派生。
2. **先度量再建设**:黄金查询集先于检索算法;读取 ROI 未证明前不加机制。
3. **联邦不合并**:原始素材留本地,只导出策展知识进共享图。
4. **每个字段配 生产者 + 消费场景**,否则不加。
5. **入口 fail-closed**:不合规不入图,进隔离区待人工。
6. **可逆优先**:标记/归档 优先于删除;破坏性操作先备份、先确认。
7. **单一事实源**:NAS 图是共享层,不是原始素材仓;真实源在各设备本地。

---

## 7. 当前状态 & 缺口(2026-07-23)

**已就位**:provenance 分类+降权、格式门+隔离区、VPS 直推(一跳)、待办人工一键回路、canonical 注入排序、历史重复已去重(72 胶囊)。

**① 采集/Schema —— 已完成(2026-07-23)**:
- `origin_device/origin_tool/origin_project` + `durability` + `kind`/`kind_confidence` 已提为一等字段(`utils/origin.py` 纯派生;`kg_hub_server._tag_schema_fields` 在每次 ingest 打标;kind 走独立 LLM 分类器 pass,低置信弃权 unclassified)。
- 存量 2302 节点全部回填完毕(`tools/backfill_schema.py`):0 空值。kind 分布 项目事实1532/事故397/决策276/方法论20/手册11/素材4/生命周期1/unclassified61(=~60 无 type 小尾巴 + 1 低置信胶囊)。
- **运维教训**:Mac 的 `~/.claude-mem/.env` ANTHROPIC token 对直连 401 失效;胶囊 kind 的 LLM 分类改在 **NAS 容器内**跑(`docker exec kg-hub-ingester python -m tools.backfill_schema --retry-llm`,容器 token 有效)。回填工具的 D2 防护(失败计数+非零退出+--retry-llm)正是为此。

**② 治理 —— 核心已落地(2026-07-23)**:
- **退休回路**(补"只进不出"缺口):反馈待办「④待退休」自动列过期 time-bound(行情/日报/快照 >30 天,当前 24 条),一键/批量归档(`archived=true` 可逆,看板与 search 均 `NOT archived` 过滤)。端点 `/dashboard/archive_episode`。只收 time-bound——evergreen+usage=0 是弱信号(usage 探针覆盖不全,会误伤 2228),不据此退休。
- **分类器校准环**:`docs/kind-calibration.json`(14 人工标注)+ `tools/eval_kind.py`。基线 71%→ 发现系统偏差(日报/流水账被误判事故/决策)→ 给 KIND_PROMPT 加"整篇日报→项目事实"规则 → 容器重判 → **100%**。度量→修→再度量环跑通,即"自进化=持续治理"的实证。
- 未做(已评估):预防式去重——VPS-sha 水印 + server (sd,sid) 幂等已覆盖主要向量,残留跨源同内容风险低;61 个 unclassified 多为无 type 的 codex/杂项尾巴(2.6%),接受为长尾,不强制分类。

**③ 召回 —— 已落地(2026-07-23)**:
- `/api/search` 改 **hybrid**(语义主序+子串精确提权+多因子排序+facet 过滤)。基线:子串命中 3/15 → hybrid 12/15@10、14/15@20,追平/超过纯语义,且多了排序治理与分面。黄金集 `docs/golden-queries.json` + `tools/eval_recall.py`,详见 `docs/RETRIEVAL-BASELINE.md`。
- 残留:muxcp-nl 一条 embedding 语义鸿沟(需查询扩展/更强模型,未来)。

---

## 8. 决策记录(2026-07-23 已定)
- **P1** `kind` 生产者:**混合,分类器 pass(LLM)优先,低置信弃权转人工一键**。分类器独立于生成方,存 `kind_confidence`,人工样本校准。(见 §2)
- **P2** 存量 2302 节点 origin/kind:**全部回填**;确定性能派生的派生,派生不出标 `unknown`(不留空)。先备份。
- **P3** 排期:schema 冻结后 **②治理 ∥ ③召回 并行**(两会话,守 sha 对账+提交纪律)。

## 9. 冻结的 schema 契约(§2 即契约本体)
新增一等字段:`origin_device` `origin_tool` `origin_project` `kind` `kind_confidence` `durability`(+可选 `valid_until`)。既有保留:`provenance` `visibility` `verified` `usage_count` `last_used` `reference_time` `archived`。**②③ 一律面向本契约开发,不得私改;需改先回本文改定并通知另一赛道。**
