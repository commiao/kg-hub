# 抽取对比战役：一个编排者 + 每个模型一个子代理（任何工具通用）

> 目标：在**给定配额**上看各模型在生产中的产出画像，每批复盘、改链路，形成常态可复用的流程（模型高下以 bench 准入为准）。
> 消化积压不是目标；但每个模型抽的都是**不同的新观测、非空结果入库、空项正式记账**，已处理观测包含非空入库和正式判空，两者分列（10-22 三模型 2000 条的做法）。
> 工具：`tools/extract_campaign.py`。规则：开局时从 `config/agent_extract_prompt.md` 冻结的快照（`prompt.md`）+ 战役补充约定（累加，c1…cN 都生效）。四条红线见 [_common.md](_common.md)。
>
> **一句话入口**：skill `kg-model-eval`（仓库 `skills/kg-model-eval/`）——「使用kg-hub对比模型+冒烟 A、B」「使用kg-hub对比模型+准入 A、B」「使用kg-hub对比模型+盲测」「使用kg-hub对比模型+A、B、C 20个一轮 共1000条」。
>
> **定位**：bench 管**准入**（同一批冻结样本、小批、过滤不合格的模型），战役是**生产画像**（新样本、全入库、边生产边改链路）。
> 各模型在战役里抽的是各自的新观测，复盘表是「各模型在各自新样本上的产出画像」，**不是**因果意义上的模型排名；
> 模型能不能用、谁更好，看 bench 准入记录。

## 新运行的公共入口与版本

先按 [_common.md](_common.md) 设置真实的 `PY`、`KG_HUB_CODE` 和 `CLI`；所有工具均调用
`"$CLI" campaign ...`，不从会话目录加载任意旧的 `tools` 包。新战役默认 `agent-v5` / `extraction-quality-v1`。
开跑前 `"$CLI" campaign preflight` 核对实际代码、规则哈希、依赖、采集排除和服务端能力，失败先修环境。
质量表与正式修复的契约见 [quality.md](quality.md)。旧战役仍冻结旧正文，旧准入不自动升级到新版本；新规则必须重新准入或使用明确授权的例外。

## 开战役之前：bench 准入（每个模型一次）

新 `start` 只接受新版 `admission-v2` 的最新 `pass`、匹配规则正文和激活套件的语义回执，并要求每模型 15 分钟内真实 `runtime probe` 凭据。旧 pass 可补审既有 native run，不自动升级。确需准入例外用已获授权的 `--skip-admission "原因"`，不跳过真实模型探测。详见 [新版评测工作流](../../skills/kg-model-eval/references/evaluation-workflow.md)。

先确认参考与当前规则兼容；旧 c8 仅等价于 v4，v5 需按 [当前规则下建立新的 bench 参考](bench-reference.md) 生成真实参考。以下命令的样本目录应使用该兼容参考。

```
"$CLI" bench run   --bench "$V5_BENCH" --executor codex|claude|qodercli --models <模型> --tag adm50 --limit 50
"$CLI" bench blind --bench <样本> --run <运行目录...> --n 10 --out <评审表>.json
#   评审者打开评审表（不含模型名），对照观测原文把每条的 verdict 填成 ok / error（error 写 note）；评完前不看 <评审表>.json.key.json
"$CLI" regression sheet --bench "$V5_BENCH" --run <运行目录> --out <待评定向表>.json
# 独立评审填证据后：
"$CLI" regression check --bench "$V5_BENCH" --run <运行目录> --review <已填定向表>.json --out <语义回执>.json
"$CLI" bench admit --bench "$V5_BENCH" --run <运行目录> --sheet <评审表>.json --semantic-review <语义回执>.json
```

| 门槛 | 要求 |
|---|---|
| 规则版本 | 候选、基线的规则版本与正文哈希需匹配；bench-20261022旧基线为agent-v4等价规则，新agent-v5不能伪装沿用旧pass |
| 硬性 | 未交 0、自检问题 0、边名越界 0、悬空边 0、未知类型 0；照抄标题 ≥95%、译文边 ≥95%；其余 fact 英文占比 ≥90%、长度落在 60–200 字符的 ≥80%（规则 23） |
| 质量 | 中英问句 top-10 命中各 ≥90%；在每个基线模型那部分不比该模型低 5 个点以上 |
| 正确性 | 盲评 10 条，事实错误（方向、数字、因果写反）≤1 条；评审表附模型调用时的 lookahead，依据它写的后续更正不算编造 |
| 空观测 | 空观测率不比基线在同一批观测上高出 5 个点以上 |
| 基线对照 | 至少有一个基线模型那部分能配上对（配不上直接判不过，不静默跳过子集检查） |
| 规则正文 | 准入记录带规则正文哈希；开战役时与开局快照比对，版本号相同但正文改过也要重新准入 |
| 条数 | ≥30 条才可能 `pass`；10 条冒烟最多 `screen_pass`（快速筛选通过，不算准入） |

`admit` 不带 `--sheet` 只预览、不写记录（避免一次随手的预览压过此前的 pass）；要撤销准入用 `extract_bench revoke --model <模型> --reason <原因>`。
准入自检只计硬错误（schema、悬空边、缺 edges 键）；照抄标题、译文边交给比例门槛，没交的交给「未交 = 0」。
结论包括 `fail`、`screen_pass`、`pending_blind`（缺盲评）、`pending_semantic_regression`（缺定向评审）、`semantic_regression_fail`（真实定向错误）、`pass`；缺新版评审不写质量失败。完整正式判定追加进本机
`~/.kg-hub/bench/admissions/<模型>.json`（模型、bench、规则版本、逐项检查、盲评、日期），以最新一条为准。
阈值写在 `tools/extract_bench.py` 的 `THRESHOLDS`，改阈值走 PR。成本不设门槛：处理条数（配额）就是成本上限。

## 规则冻结、回收与续跑

- **规则冻结**：`start` 把规则正文存成战役目录里的 `prompt.md`，`rules` 只读这份快照；仓库里的规则之后再改，本战役不受影响
  （`rules` 会提示）。战役内改链路只走 `chain` 补充约定；入图的 `prompt_version` 记为「正文版本+约定版本」（如 `agent-v5+c2`；旧战役仍保留其 `agent-v4+c2`），
  改链路前后在图里也能分开。换规则正文请开新战役。
- **补充约定是累加的**：`rules` 按登记顺序输出 c1…当前版的全部补充约定（前面加一行「累加：c1、c2… 依次生效」），
  后登记的与前面冲突时以后登记的为准；每个批次只带它领取时那一版及之前的约定（c1 时领的批次不会看到 c2）。
  所以 `chain` 只登记**本轮新增或改动**的约定，不用再把旧约定抄进新文件；要废止旧约定，在新约定里写明「取消 cN 的某条」。
  口径记在 `campaign.json` 的 `chain_mode: cumulative`（`status` 显示 `chain_versions`）。2026-10-24 之前开的战役没有这个字段，
  仍只输出当前一版——它们已审、已预览的批次绑定的是当时的规则文本，改口径会让质量表与 dry-run 标记全部失效；
  这类战役想让旧约定继续生效，仍要登记合并版（10-24 战役的 c4 就是这样补救的）。
- **续跑**：子代理中途挂掉，批次文件和已写的结果都还在——新派的子代理用同一个批次号接着 `lint` / `submit` 即可，不用回收。
- **回收**：批次做坏了不想交、或子代理带着它走了：`"$CLI" campaign release --id <战役> --batch <批次> --reason "<原因>"`。
  没正式提交的整批放回队列；已提交的只放回没入库的那几条。放回的条目还给配额，下一次任何模型 `claim` 时优先领
  （这期间已在别处入库的自动丢掉）。不要手改 `campaign.json`。

## 自检（lint）查什么

`lint` 不过不许 dry-run、不许正式提交（`submit` 先跑一遍 lint）。子代理不用再自己写脚本查下面几项：

