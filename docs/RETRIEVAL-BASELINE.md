# 召回基线(北极星③ · 2026-07-23)

> "先度量再建设"。黄金查询集 `docs/golden-queries.json`(15 条,S1/S2/S3,分 literal/nl)+ 评测器 `tools/eval_recall.py`。ground-truth 用内容级 `gold_kw` 定义相关集,独立于被测端点。

## 基线数字(k=10)

| 端点 | 命中率 | 平均 recall@10 | literal 命中 | nl 命中 |
|---|---|---|---|---|
| `/api/search` 子串(旧默认) | 3/15 | 5% | 3/8 | 0/7 |
| `/api/search_semantic` 向量 | 13/15 | 25% | 8/8 | 5/7 |
| **`/api/search` hybrid(现默认,2026-07-23 上线)** | **12/15** | 21% | **8/8** | 4/7 |
| hybrid @20 | **14/15** | 39% | 8/8 | 6/7 |

> hybrid = 语义主序 + 子串精确提权,多因子排序(external −0.12 / time-bound −0.08 / verified +0.08 / usage 有界),+ facet 过滤(project/kind/durability)。@10 与纯语义基本持平、@20 反超(14/15),且额外提供排序治理与分面——k=10 的个别位次差是 external 被合理排后所致(结果在 @20 内,非丢失)。实测探针延迟 0.3s,不影响 watchdog。
> 唯一 @20 仍漏:muxcp-nl("MCP进程占满内存"↔muxcp)——embedding 语义鸿沟,需查询扩展/更强模型(未来)。

### 标识符腿(2026-10-21,T-0339 子任务)

向量换成 multilingual-MiniLM 后,中文问句里夹英文标识符的问法(如「launchctl bootout 之后马上 bootstrap 报错」)记忆集 literal 从 4/5 掉到 2/5。子串腿要求整句或所有词都出现在事实里,中文虚词让它永远不中。

做法(`kg_hub_server._ident_leg` + `tools/search_terms.py`):

- 只取查询里的 ASCII 片段(`model-attempts.sqlite3` 这种用 `.-/:+` 连起来的算一个片段,片段内各词都要命中),去掉单字母、停用词、两位以内纯数字。
- 走 `RELATES_TO.fact` 的全文索引而不是 CONTAINS 扫描:先查每个片段命中多少条(df),df > 3000 的泛词(error、nas、mac 之类)不拿来召回,只进分母;稀有片段各取索引分最高的 200 条,按命中片段的 idf 之和排出前 100 再取行。不用一次 OR 查询:索引自己的分数不看覆盖了几个片段,线上实测全覆盖的事实排不进 OR 的前 100。
- 全文索引不在中文标点/汉字处断词,「XAUTOCLAIM；Stream」被存成一个词,所以召回时 ≥4 字符的词用前缀查;覆盖度在 Python 里按 ASCII 字符切词、按完整词(只放宽复数)判定——紧贴中文的写法照样算中,前缀带进来的 InvalidCollector 不算中「Invalid」。
- 覆盖度 = 命中片段 idf 之和 / 查询所有在图中出现过的片段 idf 之和;≥ 0.5 才补入/提权,加分 = 1.0 × 覆盖度(语义分按排名换算在 0~1)。

离线全图模拟(存盘 MiniLM 向量 + 导出事实 + 线上全文索引真实返回 + 线上 episode 属性复刻 nudge;基线与线上实测逐格一致),top-10:

| 组 | 改前 | 改后 | 新中 / 新丢 |
|---|---|---|---|
| 记忆集 literal(5) | 2 | **3** | 1 / 0 |
| 记忆集 nl(11) | 3 | 3 | 0 / 0 |
| 原黄金集 literal(8) | 5 | 6 | 1 / 0 |
| 原黄金集 nl(7) | 2 | 2 | 0 / 0 |
| 中文问英文事实(100) | 30 | **38** | 9 / 1(McNemar p≈0.02) |
| 英文标题对照(100) | 72 | 74 | 2 / 0 |

记忆集 literal 没回到切换前的 4/5:launchctl 那条的记忆事实排第 12,前面是同一事故其他 episode 的事实(内容同样切题,只是不算这条记忆的金标);按 episode 限每条最多占 2–3 个名额也救不回来。剩下的差距要靠语义腿或问题式 fact。

线上少量抽样(本机跑新版 `search()`、只读连线上图,记忆集 16 条):literal 2 → 3、nl 3 → 3,与离线一致。

代价:全走倒排索引,抽样实测每次 p50 27 ms、最大 52 ms(本机经 tailnet 计)。同一次检索里原有的整句 / 全词 CONTAINS 扫描合计约 1.5–3 s,仍是 `/api/search` 最慢、也最让 NAS 发热的一步。

## 结论

1. **子串检索近乎不可用。** 连字面多词查询("ShuMei 图片审核""SLS 日志查询")都 0 召回——因为实现是把整串当一个子串 `toLower(e.fact) CONTAINS $query`,多词/跨语言/同义全废;自然语言 0/7。
2. **向量语义检索是数量级提升**(命中 3→13),且**代码里早就有**,只是 `/api/search` 默认走子串。这是③最高性价比的第一刀。
3. recall@10 绝对值仍低(25%),主因是宽主题 gold 被 cap 到 60,10 条结果盖不满——**命中率(能否召回到相关的)比 recall% 更能反映体验**,semantic 命中率 87%。
4. 仍 2 条 nl 全 0(muxcp内存症状、公众号别掉流量)——需 hybrid(关键词+语义+图遍历)或查询扩展补尾。

## ③ 下一步(实现,属并行赛道)

- **把 `/api/search` 换成 hybrid(语义为主 + 关键词兜底)**,而非继续子串默认。
- **⚠ 陷阱**:watchdog 用 `/api/search` 做**存活探针**且超时 10s,而 semantic "要几十秒"。**不能直接把默认端点换慢**,否则打挂监控。方案:hybrid 做快路径(关键词命中即返 + 后台语义补),或给探针换轻端点/健康检查,或语义加缓存。
- 排序:命中后按 相关性 × provenance(external 沉底) × durability(过期沉) × verified/usage 多因子(复用 canonical 排序)。
- facet 过滤:用 track① 落的 origin_project/kind/durability(如 S1 限当前项目+方法论/手册,S3 按项目时间线)。
- 每次改动都用 `python -m tools.eval_recall` 对黄金集打分,守住"可度量"。
