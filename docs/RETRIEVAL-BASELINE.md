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