| 检查 | 口径 | 何时查 |
|---|---|---|
| 悬空边、实体类型 / 边名越界、缺 `edges` 键 | schema 硬错误 | 一直查 |
| 第一条边照抄标题、第二条边是标题译文 | 规则 21、22 | 一直查 |
| 其余 fact 超长 | 第 3 条边起，空白归一后**超过 200 字符**（与复盘表「其余长度合规%」同一量法）；逐条列出边序号与两端实体。按规则 31 拆成几条边或重写，不截断 | 一直查 |
| 有向边写反方向 | 两端类型恰好是 `schema.EDGE_TYPE_MAP` 约定的反方向：`fixed_by` Fix→Issue（应 Issue→Fix）、`documented_in` KnowledgeDoc→Concept、`derived_from` Issue→Lesson、`belongs_to` Project→File / Project→Session；逐条列出边序号与两端实体（含类型），对调 source / target。只是类型不在约定里（如 `fixed_by` Issue→Tool）不报。准入判定不计这一项（与规则 21/22 同属执行者自检） | 一直查 |
| 新建实体 summary 过短 | 少于 **30 字**（`strip` 后的字符数，规则 24）；只查 dry-run 返回的 `new_entities` 里的实体——并到图里旧实体的、并到同批前面条目新建实体的（`dry_run_pending`）都不查 | dry-run 之后 |
| 结构过薄（**提醒，不阻断**） | 正文点名 ≥2 个不同的具体对象，而结果只有 ≤1 个端点对或 ≤2 个实体；列出 obs id 与识别到的对象样例。不计入问题数、不改退出码，`submit` 前的自检也不看它；bench `lint` 同样只提醒、不进准入判定。识别口径见下文「复盘表的结构指标」 | 一直查 |
| 结构门槛（**整批，阻断**） | 战役启用了结构门槛时：整批恰2实体% / 端点对/条 / 可选 relates_to 超阈值，退出码 1，逐条列出偏薄条目。见下文「结构门槛」 | 战役启用、批次 chain ≥ `from_chain` |

新建与否离线分不出来（哪些名字会并到旧实体要服务端消解才知道），所以 summary 这一项取**最近一次 dry-run** 的结果：
每次 dry-run 把各条的 `new_entities` 记进批次的 dry-run 标记文件（`dryrun-<批次>.json`），同一条再预览就覆盖。
还没 dry-run 的条目这一项不查，`lint` 会在标准错误提示「dry-run 后再 lint 一次」；正式提交要求每条都 dry-run 过，
所以提交前那次自检一定查得到。改了 summary 结果就变了，要重新 dry-run（与改名同一规矩）。

**方向检查只查有把握的那几对**（2026-10-25 盘点全部战役结果 716 个文件、11,684 条后定的，清单在
`tools/extract_bench.DIRECTED_PAIRS`，每一对都必须在 `schema.EDGE_TYPE_MAP` 里）：上表四种反向命中逐条看全是写反
（`fixed_by` 16 条、`documented_in` 9 条、`derived_from` 1 条；`belongs_to` 没出现过，但「项目属于文件」不可能对）。
不查的：`caused_by`（Concept→Issue 的 25 条反向命中里多数方向是对的，只是把「停机」「提案撤回」这类结果标成了 Concept；
Issue→Fix「改动引起回归」与 Fix→Issue「因故障采取的操作」都是合理写法）、`derived_from` Concept↔Observation（反向命中都是方向对、类型松）、
`depends_on`（Config→Tool 读成「配置依赖执行它的工具」也说得通）、两端同类型的边（看不出方向），以及 Capsule / Observation
相关的几对（自抽取几乎不出这两类实体，没有数据判断）。要加新的一对，先用同样的盘点确认反向命中都是真写反。

## 复盘表的结构指标

v5 质量门与上面的硬错误只查对错与格式，不查**结构**。2026-10-25 起的一场 codex 8000 条战役事实文字准确，
但 78% 的观测恰好 2 个实体、每条只有 1.7–1.8 个不同端点对、第 3 条边起 92–94% 是 `relates_to`，
跨观测复用的实体出现只占 36–44%（同期 Claude 战役 75–85%）——正文点名的文件、提交、配置键没有成节点，
12 版补充约定都没看出来，只能靠人工统计。所以 `report` 每批、按模型、按模型 × 约定版本都出下面四列
（实现在 `utils/extraction_structure.py`，战役与 bench 共用）：

| 列 | 口径 |
|---|---|
| 恰2实体% | 非空条目（有实体）里，不同实体名（空白归一、忽略大小写）恰好 2 个的占比 |
| 端点对/条 | 每条非空条目里不同的有向 (source, target) 组合数（名字同上归一）的均值；标题、译文两条边也算 |
| 其余relates_to% | 第 3 条边起（前两条是照抄标题与译文）边名为 `relates_to` 的占比；分母是这些边的条数，没有其余边时显示「—」 |
| 跨观测复用% | 实体出现次数（一条观测里同名只算一次）中，该名字（忽略大小写）在**本模型本战役**内出现于 ≥2 条观测的占比 |

- 回收出去的条目不算在原批，与其他列同一口径；没交 / 空结果不进任何分母。
- 合计行的前三列由各批计数相加后重算（等价于按非空条数、按其余边数加权）。
- **跨观测复用% 的参照池按模型分开、不跨模型**，按领批顺序累加：每批行用截至该批（含）的参照池，后领的批不会改写前面的行，
  所以前几批天然偏低；按模型、按模型 × 约定版本两种合计行都用截至最后一批的参照池（同一名字在后面的批里又出现，
  前面那次出现也算复用）。所以合计行不是每批行的加权平均，两种口径不要直接相减。

「结构过薄」提醒对具体对象只认三种（保守，宁可漏报）：带斜杠且以字母开头的扩展名结尾的路径，或常见代码 / 配置扩展名的裸文件名
（`Node.js` 这类框架名排除）；7–40 位小写十六进制、同时含数字和 a–f 字母的提交 SHA（UUID 分段、纯数字不算）；
反引号里 2–80 个非空白字符的标识符。反引号里的路径与裸写同一路径、路径与它末尾的文件名只算一个。

## 结构门槛：整批结构过薄时拒绝提交（按战役启用）

提醒挡不住退化：同一场 8000 条战役登记结构约定 c13 后一度改善，扩到每模型 4 组并发后 gpt-6-sol 回潮
（066 批恰 2 实体 40%、071 批 56%、075 批 100% / 端点对 1.12），提醒照出、批次照样入库。所以结构指标可以升级为提交门槛
（`tools/campaign_structure_gate.py`）：**整批**非空条目的恰2实体% 超上限、或端点对/条低于下限、或（配置了的话）其余relates_to% 超上限，
`lint`（退出码 1）、`prepare`（停在 `stage=structure_gate`，不排 dry-run）、正式 `submit`（退出码 1，不进 NAS 闸门）都拒绝，并逐条列出偏薄的条目。
`submit --dry-run` 不拦（不写图）。门槛只**增加**拒绝条件：lint、v5 质量表、dry-run 绑定、命名分裂等原有判定一条不改。

| 阈值 | 缺省 | 说明 |
|---|---|---|
| `max_two_entity_pct` | 30 | 恰2实体% 超过就拒（等于放行），与 c13 批次观察目标同线 |
| `min_pairs_per_obs` | 2.5 | 端点对/条低于就拒；c13 观察目标是 3，门槛放宽到 2.5，只拦明显退回两节点模板的批次 |
| `max_relates_to_pct` | 不查 | 区分度有限（Claude 战役中位数也约 80%），缺省只在超过 90% 时提醒；要拦就显式配置 |
| `max_exempt_pct` | 20 | `structure_note` 例外最多占非空条目的这个比例（至少允许 1 条），超了整批拒 |

**启用只走公共命令，不手改 `campaign.json`**：

```bash
"$CLI" campaign structure-gate --id <战役> --enable --by <谁拍板> --reason <原因> [--from-chain cN] \
    [--max-two-entity-pct 30] [--min-pairs-per-obs 2.5] [--max-relates-to-pct N|off] [--max-exempt-pct 20]
"$CLI" campaign structure-gate --id <战役> --disable --by <谁> --reason <原因>
```

- 门槛只管 **chain ≥ `from_chain`** 的批次。`from_chain` 缺省是当前 chain，可以写下一版（先启用、再 `chain` 登记，新版起生效）；
  写早于当前的版本会把还没提交的旧 chain 批次也纳入，返回的 `open_batches_gated` 列出受影响的批次。没启用的战役行为不变。
- 已开时再 `--enable` 是改阈值：只改给出的项，`off` 表示不查这一项。每次启用、改阈值、关闭都在 `structure_gate_log` 追加一条
  （`at`、`action`、`by`、`os_user`、`via`、`reason`、当时的完整配置）；`status` 显示当前配置与最近 5 条记录。
- 执行者（带 `KG_HUB_CAMPAIGN_SLOT` 或 `KG_HUB_REWORK_BATCHES` 的会话）不能改门槛。
- **新战役**：`campaign start` 缺省按上表阈值从 c0 开启（记一条 `via=start` 的日志）；不要就写 `--no-structure-gate <原因>`（记 `declined`）。
- 正式提交通过门槛时，批次上记 `structure_gate`（版本、阈值、三项指标、计入 / 非空条数、例外 id），复盘可查。
- 在跑战役启用前先看 `migrate` 预览：预览绑定 `campaign.json` 摘要，启用后要重新预览。

