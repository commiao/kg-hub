# kg-hub 胶囊自进化 — 判分标准与场景路由设计

> **Status**: 📐 设计中（2026-06-22）。未实现。
> **关系**: `CONTRIBUTION-SIGNAL.md` 的结论是「真贡献度自动测不准」。本文是出路：
> 不追真值，而是用一个**诚实的自动判分**驱动系统**自进化**——好胶囊自己留下、
> 噪音胶囊自己淡出，无需 human 拍板。

---

## 1. 自进化闭环（一句话 + 四步）

> 试着贴 → 看这次成没成 → 成了且真用到就给分 → 高分多贴、冷门偶尔再试。

```
① 会话开始：按 (胶囊, 场景) 的历史得分挑 top_n 注入（+ 留 1 个探索名额试冷门）
② 会话进行：正常干活
③ 会话结束：自动算 reward（见下），更新被注入胶囊的得分
④ 下次会话：得分变了，选择自动跟着变 —— 这就是“自进化”
```

现有 `usage_count + 探索槽` 已是退化版闭环（reward 恒为「被注入」=循环自证）。
**本设计只改一件事：把 reward 从「被注入」换成「被注入且这次会话真的成了」。**
学习算法本身是成熟件（Beta 后验 + Thompson 采样 / 衰减成功率），代码量小，不是难点。

---

## 2. 判分是场景相关的 → 可插拔架构（本文核心）

同一胶囊在不同场景价值不同，所以：

- **得分按 `(胶囊, 场景)` 分开存**，不是每胶囊一个全局分。
  例：`DESIGN` 在 `planning` 场景得分高、在 `coding:sd-server` 场景得分≈0。
- **每个场景一个独立的判分器（RewardProvider）**，按场景路由。现在只实现 `coding`，其余留接口。

### 接口（稳定，别动）

```python
class RewardProvider:
    scenario: str
    def reward(self, session, injected_capsule) -> float | None:
        """0..1 的分；返回 None = 弃权（这次不学，避免拿脏标签污染模型）。"""

REWARD_PROVIDERS = {
    "coding":   CodingReward(),     # 本期实现
    "research": None,               # 预留：返回 None（弃权）
    "ops":      None,               # 预留
    "writing":  None,               # 预留
    "planning": None,               # 预留
}
```

### 主流程

```
scenario = classify(session)                 # 见 §3
provider = REWARD_PROVIDERS.get(scenario)
if provider is None:        # 未实现/未知场景 → 整段弃权，不更新任何得分
    skip
else:
    for cap in session.injected_capsules:
        r = provider.reward(session, cap)    # 可能 None=弃权
        if r is not None:
            update_score(cap, scenario, r)   # Beta 计数 / 带衰减的成功率
```

**关键纪律：拿不准就弃权（None），宁可不学，不可乱学。** 自进化系统最怕喂错标签——
错的 reward 比没有 reward 更糟（会自信地跑偏）。

---

## 3. 当前会话是什么场景？（场景判定）

### 3.1 两个时刻，两种精度

- **注入时（会话开始）**：信息少（只有 cwd + 可能的首条 prompt）→ **粗判**，用于*选*注入。
  v1 可直接用 `cwd/项目` 粗分（代码仓→偏 coding）。
- **结算时（会话结束）**：信息全（observations / 改了哪些文件 / 跑没跑测试）→ **细判**，用于*算 reward + 更新得分*。
  得分按结算时的细场景归类。

> 注入时猜错没关系（探索槽兜底）；结算时判准才重要，因为它决定学到哪个格子里。

### 3.2 判定方法：规则优先 → LLM 兜底 → 弃权

```
1) 规则（确定性、免费、覆盖大多数）：
   - 改了源码文件(.py/.go/.ts/.java…) 或 跑了 build/test/lint     → coding
   - 只读不改 + 大量 web/检索 + 产出是答案                         → research
   - 主要在看日志/告警/排障/改配置                                → ops
   - 只改 .md/.txt/文档目录                                       → writing
   - 无文件改动、以讨论/决策为主                                  → planning
2) LLM 兜底（仅当规则判不清时）：把 observations 摘要丢给便宜模型，
   分类到上述枚举之一或 unknown。
3) 仍不清 → unknown → 弃权（不更新得分）。
```

