# tools/experimental — 胶囊「贡献度 / 自进化」探索代码（默认不接排序）

> **状态：实验性。ROI 存疑。默认不驱动任何线上行为。**

这里是为「自动测量胶囊贡献度、让胶囊集自进化」做的探索。设计与结论见
`docs/CONTRIBUTION-SIGNAL.md`、`docs/SELF-EVOLVING.md`。

## 一句话结论（读代码前先看）

**真实「因果贡献」在本领域基本不可自动测**（没有诚实的 outcome 信号、没有反事实、
聚合又带来共线性）。而且当前胶囊只有 ~9 个，**人工季度扫一眼比任何自动判分更准更省**。
这些工具是探索副产品，**不是已解决的方案，默认不接入排序**。当前线上排序仍是
`kg_hub_server.canonical_context` 的「相关性 + 探索」（那个已部署、已解决原始痛点）。

## 文件

| 文件 | 是什么 | 已知局限 |
|---|---|---|
| `engagement_audit.py` | Tier-1：曝光 vs 参与（确定性术语重合） | 相关非因果；逐字匹配，对语义复述失明 |
| `contribution_judge.py` | Tier-2：LLM 裁判贡献分 | **对 prompt 极敏感**（实测 DESIGN 1.33→收紧后 0）；话题混淆 |
| `scenario_classifier.py` | 规则版会话场景判定（coding/ops/…） | files_modified 采集不全；边界靠 type 兜底 |
| `capsule_score.py` | `(胶囊,场景)` Beta 得分本地 JSON 旁路表 | 本地、单机；非中心 |
| `coding_reward.py` | coding 场景判分 = outcome ∧ attribution | outcome 文本标记保守→大量弃权 |
| `self_evolve_update.py` | 闭环更新通路（注入→会话→场景→更新得分） | 本地管道产出率极低（实测 80% 注入对不上会话） |

## 为什么搁这儿而不是删掉

记录了「此路为何不通」的实证（尤其 Tier-2 的 prompt 敏感、聚合共线性），避免未来有人
重启这条路重走一遍。要继续，唯一被认可的最小路线见 `SELF-EVOLVING.md §10`：
**摄入环节做相关性裁判（目标=相关性非真贡献）、仅用于降权、保留探索地板**——且先确认
胶囊规模真的大到值得自动化。

## 运行（仅离线分析/原型用）

```
python -m tools.experimental.engagement_audit          # 曝光 vs 参与
python -m tools.experimental.scenario_classifier --route
python -m tools.experimental.self_evolve_update --dry  # 不落盘
```

> 注：这些读本机 `data/.push_hook.log`（会轮转）+ `~/.claude-mem/claude-mem.db`，
> 只覆盖本机 Claude Code 会话，非跨设备/工具。