**例外（c13 规则 3）**：原文确实只讲一个对象、或证据只支持两个实体的条目，审阅者在质量表该行写 `structure_note`（不是通用的 `note`——
c14 起每行都有 `note`），说明本条的证据限制，这条就移出分母、在报告里单独列出。`structure_note` 要：点名本条至少一个实体名；
去掉本条实体名与数字后仍有 ≥10 字的说明；同批两条以上去掉实体名与数字后相同的视为模板句，**全部不算**（呼应 c14 禁止整批统一置为通过）。
只有确实偏薄（恰 2 实体或端点对低于下限）的条目上的 `structure_note` 才看。`lint` 与 `prepare` 的预览读审阅中的质量表，
`prepare` 的判定与正式 `submit` 读 `quality-check` 通过后存的那份。不要为凑数编实体、拆同一对象或编关系。

## dry-run 的复用回显

`submit --dry-run` 的输出除了 `reuse_mismatch`（抽出名 → 会并到的不同名字，口径不变，仍是 ≤ 0.2/条那个指标）还有两项：

| 字段 | 内容 | 怎么用 |
|---|---|---|
| `reused` | 每个会复用旧节点的实体一行（**同名复用也列**）：`条目: 抽出名（类型）→ 被复用节点名（类型）［同批新建］［近名］［类型不同］「被复用节点 summary 开头」` | 同名也看 summary 是不是同一个仓库 / 同一件事：不同仓库的同名文件（2026-10-25 throttle 与 sd-server 的同名 Java 文件）要带仓库前缀改名 |
| `reuse_cross_type` | 复用到的节点类型和抽出类型不同的那几行 | 服务端精确同名不看类型（只有向量近名才查类型），这类误并只能在这里看见；逐条确认 |

来源是服务端 dry-run 响应新增的 `entities`：每个实体给 `reused`、`matched_name`、`matched_type`（节点标签去掉 `Entity`，
没有类型标签的旧节点为 null）、`match`（`exact` 精确同名 / `similar` 向量近名）、`matched_summary`（被复用节点 summary 前 100 字；
同批新建的没有）、`in_batch`。`submit` 把它按条记进 dry-run 标记文件的 `resolution`，同一条再预览就覆盖。
旧版服务端没有 `entities` 时退回用 `resolved` 拼（`matched_type` 为 null、不判跨类型），这一改动要先发服务端（提供方）再发客户端。

## 并行批次的命名同步与 NAS 闸门

2026-10-24 opus2000 战役两路并行时，同一事物在相邻批次起了不同名字，图里留下重复节点（人工发现约 12 组；
回放已提交批次又找到 4 起人工没发现的）；而每次 dry-run、正式提交都让 NAS CPU 温度短时上升 5–16°C。
要跑 4 路并行，这两件事都不能靠模型自觉，工具里做成了确定性的规则。

### 命名同步：`campaign namesync`

`tools/campaign_namesync.py` 从名字和观测正文算**锚点键**，同键不同名就统一（不判断「是不是同一个东西」）：

| 键 | 怎么算 | 处理 |
|---|---|---|
| file | 仓库 + 仓库内相对路径（名字前缀 `仓库:`、括号里的仓库名、正文绝对路径、正文「Files read/modified」里的相对路径；`recruitment-intel-service` 记为 `zhaopin`） | 自动改 |
| id | 名字主体是纯标识符（含 `_ . -` 或驼峰，末尾 `()` 不算差异），括号里没指明别的仓库，类型类别相同 | 自动改（类型不同只报告 `id-type`） |
| wt | 名字以「工作树 / worktree」结尾，按正文里的 `.claude/worktrees/<名>`、已知工作树目录名、其前缀或 hash 后缀对上 | 自动改 |
| core / pr / head | 同主体不同括号限定；同仓库同 PR 号的不同 Fix 名；「标识符 + 短后缀」对上全名标识符 | 只报告，执行者判断 |
| xrepo | 本批文件确定在仓库 A，服务端会复用的图中**裸**同名节点（无仓库前缀）在已提交记录里只属于仓库 B | 只报告，建议改成 `A:相对路径` |

另有 `server` 一栏：服务端会自己把这个名字并进图里的哪个节点（「服务端会合并到 X」）。判据直接调服务端的
`utils/extracted_ingest._lexically_same`（带实体类型，再核对图中节点的类型标签），不在工具里另抄规则——
以前只抄了「全路径与短名、只差 `()`」，`feature/course-benefit-freetimes（libtv）` 这种只比裸名多一个短括号、
服务端按包含占比（`CONTAIN_MIN_SHARE`）会并进裸名节点的写法就只被列成 review。`server` 只报告、不改名，也不影响退出码；
「并过去」不对（其实是不同的东西）时按规则 14 改名。向量召回这一步工具算不了，按「近名都召回得到」假定。

仓库从哪里认：`~/work-ai` 下的目录，加上 `~/workspace` 下带 `.git` 的目录（公司仓库 sd-server、throttle 等在那里；
`KG_HUB_NAMESYNC_ROOTS` 用路径分隔符给别的目录，给空串就不看）。观测正文的「Project: X」只是会话标签，
「Files read」里的相对路径只有在本机仓库 X 里**真有这个文件**时才算 X 的（opus2000 战役里标 fleet-ops 的会话读的多是
kg-hub、credvault 的文件）。`~`、`nas`、`vps` 这类位置不算仓库，不参与 xrepo。
xrepo 不自动加前缀：正文路径可能是暂存或部署副本（回放里 `credvault_dashboard.py` 的路径在 fleet-ops/scratchpad 下），自动改会把一个文件拆成两个节点。

规范名：图里已有同键节点时，取**挂得最多的干净节点名**（按各战役 `log.jsonl` 的服务端复用映射换算；不是绝对路径、不以任务号开头）；
写了 `仓库:` 前缀的名字不往没前缀的写法上改（裸名常是混了几个仓库的泛名节点，如 `pom.xml`），提交前也不算分裂；
服务端本来就会自己合并的写法不改、列进 `server`；图里没有时取本战役**更早的待提交批次**的写法
（并行批次之间按批次号先到先得，还没合并的分片也算）；都没有就本批内部统一成首次出现的写法。
参照物是本机状态目录下**全部战役**的已提交批次；不在任何战役里的旧图节点看不到。`--graph` 问图（只读，经 `/api/node_neighbors`，每类至多 20 次）：
「标识符 + 短后缀」名问图里有没有同名短节点；本批确定仓库 A 的裸文件名问图里同名节点的邻居，邻居的名字或 fact 提到别的仓库就在
`graph_hints` 里标 `xrepo`、给出 `suggest`（`A:相对路径`）——图里 `RedisCacheKeyConstants.java` 属 throttle 是旧图里的事，只能这样看到。

```
"$CLI" campaign namesync --id <战役> --batch <批次>                  # 只看报告
"$CLI" campaign namesync --id <战役> --batch <批次> --apply --graph  # lint 通过后、质量表之前：自动统一 auto 类
"$CLI" campaign namesync-replay --id <战役>                           # 回放各批会被抓到的同键异名与分裂（只读，统计用）
```

`--apply` 改写结果文件：实体名、边两端，以及第 3 条边起 fact 里整词出现的旧名（前两条照抄标题与译文不动）；改名后会成自环的边不改、列进 `skipped`。
输出里 `auto` 是改之前算出的计划，`applied` 是**实际写回的每一条**（`item / from / to / kind / source`），`residual` 是改完再查仍没统一的
（正常为空，不空时退出码 2）。以前 apply 后把 `auto` 换成残余、只留一个计数，022 批 `graphiti_core` 被改成「graphiti_core（Python 依赖包）」复查时看不出来。
改过就要重新 lint、重做质量表与 dry-run（旧的都已失效，工具会按内容摘要拒绝）。

**提交前的分裂判据**接在 `submit` 里（正式提交时自动查，不用另跑）：最近一次 dry-run 的 `new_entities` 里有这个名字（服务端会新建节点），
而同键在图里已有别的名字的节点 → 提交后图里会多一个同物节点，拒绝。确属不同事物用 `--allow-split "原因"` 放行，原因与被放行的行记进批次的 `split_exceptions`。
单模型 `agent_extract` 同一套：`"$CLI" agent namesync --batch <批次文件> --file <结果> [--apply]`，`submit` 同样拦分裂、同样经闸门。

### NAS 重操作闸门

`submit --dry-run` 与 `submit` 都先过 `tools/nas_gate.py`（命令内置，不用另外包）：

1. **全机一把锁**：同一时刻只有一个重操作在 NAS 上跑，其余排队；
2. **间隔**：距上一次重操作结束至少 60 秒（dry-run）/ 180 秒（submit）；
3. **测温**：连读 3 次 NAS CPU 温度取均值，≤90°C 才放行，否则每 120 秒复测。