判定的输入信号（全自动，来自 `claude-mem.db` observations + git）：
`files_modified` / `files_read` 后缀与路径、observation `type`(feature/bugfix/refactor/discovery/decision)、
是否出现 build/test/CI 痕迹、有无 git diff/commit/PR、user prompt 文本。

### 3.3 混合会话

一个会话可能 coding + research 混杂。**v1：取主场景**（改动占比最大的）。
**预留**：多标签——把各场景的 reward 分别记到对应格子（接口已支持，按 capsule 分别判）。

---

## 4. coding 场景的判分（唯一现做）

reward 由两部分**取交集**，缺一不可：

### A. outcome —— 这次会话成了吗？（硬信号优先）

按可得性从强到弱，取到哪个用哪个：

| 信号 | 怎么取 | 强度 |
|---|---|---|
| 测试通过 / build 绿 / CI pass | observations 文本里的 pass/fail 标记；或 lint/test 工具输出 | 强（不撒谎） |
| 改动未被回滚 | git：该会话的改动后续没被 revert/覆盖重写 | 强 |
| PR 合并 | git/gh：关联 PR 状态 | 强 |
| 无错误回环 | 没有反复重跑同一失败命令 | 中 |
| 用户没重述/重启同一任务 | user_prompts：同任务没被换法重提 | 弱（兜底） |

**都取不到 → outcome 未知 → 整对弃权（reward=None）。** 不拿弱信号硬凑。

### B. attribution —— 成功能算这张胶囊头上吗？

用 Tier-1（`engagement_audit.py` 的术语重合）：胶囊的**具体内容**在会话工作里出现过，
才把这次成功记到它头上；否则一次成功的会话不该给所有被注入胶囊都发奖。

### 合成

```
reward = 1   若 outcome=成功 且 attribution=有
       = 0   若 outcome=失败/无关
       = None 若 outcome 测不到（弃权）
```

实现复用现成件：Tier-1 的 join + 术语重合、observations 读取、git 查询。

---

## 5. 预留其他场景（接口怎么填）

每个场景只需实现自己的 `outcome` 信号（attribution 多半都能复用 Tier-1）：

| 场景 | outcome 候选信号（待定） |
|---|---|
| research | 答案被采纳 / 用户没追问纠正 / 结论被后续会话引用 |
| ops | 告警恢复 / 故障未复发 / 服务指标回正 |
| writing | 文档被合并 / 没被大改 / 评审通过 |
| planning | 计划被执行（后续出现对应 coding 会话）/ 决策未被推翻 |

填法：写一个 `XxxReward(RewardProvider)`，实现 `reward()`，在 `REWARD_PROVIDERS` 注册。
**其余代码（场景判定、得分存储、选择、探索、护栏）全部不动。**

---

## 6. 防“舔狗”护栏（reward hacking）

自进化系统会朝你给的判分进化——判分有漏洞就被钻。固定加四道：

1. **探索地板**：永远留 ≥1 个名额给低曝光/新胶囊，得分再低也有翻身机会。
2. **多信号交集**：outcome 用硬信号、attribution 另一路，单一信号刷不动。
3. **弃权优先**：判不准就不学（§2 纪律）。
4. **周期抽审**：定期人工/消融抽查 N 条，比对系统判分有没有跑偏；漂移就回滚判分器。

---

## 7. 分阶段落地

1. **场景判定 v1（规则版）** + `(胶囊,场景)` 得分存储（落 FalkorDB 节点属性或旁路表）。
2. **CodingReward**：outcome（先接最易得的「未回滚 + 无错误回环」，逐步加 test/CI）∧ Tier-1 attribution。
3. **选择切到按 `(胶囊, 预测场景)` 得分 + 探索**（替换当前纯相关性+探索）。
4. **护栏**：探索地板 + 弃权 + 周期抽审。
5. 跑一段，抽审校准；稳了再考虑填 research/ops 等场景。

> 落地前提醒：reward 的**诚实度**是天花板（见 CONTRIBUTION-SIGNAL.md）。本设计的价值
> 在于「自动、自纠、按场景」，但它进化的方向只有判分标准那么准——所以 §4 的硬信号
> 选取和 §6 的护栏，才是真正决定成败的地方，不是学习算法。