配置在 `config/nas_gate.json`（SSH 目标、传感器路径、门槛、间隔、锁目录）；`KG_HUB_NAS_TEMP_SSH` / `KG_HUB_NAS_TEMP_SENSOR` / `KG_HUB_NAS_GATE_DIR`
覆盖单项，`KG_HUB_NAS_GATE_CONFIG` 换整份。锁目录缺省 `~/.kg-hub/campaign-tools`，与仓库外原来的 `nas_gate.py` 是同一把锁、同一个状态文件，过渡期混用也互斥。
`python -m tools.nas_gate --probe` 只读一次温度与上次重操作时间；`python -m tools.nas_gate --kind dryrun -- <命令>` 可以包别的重操作。
排队、等间隔、等降温合计最多等 1 小时；4 路并行时一次提交可能要排十几分钟，命令放后台跑、给足超时，不要中途杀掉重来。
每天约 02:00 家里路由器重启，那段时间读不到温度属正常，闸门会退出 75，过后重跑即可。

### `submit` / `namesync` 的退出码

| 退出码 | 含义 | 子代理怎么办 |
|---|---|---|
| 0 | 成功（namesync：没有要改的、按当前 dry-run 也不会分裂） | 继续下一步 |
| 1 | 原有拒绝：lint 没过、没 dry-run 或预览后改过结果、缺质量表、批次已提交 / 已回收；战役启用了结构门槛时整批结构过薄 | 按提示修 |
| 2 | 命名：namesync 还有没改掉的 auto，或 submit 判出会分裂 | `namesync --apply`（自环等改不掉的手动改）→ lint → 质量表 → dry-run → 再提交 |
| 3 | dry-run 标记太旧（旧版本预览没记 `new_entities`），判不了分裂 | 重新 `submit --dry-run` |
| 75 | NAS 闸门：连续 5 轮读不到温度，没提交 | 停下汇报（02:00 前后属正常，过后重跑） |
| 76 | NAS 闸门：排队、间隔、降温合计等了 1 小时还没放行，没提交 | 停下汇报，不要绕过闸门 |

## 提交前准备合成一条：`campaign prepare`

2026-10-24 opus2000 战役实测，每批成本 ≈ 27.7 万 token + 1.3 千 token × 工具调用数（r=0.72）；工具调用从 c1 的 48 次涨到
c10 的 113 次以上，多出来的几乎都是流程步骤：分别跑 lint、namesync check/apply、写脚本比对改名前后、quality-sheet → 手填 →
quality-check、整批 dry-run（常常 2 次）、再写脚本读 dry-run 输出。`prepare` 把这些串成一条命令，只把要人判断的列出来：

```bash
"$CLI" campaign prepare --id <战役> --batch <批次> [--sheet <质量表路径>] [--no-graph] [--fresh-dryrun]
```

| 顺序 | 做什么 | 停下的条件 |
|---|---|---|
| 1 | `lint`（离线） | 有问题 → `stage=lint` |
| 2 | 命名同步（与 `namesync --apply --graph` 同一套）：只写回 `auto`（锚点键确定的同物异名）；`review` / `server` 只列出；改过名再 lint | 有改不掉的 → `stage=namesync` |
| 3 | 质量表（v5）：缺省写到 `<战役目录>/reviews/<批次>.sheet.json`，为每条边与 summary 预填**引文候选**；表已存在且内容没变就不覆盖 | 没填完（缺审阅人、有行没下结论、候选没确认、引文空着）或有判 error 的行 → `stage=quality_sheet` |
| 4 | 表填完 → `quality-check`（通过后照旧存一份到 `reviews/<批次>.json` 供提交） | 不通过 → `stage=quality_check`，列 errors |
| 4½ | 战役启用了结构门槛：按通过的那份质量表判整批结构（第 1–2 步之后报告里已有按审阅中质量表的预览 `structure_gate.preview`） | 没过 → `stage=structure_gate`，`structure_gate.thin` 逐条列出 |
| 5 | 整批 dry-run，**每次 prepare 至多一次**，经 NAS 闸门；输入摘要没变且 dry-run 标记仍覆盖当前每条结果时直接复用上次预览 | 闸门拒绝（75/76）或有条目报错 → `stage=dryrun` |
| 6 | dry-run 之后再 lint（新建实体 summary）、按 `new_entities` 查命名分裂，列误并候选 | summary 过短 → `post_lint`；会分裂 → `splits`（退出码 2） |

报告（JSON）字段：`stage`、`ready`、`next`（下一步做什么）、`lint`、`namesync`（`applied` 实际改名、`review` 要判断的提示、`residual`、`graph_hints`）、
`quality_sheet`（路径、`status` 里的待确认项数、`candidates` 预填计数）、`quality_check`、`dryrun`（`cached`、`by_status`、`errors`）、
`merge_candidates`（`reuse_mismatch` / `reuse_cross_type` / `reused`：抽出名（类型）→ 复用目标名（类型）「summary 开头」）、`post_lint`、`splits`、
`structure_gate`（启用时：`ok`、三项指标、`violations`、`thin`、`exempt`、`invalid_exempt`）。
退出码：0 可以 `submit`；1 要人处理（看 `next`）；2 命名分裂；75 / 76 闸门没放行；73 / 74 同 `submit` 的回执状态。

**引文候选**（`utils/quality_candidates.py`）：确定性的句子匹配，不调模型。出处按本条正文 → lookahead → 同批其他条目找字词重合最多的原句
（照抄标题与标题译文两条边取标题行；`belongs_to` 只在本条正文里找、且句子要含目标名）。写进表之前用与 `quality-check` 同一套取证再核一遍，
保证是冻结输入里真实存在的子串；核不过就不填。候选带 `candidate` 键（`true` 是匹配所得，`previous_review` 是结果改过后从上一版表沿用的、
审阅者当时填的引文）。**候选不是通过**：verdict、四个核对标志、判空理由一律不预填；审阅者逐条核对，对就删掉 `candidate`，不对就改 quote。
`candidate` 键不影响 `quality-check` 的任何判定，`prepare` 自己见到还有没确认的候选就不往 quality-check / dry-run 走。

**预览复用**：每次整批 dry-run 成功（没有报错）后，`submit --dry-run` 把输出存到 `<战役目录>/previews/<批次>.json`，键是
（批次含 lookahead、结果、冻结规则、质量表、身份）的摘要——与提交回执的 `input_sha256` 同一口径。`prepare` 只在摘要一致**且**
dry-run 标记仍覆盖当前每条结果（`dryrun_state == ok`，与正式提交前查的同一绑定）时复用；任何一项变了就重新 dry-run。
正式 `submit` 的门禁不变（每条都要按当前内容预览过、另查命名分裂），输出里 `preview.reused` 说明是否沿用了这次预览。
预览不随图的变化失效（与原有 dry-run 标记一样）；隔了很久或别的批次刚入库很多，用 `--fresh-dryrun` 重跑。

推荐流程：**写完结果 → `prepare` → 处理报告（改 lint / 名字、审质量表、看误并候选）→ 再 `prepare` 直到 `stage=ready` → `submit`**。
单步命令（`lint`、`namesync`、`quality-sheet`、`quality-check`、`submit --dry-run`）行为不变，仍可单独用。

## 每模型并发组数、每组条数与总配额

三个参数相互独立：`--parallel` 是每模型并发组数（缺省1），`--batch` 是每组条数，
`--quota` 是每模型所有组合计配额。比如每模型并发4组、每组50条、每模型总共2000条：

```bash
"$CLI" campaign start --id <战役> --models <模型A> <模型B> --executor codex \
  --parallel 4 --batch 50 --quota 2000 --model-preflight <A探测回执> <B探测回执>
"$CLI" campaign supervise --id <战役> --model <模型A>
"$CLI" campaign supervise --id <战役> --model <模型B>
```

两个 supervise 分别在后台运行；每个池自动管理4个槽位，一共配置8个执行者。
每模型累计最多2000条（通常40批），所有模型合计4000条；实际执行组数受余量、可用观测和执行状态影响。
每模型最多同时处理200条；槽位完成一批后继续领取下一批，不要求4组齐步结束。
只说“总共2000条”时仍按所有模型合计，开局计划须写清分摊，不能当成每组或每模型2000条。

每个槽位有独立原生会话和 `supervisors/<模型>/slots/<序号>/` 回执。
同一槽位调用 `claim --model <模型> --slot <序号>` 会先返回其未完成批次；其他槽位不会接管它。
所有槽位仍通过同一战役锁推进游标、共用每模型配额，尾批只领剩余条数。原生子进程绑定战役、模型与槽位，
拒绝误领或提交其他槽位的批次。同一原生会话不能绑定多个槽位。

Codex `supervise` 默认相邻槽位错开300秒；`--stagger-seconds 0` 可同时派发。
抽取和本地校验并发，NAS预览、正式提交仍受原全机锁、间隔与温度闸门约束。
某槽位遇真实拒绝或终态不明时，池停止新的原生派发，已运行的原生回合按原流程结束，不杀进程。
单槽恢复用 `supervise --slot <序号>`，需要接管时提供该槽原会话的配对终态证据；不能借其他槽的成功掩盖本槽停滞。
状态页把 `parallel` / `configured_workers` 与持有原生 lease 的 `active_workers` 分开列出，
`worker_slots` 显示每槽运行状态和未完成批次，正式累计只认成功或已确认的empty。

旧 manifest 没有 `parallel` 时仍为1，原会话与回执路径不变。
新参数在开局登记；在途单路战役需要下述显式迁移，不能手改字段后重复启动原执行线。
`agents` 按每模型×槽位生成定义，`launch` 按槽位打印命令；其他执行器由其宿主按这些槽位派发。

### 在途 Codex 单路战役迁移

确认目标仍是原战役配额后，先让协调器停止续开原生回合，再通过公共入口冻结新领取：

```bash
"$CLI" campaign drain --id <战役> --reason <已获授权的迁移原因>
"$CLI" campaign migrate --id <战役> --preview --parallel 4 --proof <原执行线证据.json> --out <新预览.json>
"$CLI" campaign migrate --id <战役> --apply <新预览.json>
```

`drain` 只阻止领取，当前批仍可完成提交、随机复查与成本记录；不杀进程。
证据 JSON 的 `workers` 按模型列出 `session`、`runtime_receipt_dir`、`events`，
必须与战役 `handoff-status.json` 记录的原生会话、运行目录和 stdout 一致。
所有原执行线及操作必须无活动租约、终态明确；部分正式提交、未解决的 NAS 拒绝或未知终态会拒绝迁移。
至多一个尚未正式提交的原批次可保留，由同一个原会话在槽位1继续处理。

预览绑定当前 manifest、正式日志与原执行证据；任一变化须重新预览。
应用保留模型身份、规则、总配额、游标和原批次文件，原会话成为槽位1，其余槽位首次运行才创建会话。
应用回执和迁移前快照存于战役 `migrations/`；重复应用同一预览不会重复迁移。
应用完成后，每模型只启动一个 `campaign supervise` 池管理器。
`campaign migrate --id <战役> --rollback` 仅允许在所有新槽位尚未运行、尚未新领批之前撤回激活，
回到暂停领取的原单路状态；已经推进的战役必须对账处理，不能以回滚丢弃新历史。

冻结后用 `campaign resume --id <战役> --reason <已授权原因> --batches-per-model 1`
开启受控观察窗口：只允许各模型原槽位1各新领一批，共两模型两批，仍计入原配额。
旧未完成批次先续作，不计入新批预算；一模型领满即暂停新领，全部模型领满后自动冻结。
只启动 `supervise --slot 1`；窗口内槽位2及以上不会派发原生回合。
重启同一窗口不能重置已用预算，也不能在观察批未完成时扩大或清除预算。
完成两批及语义检查、获准继续后，`campaign resume --id <战役> --reason <验收结论> --all-remaining`
恢复原剩余配额，再启动各模型执行池。恢复前必须无活动租约、无未知操作，且各原会话有匹配的干净事件和零退出码回执。
`migration_drained` 是迁移和监督共同认可的正常收尾；真实拒绝、非零退出或未知终态仍会停止。

NAS75/76 后须先确认当前NAS可用并取得恢复授权，再对原批运行 `reconcile-operation`。
部分正式提交后发生上传失败时，不能只按来源键存在性清理回执，也不能整批重交。
协调器可用 `reconcile-upload --id <战役> --batch <批次>` 核验一个严格限定的情况：
原公共命令在发送单个完整 JSON 正文的 `sendall` 阶段超时，先前 HTTP200 回执是原结果的连续前缀，
与冻结输入、服务端 UUID 和来源状态吻合，NAS 只读执行账本证明该前缀已结束，接收备份证明
其后未接受本批其他请求。读取响应时超时、来源已存在、账本未终止/缺项、输入变化一律拒绝。
工具保留原失败及已写入标记，产生 `reconciled_partial`；新 `submit` 仍做整批质量与预览检查，
仅发送未接收的后缀，不重复写前缀，也不再追加其正式日志。NAS 温控、间隔和等待上限不变。

原 native 终态还需 `reconcile-worker --id <战役> --model <模型> --slot <槽位> --reason <人工恢复原因>`。
它仅接受上述已核清的部分上传，或精确的原模型容量拒绝；容量拒绝另须 `--model-preflight <新探针回执>`，
核对相同模型、effort、proxy 和新鲜真实 canary，并要求原在途操作已完整对账。
回执绑定原槽位、session、native、事件及操作哈希；原退出码不改，真实审批/权限拒绝不可这样恢复。
下面的独立所有权恢复协议仅接受已终止、未写入的跨槽 `campaign review` 误用，不接受其他权限拒绝。
两项通过后使用 `resume --all-remaining` 和原 `supervise`。这些命令不做模型补评/补测，不重抽旧结果。
只有完整源键回读确认未写入、对账仍绑定原操作attempt/身份/输入时，`resume` 才认可已解决的那次拒绝；
保留原76和原生事件，不改写历史。未对账、已有写入、活动或未知终态仍拒绝恢复。

## 领批：凑满一批再交

### 协调器显式纠正跨槽评审的执行范围

`reconcile-ownership` 不执行被拒绝的评审、不移交批次、不把非零退出改成成功。
它仅适用于：已提交来源的协助会话错误调用另一槽位批次的 `campaign review --kind targeted`，
命令唯一失败为 exit1 与原样 `batch belongs to a different worker slot`，所有命令均有完整终态；
协调器已在无在途命令时中断该调用，原 native exit1、PID/session/时间及中断凭据匹配。
所有源批次须已有 submitted/released 终态、全机原战役 lease 均闲置。
原调用中的公共写入只允许已具备 apply/status verified 回执且来源/episode/请求完全匹配的同源修复。
其他公共变更、真实审批/NAS/提供商拒绝、未知写入、活动或缺失终态均拒绝。
原生记录里的 `agent quality-check` 可将stdout写到本战役audit目录内的绝对JSON路径；
只接受单个尾部 `>`，并绑定成功JSON回执摘要。追加/输入/目录外/相对重定向、命令链、
反引号、变量/命令展开或换行不接受，写入命令也不能用重定向获得此例外。

```bash
"$CLI" campaign reconcile-ownership --id <原战役> --model <原模型> --slot <错误调用的原槽位> \
  --preview --events <原调用stdout.jsonl> --exit-receipt <同目录exit-status.json> \
  --stop-receipt <协调器原中断回执> --reason <原范围纠正原因> --out <新私有预览.json>
# 协调器读预览中的每条完整命令及原输出、核实已完成修复，不存在隐藏/未知写入；
# 确认原批次仍归原owner，原跨槽评审不重放。预览含私有材料，不提交到公开仓库。
"$CLI" campaign reconcile-ownership --id <原战役> --apply <新私有预览.json> \
  --confirm-plan-sha <实际预览sha256字段> --reviewer <实际协调器> --reason <明确授权与核验结论>
```

apply 在锁内重新核对全部输入及冻结配置，保存不可变预览/原失败事件摘要、原worker快照和批准凭据，
将实际失败事件的原样副本登记到该模型目录。原 native exit1 和拒绝文件始终不改。
新凭据仅允许原 session 在自己的槽位继续，原批次的逐条返工仍由正确owner完成。
它不解除 `assert_worker_batch`，不自动派发模型、不恢复新领取；质量返工未闭环时继续 draining。
完成必要返工后按原 `resume --all-remaining`/`supervise` 流程恢复原剩余配额。
批准后任何证据、原来源正式日志、配额/批量/并发/session配置或 native attempt 改变都会使凭据失效；
新原生调用开始后不再复用旧批准。此协议不增加模型、测试或新来源预算。

### 冻结新领取期间由原owner返工

单路战役也使用 `--slot 1`：入口沿用原无槽位监督回执与 `worker.json` 中的唯一原会话，
不会迁移战役或增加 `slot_sessions`。子进程仍绑定原模型与返工批次名单；失败原生调用、
重复会话、活动 lease 和名单外来源同样拒绝。返工结束后仍需协调器验收，再恢复原配额。

若返工子进程在 Codex 本地 app-server 初始化前因系统权限失败，先检查完整退出、空原生事件
及 stderr。修正宿主启动权限后，可显式加 `--retry-startup` 重试同一任务：仅接受精确初始化
错误、原成功会话锚点和未变的说明/批次；失败回执另存而不改写。出现任何原生事件、真实审批/
provider/NAS 拒绝、未知写入或上次重试也失败时均不适用；不能用该选项反复启动或解除沙箱。

不能先全额resume再补质量欠账。协调器使用独立 `rework` 入口，仍由原模型、原槽位和原session
完成自己已提交的批次，不把批次交给协助槽。普通supervise在draining时不新开模型回合。

```bash
"$CLI" campaign rework --id <原战役> --model <原模型> --slot <原owner槽位> \
  --batches <自己已提交批次...> --assignment <已核对的私有返工说明> --reason <明确授权原因>
# 原native调用在外部目录留回执时，须加同一调用的配对证据：
# --adopt-events <原stdout.jsonl> --adopt-exit <同目录exit-status.json>
```

要求原native真实exit0、thread.started/session与原绑定相同、turn.completed、没有真实审批/NAS拒绝
或失败公共操作；只读诊断失败不自动推导为写入失败。若原owner正常终止时只报告另一槽的
business_permission_denied，还须关联上面已显式批准的那个peer所有权恢复凭据，且自己的原生
事件中没有失败公共调用。自己的真实拒绝不能借另一个槽的凭据清除。

原生产会话以 `quality_process_violation` 主动停报时，可由协调器对其自己已提交的
批次调用同一 `rework` 入口。仅接受原 native exit0、唯一原 session、完整命令终态，
以及零新增提交/预览的明确报告；原事件只可包含本战役状态读取、规则读取和私有审计
文件保存，任何失败命令、网络/子进程代码、生产写入、跨范围身份、动态执行或审计目录外
写入都拒绝。入口将原事件、原 native、原报告及本地证据摘要另存至本轮私有 job 的
`quality-stop-continuation.json`，不会把旧报告改成正常结束，也不改变普通生产监督器的
`normal_recovery` 判据。此许可只进入保持 draining 的逐来源返工；全部质量验收完成前
仍不能恢复配额。它不接受用另一调用的 adoption 文件替代原自报事件。

入口持有原supervisor租约，核服务preflight并用原native参数/自动审批/工作区sandbox启动一次原session。
不增加canary、评测、来源或配额。说明正文先冻结在私有job目录，证据和实际提示绑定同一份字节。
子进程限定 `KG_HUB_REWORK_BATCHES`：只能对名单内自己的来源做正规修复/targeted评审；
campaign及standalone agent均禁止新领取/提交，不能覆盖原批次结果与质量表或增加random分母。
本轮有效候选、逐源决定及质量凭据另存私有audit目录，原错误、旧表和成本未知项保留。
语义判断和每个复用候选须逐条核实，禁止批量默认true/ok；合并只能复制已经明确保存的逐条判断。
命令返回rework_turn_completed只证明这次原生调用正常结束，不代表该批或全战役语义验收。
协调器核实完整返工、所有真实修复回读、固定原随机记录和费用后，才另行恢复剩余配额。

`claim`（以及 `agent_extract next`）从游标往后逐段扫（每段 `max(还差条数×4, 40)` 条，最多 8 段，
`tools/agent_extract.FILL_MAX_ROUNDS`），跳过质量闸拒掉的、已入库的、别人正领着的，凑满一批再交。
积压稀疏时一段凑不满（2026-10-23 战役第 1 轮：游标后 80 条只剩 4 条可抽），以前会交出不满额的一批、子代理领完就收工。
8 段都凑不满才交不满额的批次（或提示游标已前移、再领一次）；批次 `stats.rounds` 记扫了几段。

## 抽取会话不产生新观测：战役一律在专用目录里跑

2026-10-24 实测：跑战役的 codex 会话 2 小时里产生了 214 条 claude-mem 观测（同期它入库 293 条）——领批、读观测、抽取、提交的每一步
都被记成了新观测、又进了待抽队列，看板「剩余」几乎不降。所以：

- **先建专用目录**：`"$CLI" campaign workspace --repo <已核实Git仓库>` 建（或同步）`~/work-ai/kg-hub-campaign`（kg-hub 主干的独立 worktree，
  分离 HEAD，不在里面改代码；`KG_HUB_CAMPAIGN_WORKSPACE` 可改位置），并把它追加进 claude-mem 的 `CLAUDE_MEM_EXCLUDED_PROJECTS`
  （先备份 `~/.claude-mem/settings.json`，原有规则保留）。claude-mem 的钩子与记录服务两侧都按会话目录排除，对所有工具都生效。
- **编排者与子代理都在专用目录里打开会话**——只在命令里 `cd` 进去不算，钩子按会话目录判断。
  从别的目录（例如工具为会话自动建的 worktree）触发战役时，在那个会话里只出计划、等确认，确认后到专用目录直接开新会话跑，
  不给触发会话的目录追加排除、也不加 `--allow-tracking`（2026-10-24 用户约定：战役固定在专用目录，不每次新加目录加白）。
- **领批前自检**：`start`、`claim` 先查会话目录是否已排除（或设了 `CLAUDE_MEM_INTERNAL=1`），没有就拒绝；确需例外用
  `--allow-tracking "原因"`，会记进战役的 `tracking_exceptions`。
- `launch` 生成的命令都在真实采集已排除的专用目录里执行，不自动设置 INTERNAL 绕过；qodercli 加 `--setting-sources local`，codex 加 `--ignore-user-config`。
- 别的设备（NAS 上的 DSH、Windows）各有自己的 claude-mem 设置，要在那台设备上同样跑一次 `workspace`（或手工加排除规则）。

## 空观测也记录：记为「已处理（空）」

执行者判为「没有可抽取知识」的观测，正式提交时发一条 `empty: true` 的记录：服务端只记 IngestedKey `status=empty`（带判定的模型、
执行工具、run_id），不写任何图数据；refinery 对账把它记进水位的 `rejected`，看板「剩余」随之下降，断路器恢复后网关也不会再抽一遍。
领批查重把 `empty` 与 `ok` 一样当作已处理。早先只记在本地日志里的空观测用 `"$CLI" campaign mark-empty --id <战役>`
（普通执行器的状态目录用 `"$CLI" agent mark-empty`）补记，可重跑。普通领批不会再抽正式记空的来源；若复核发现漏抽，使用正规修复入口将 empty 升级为非空，并保留原来源键和审计，不把它当成新增样本。

## 同一台设备上多个工具、多个战役同时跑

### Codex 持久监督与提交回执

新战役每个模型用公共入口启动一个监督器；命令作为长任务后台运行，原生抽取仍使用冻结模型、effort、代理和原 brief：

```bash
"$CLI" campaign supervise --id <战役> --model <模型>
```

已有原生会话必须交接其本次调用的唯一退出回执和 JSONL 事件，不中断仍在工作的会话：

```bash
"$CLI" campaign supervise --id <战役> --model <模型> --resume-session <原session> \
  --adopt-exit <本次调用exit-status.json> --adopt-events <本次调用stdout.jsonl>
```

退出回执须含 `exit_code=0`、`ended_at`，事件须有相同 `thread.started` 身份及 `turn.completed`。
监督器在旧调用真正结束前只等待，不另起 worker；没有历史调用终态凭据不能接管已有模型历史。
以后每次原生调用由 `runtime exec --receipt-dir` 保存私有运行和退出回执，并用独占 lease 防止重复监督。
可选诊断不可用或正常 turn 提前结束后，在同一 session 续跑到正式配额；默认每15秒检查，连续3个无正式进展的 turn 留待协调器，避免无界消耗。
真实审批拒绝、NAS75/76、业务权限拒绝、未知提交终态不能自动重试。监督器不替模型抽取/审核，不自动提交、不发信号、不改变安全审批。

提交和预览命令自动记录 `operations/<批次>/`，包含实际冻结输入摘要、来源键、等待/提交阶段与退出结果；另一同批请求或回收不能越过活跃 lease。
诊断优先读取以下公共命令，不依赖 `ps`、`pgrep` 或向进程发信号：

```bash
"$CLI" campaign operation-status --id <战役> --batch <批次>
"$CLI" campaign status --id <战役>
"$CLI" runtime status --receipt-dir <原生调用回执目录>
```

退出73表示原操作仍活跃，应等待；74表示终态待核对。持锁只证明进程存活，CLI退出0也不代表所有条目成功。
没有终态或HTTP结果不明时，协调器先用 `campaign reconcile-operation --id <战役> --batch <批次>` 查询全部原来源键。
只有回执证明未启动写请求（或只是只读预览），且完整查询确认全部不存在，才解除重放阻止，之后仍须逐条审核和整批预览。
已发出写请求时，键不存在也不能证明远端请求已结束，仍需协调器核对远端真实终态；任一来源已存在/进行中或服务核验响应缺项时均保持待核对，不能算成本模型成功。
旧未受管提交没有 lease 回执时仍需宿主只读终态证据，不凭锁不存在猜测可以重交。退出75/76和真实平台拒绝保留原有处理规则。
旧版调用明确退出76且未获NAS放行时，协调器可用以下兼容入口登记原始证据：

```bash
"$CLI" campaign reconcile-operation --id <战役> --batch <原批次> --resume-session <原session> \
  --legacy-exit <唯一调用前缀-exit-status.json> --legacy-events <同前缀-stdout.jsonl>
```

入口核对同一次原生调用的身份与终态、最后一条精确公共提交命令的76退出、对应控制台的“未放行”终态及冻结批次，
并重新完整查询全部原来源键。任何已存在、缺项、活动lease、未知终态或非闸门拒绝都不解除阻塞。
只在全部通过后保存不可变的旧证据摘要和 `reconciled_not_written` 回执；保留原76，不自动提交。
监督器只接受与该原生事件文件及命令编号绑定的恢复凭据，然后仍按原温度门、完整审核和整批预览恢复同一session。
`campaign status.supervisors` 显示运行阶段及 `coordinator_required`；协调器须及时处理该状态，不因会话显示 idle/running 就推断业务进度。

- **同一个战役**：领批、提交、记成本都在战役目录的文件锁里，多个工具（Claude Code、qoder、codex、cursor、DSH）同时跑不会领到同一条。
- **不同战役之间、战役与 `agent_extract next` 之间**：走**全设备在途领取登记**（`tools/claim_registry.py`，
  默认 `~/.kg-hub/claims/`，不随 `KG_HUB_AGENT_EXTRACT_DIR` 变）。领批时剔除别人正领着的观测，领到的登记到自己名下
  （每个战役一个领取方，每个 next 状态目录一个）；正式提交后入库的注销，没入库的留着等回收重领；登记 12 小时
  （`KG_HUB_CLAIM_TTL_HOURS`）没动静自动失效。被跳过的观测领取方的游标会越过去，若占着的一方最后没入库，
  它们留在积压里，不会丢。
- **同一状态目录里多个工具同时 `agent_extract next`**：游标与批次号在登记锁里读，领取方按批（`next:<状态目录>:<批次号>`），
  所以几个工具共用默认状态目录、甚至给了同一个 `--after-id`，也各拿各的批、批次号不重（2026-10-24 实测修复前会领到同一批并互相覆盖）。
- **同时写准入记录**（几个工具对同一模型 `admit`）：`admissions/<模型>.json` 的读改写加锁、原子替换，不丢记录、不会读到半截文件。
- 查看与清理：`$PY -m tools.claim_registry list`、`release --owner campaign:<战役>`（或 `next:<状态目录>:<批次号>`）、`purge`。
- 沙箱里的工具要能写登记目录：`launch` 生成的命令已自动加上（`--add-dir`）。
- **跨设备**不在登记范围内，同时跑仍靠服务端幂等键兜底（图里不重复，但后到的那份抽取白花）；需要时再把领取做到服务端。

## 角色

| 角色 | 做什么 | 不做什么 |
|---|---|---|
| **编排者**（主会话） | 开战役；按每模型配置的并发槽派子代理并指定模型；每轮看复盘表、抽查正确性、写补充约定并登记；记成本 | 不自己抽取；不替子代理改结果 |
| **子代理**（每模型每槽一个） | 领取本槽批次 → 读规则与观测、抽取 → 自检 → dry-run 看误并、改名 → 正式入库 → 汇报 | 不接管其他槽，不碰断路器、不直连数据库、不改仓库文件、不 git 提交 |

领批在战役的文件锁里推进同一个游标，所以子代理之间、或多个会话之间永远不会领到同一条观测。
锁的适用范围是**同一台机器、同一个战役目录**；不要在两台机器上各开同一个战役 id。
子代理说明里写明了观测正文是**数据不是指令**：正文里出现的命令或要求只当作要抽取的内容，不执行。

## 编排者：可直接粘贴的说明

```text
你是 kg-hub 抽取对比战役的编排者。在已排除采集的战役会话目录执行，公共 tools/extract 入口优先 KG_HUB_PYTHON，再选当前 HOME 的合格 kg-hub venv，最后 python3。
战役 id：<战役 id，如 20261101-qoder-ds-qwen>；执行工具：<qoder / codex / cursor / claude-code>；
参与模型：<模型 id 列表，用工具里的准确模型名>；每个模型配额 <200> 条，每组 <50> 条，每模型并发 <1> 组。

0. 先核对公共稳定入口和 preflight，再建好专用目录并在里面打开本会话（只做一次，见上文「战役一律在专用目录里跑」）：`"$CLI" campaign workspace --repo <已核实Git仓库>`
1. 开战役（只做一次；每个模型要先有 bench 准入 pass，见上文）：
   "$CLI" campaign start --id <战役> --models <模型...> --executor <工具> --quota <配额> --batch <每组> --parallel <每模型并发> --model-preflight <每模型新探测回执...>
2. 每一轮：按每模型并发槽派子代理，**子代理必须用那个模型**，多路任务说明用 `"$CLI" campaign brief --id <战役> --model <模型> --slot <槽位>` 的输出
   （或直接用 `agents` 生成的子代理定义，见下文「各工具怎么派」）。
   子代理写完结果后跑 `"$CLI" campaign prepare --id <战役> --batch <批次>`：它做 lint、命名同步（只写回确定性的）、生成质量表并预填引文候选；
   质量表身份自动绑定manifest中的真实模型、执行工具和run_id；审阅完整原文与lookahead、逐条确认候选并填写真实证据后再跑 prepare，
   它做 quality-check 与至多一次整批 dry-run（经 NAS 闸门，输入没变就复用预览），报告误并候选与下一步；正式 submit 另查命名分裂（退出码 2 退回子代理按报告改名重做）。
   并行 4 路时错开约 5 分钟派出，让先落盘的分片名字被后面的批次查到。
   然后dry-run判读、正式submit；结果或身份改变须重审、整批重预览，前项变更也影响后项pending实体上下文。子代理并行跑；全部回来后：
   "$CLI" campaign report --id <战役>
   看每批：入库数、错误、预览误并候选、照抄标题%、译文边%、边名越界、悬空边、零边、credits/条、秒/条。
   再从每个模型本批结果里各抽 3 条，对照观测原文看事实方向、数字、因果有没有写错（评分表看不出这类错误），逐条记下：
   "$CLI" campaign review --id <战役> --batch <批次> --obs <观测 id> --verdict ok|error --kind random --note "<错在哪>"
   复盘表按 random / targeted 分列样本及错误；专项用 --kind targeted，不能和随机分母混算。
   旧无kind记录单列legacy_reviewed / legacy_review_errors（旧记录未分类/旧记录错误），不计入random_*；reviewed仍是全部评审总数。
   系统性归属错误要全量核对belongs_to，高空率要阅读全文复核空项；发现未修问题如实保留。
3. 有新教训时把本轮发现写成补充约定；没有就记沿用。补充约定是累加的，新文件只写本轮新增（只写可执行的规则，附一句理由），存成文件后登记：
   "$CLI" campaign chain --id <战役> --file <补充约定文件>
   之后领的批次自动带新版本，复盘表按「模型 × 约定版本」分组，改链路前后可以直接比。
4. 成本分两本账：子代理汇报里的 credits / tokens / seconds（拿不到是 null）记进它那一批，复盘表的每条成本只含子代理：
   "$CLI" campaign cost --id <战役> --batch <批次> --credits <N> --tokens <N> --seconds <N>
   编排者自己的开销单独记，复盘表另列合计、不摊进任何模型：
   "$CLI" campaign cost --id <战役> --overhead --credits <N> --note "<这一轮做了什么>"
   （qoder 编排时，`qodercli -o json` 的 `modelUsage` 按模型分开给出 credits，可直接拆成各子代理与编排者两份。）
5. 子代理失败：能接着做就用同一批次号续跑；做坏了就 `release --reason`，条目回到队列。
6. 重复 2–5，直到所有模型核对最新终态达到配额且 claim 返回 done。最后给出：各模型 × 约定版本的产出画像表、
   每轮改了什么、改完指标怎么变（同一模型、不同约定版本之间比）、每条成本。
   注意：不同模型抽的是不同观测，模型之间的高下以 bench 准入记录为准，不要从本表下因果结论。
```

## 子代理说明：由命令生成，不手抄

说明模板只有一份，在 `tools/extract_campaign.py` 的 `SUBAGENT_BRIEF`。按战役与模型填好：

```
"$CLI" campaign brief  --id <战役> --model <模型 id>      # 打印一份，粘给子代理或某个会话
"$CLI" campaign agents --id <战役>                        # 每个模型一个子代理定义（JSON，给 qodercli --agents）
"$CLI" campaign agents --id <战役> --format md --dir <目录> # 写成带 frontmatter 的定义文件（含 model 字段）
```

子代理的流程：读 `rules` → `claim` 领一批 → 每 10 条一组读全文抽取（起草时就按一句一事实写短句）→ 合并成结果文件 →
`prepare`（lint、命名同步、质量表 + 引文候选，见上文「提交前准备合成一条」）→ 按报告的 `next` 处理：审质量表（逐条核对候选引文、
归属/完整句/译文/覆盖及判空）、改名 → 再 `prepare`（quality-check + 整批 dry-run，看误并候选）直到 `stage=ready` → `submit` 入库
（沿用 prepare 的预览；自动查命名分裂；dry-run 与提交都经 NAS 闸门，退出码见上文「`submit` / `namesync` 的退出码」与 prepare 一节）。
被权限、hook 或分类器拦下时立即停下汇报，不换写法绕过。
若修改结果或身份，重新生成并审阅质量表、重新核验和整批预览。汇报包括实际终态、未修问题与成本覆盖。红线同 [_common.md](_common.md)。

## 各工具怎么派「指定模型的子代理」

| 工具 | 方式二（一个编排会话派多个模型的子代理） | 证据（2026-10-22 实测） |
|---|---|---|
| **Claude Code** | ✅ 子代理工具带 `model` 参数（opus / sonnet / haiku） | 10-22 三模型 2000 条实验就是这样跑的 |
| **qoder** | ✅ 子代理定义的 `model` 字段生效。命令行：`qodercli -m <编排者模型> --agents "$("$CLI" campaign agents --id <战役>)"`；界面：`agents --format md --dir <仓库>/.qoder/agents` | 主会话 Qwen3.8-Max，子代理分别指定 DeepSeek-Flash / Kimi-K3，输出 `modelUsage` 的内部模型名随之变为 `dfmodel` / `kmodel_latest`，credits 按模型分开记 |
| **cursor** | ✅ 子代理定义的 `model` 字段生效，派子代理的工具本身也带 `model` 参数。`agents --format md --dir <仓库>/.cursor/agents` 生成定义，编排者在 cursor 里按名字调用 | 主会话 gpt-5.2，`taskToolCall` 参数里是 `model: gemini-3.7-flash-high`，子代理回复自报 Gemini 3.7 Flash |
| **codex** | ⚠️ 未确认：派生工具自报有 `model` 参数，但 `codex exec --json` 的事件流里两次都没有派生记录（只有一次空的 `wait`），不能确认子代理真按指定模型跑 | 先用退路 |
| **DSH**（DeepSeek Harness） | ❓ 待验证：DSH 有子代理插件（`subagent` / `subagent_fork`），能否给子代理单独指定模型要在 DSH 里实测（让编排者派一个指定了模型的子代理，看 DSH 的用量记录按哪个模型计费） | 先用退路：`launch --tool dsh`，每个模型的会话开跑前在 DSH 模型设置里切好模型 |

做不到方式二（或想并行开多个会话）时，用**每模型每槽一个会话**，仍是同一个战役：

```
"$CLI" campaign launch --id <战役> --tool codex|cursor|qodercli|dsh [--model <模型>...]
```

打印每个模型的启动命令：子代理说明由 `brief` 现场生成；都把战役状态目录加成可写（`--add-dir`）、放开网络
（子代理要连 NAS 领批、连 kg-hub 提交）；codex 还要关掉标准输入（`< /dev/null`），否则会一直等输入。
启动参数由公共工具生成；第一次在新设备/新执行器使用时先预检并完成1批，不能用别的设备成功替代本机验证。

注意：cursor 与 codex 会加载用户级设置里的钩子（实测 cursor 会话开头被注入了一行 kg-hub 上下文）；
公共入口不会改变会话目录；必须仍使用排除的会话目录或已核验的internal模式，不能把加载新代码误当采集已排除。

## 和其他用法的关系

- **冒烟**（新模型进场前，不入库）：`tools/extract_bench.py` 在冻结样本上离线评分，见 [qoder.md](qoder.md) 的「对比模式」。
- **运行器**（`extract_bench run --executor qodercli|codex|claude`）：把 qoder、codex 或 Claude Code 命令行当纯模型通道、无人值守地比模型本身；不入库，用于冒烟与准入。
- **战役**（本文）：多模型生产画像，入库，每批复盘、改链路；模型能不能用、谁更好看 bench 准入记录。

## NAS queue and recoverable infrastructure waits

已完成返工因成本备注保护被拒绝时，只能按[成本拒绝交接协议](rework-completion.md)审阅冻结证据并显式登记；该入口不开放成本权限、不重试被拒操作、不自动验收质量或恢复领取。

NAS-heavy operations reserve process-leased FIFO tickets before acquiring the existing machine-wide lock. Only the oldest live ticket may contend for that lock. Abandoned tickets are removed only after their file lease is provably released; PIDs, age or stale timestamps never authorize evicting a live owner. All clients must load the same released gate to guarantee FIFO fairness; legacy clients remain mutually exclusive but cannot participate in ordering.

`max_wait_s` remains3600. At the queue head it bounds lock acquisition, the original60/180-second gaps and three-sample temperature admission at90C. Behind other tickets it bounds lack of progress: only completed HTTP responses from the head or a forward queue transition refresh that budget. Polling and timer heartbeats do not. Eight productive batches may therefore queue longer than an hour without being falsely labeled a NAS failure, while a stalled owner still refuses with76. Temperature read failures still refuse with75. Thermal admission is never weakened.

Pre-extracted HTTP writes and previews use the existing `utils.ingest_budget.ingest_ceiling_sec()` plus30seconds transport margin, instead of the generic120seconds. Other requests retain120seconds. A timeout still means unknown remote state and does not authorize retry, source-key changes or promoted success. This prevents premature client abandonment; it cannot retrospectively prove the outcome of an older timed-out request.

Only the exact native `Selected model is at capacity. Please try a different model.` refusal, a clean native exit1 and `turn.failed` with the same original session qualify for capacity recovery. Supervision waits300seconds and resumes the same session up to three times, without new claims or model probes. Unknown writes, approval denials, NAS refusals and other provider errors still stop. Capacity refusals do not spend the no-formal-progress budget; persistent capacity exhaustion remains an explicit infrastructure blocker. The retry counter is persisted with the worker.


## Persistent recovery and source decisions

Existing parallel Codex campaigns may opt into the recovery manager with
`campaign recovery-enable --id <campaign> --reason <coordinator-reason>`.
It freezes the original quota, model/run identities, runtime settings and slot
count. `recovery-plan` reads current leases and stop dispositions; it does not
clear a refusal. `recovery-watch --once` executes eligible existing-owner actions
and records immutable event requests and results. Use `recovery-service --out
<private-plist>` to generate a Mac StartInterval service (45 seconds, stable
public entry). Installation uses the fleet-ops `launchd_reload` procedure after
normal publication review. A generated plist is not an installed or verified
service. The service uses the discovered native CLI directory, not an assumed
interactive-shell PATH. Verify launchd's actual job, loaded code and subsequent
observer receipts after installation and after login/reboot.

Register already authorized submitted-source rework with `recovery-register
--model <model> --slot <slot> --batches <own-batches> --assignment <frozen-file>
--checkpoint <campaign-audit-checkpoint> --reason <reason>`. The checkpoint must
contain disjoint `reviewed` and `pending` integer source-ID arrays covering the
exact registered batches, an explicit `in_flight` value, and `repairs_verified`.
A clean incomplete turn can then continue the same original session. A released
batch is not submitted-source rework. Unknown writes, actual approvals, NAS
refusals and unmatched terminal states retain their original public recovery
requirements. A clean exit, increasing review count or all IDs reviewed does
not constitute semantic acceptance.

After source-level semantic acceptance and all other known blockers close, the
coordinator may register `recovery-approve-resume --evidence <audit-files...>
--reason <acceptance-reason>`. This approval binds exact evidence bytes and one
drained dispatch generation. Pending registered rework blocks the request;
the existing public resume protocol still checks every live owner and uncertain
operation. After a successful resume, eligible original slots are supervised.
The manager never invents semantic acceptance or clears an incident by changing
its report. A new drain invalidates the old generation's approval. Repeated
observer launches share the same event lease; a completed event is not executed
again, and a real public refusal is retained as a blocked event. Lack of rework
progress is bounded separately from production's formal-source counter.

With recovery enabled, exhausted model-capacity or no-formal-progress failures
remain local to their slot. Approval, quality-process, unknown-write and NAS
failures retain conservative shared-stop behavior. Other slots continuing does
not make a blocked pool successful. `recovery-disable --reason <reason>` stops
new manager actions and leaves live native and service operations intact. For
rollback, drain new claims, disable/remove this observer, wait for existing
owners and verify operation terminals before switching code. Do not resume old
code on batches requiring a newer admission protocol.

For future claims only, `decisions-enable --reason <reason>` while drained
activates `source-decisions-v1`. Existing batches retain their frozen contract.
Each source reviewer submits one explicit review row with `campaign decision
--batch <batch> --file <single-row-json> --reviewer <same-reviewer-as-sheet>`.
The row must contain its ID/current context digest, an explicit verdict, all
four explicit boolean checks, its own nonempty note and evidence. Lists/full
sheets/omitted checks are rejected; no defaults are supplied. Final quality
sheets can only merge the exact recorded rows. Both quality-check and direct
preview/submission check the ledger, so bulk editing a sheet cannot bypass it.
Source/lookahead/rule/candidate changes require current review. All revisions
remain addressable. These checks establish evidence and process integrity;
they cannot detect every scripted lie or establish semantic truth.
