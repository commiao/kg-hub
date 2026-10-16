# kg-hub 预抽取：实体/关系抽取规则（prompt_version = agent-v3）

你在把一条 claude-mem 观测抽成 kg-hub schema v0.2 的知识图三元组。输出只给 JSON，不要解释。
这份规则就是 graphiti 原来喂给网关模型的约束，再加上 10 月画像里暴露的三类纪律问题的硬规定。

## 实体类型（只能用这 13 个，区分大小写）

| type | 用于 | 例子 |
|---|---|---|
| Person | 人 | jingmiao@liblib.ai |
| Project | 代码项目/仓库 | kg-hub, claude-mem, credvault |
| File | 具体文件或目录 | kg_hub_server.py, docs/DESIGN.md |
| Tool | 软件/服务/组件/命令 | FalkorDB, launchd, model-gateway, refinery |
| Concept | 抽象概念/规则/参数/业务 key/模型名 | 断路器, SEMAPHORE_LIMIT=1, kg_hub.entity_extract, qwen3.6-plus |
| Issue | 发生过的问题/故障/错误 | 429 额度用尽, search 30s 超时 |
| Fix | 对 Issue 的具体修复 | 把 timeout 改成 180s, PR #106 |
| Config | 配置文件/配置项 | ~/.claude-mem/.env, docker-compose.yml |
| Session | 一次会话 | 2026-10-16 的 codex 会话 |
| Observation | claude-mem 原始观测本身（很少用） | |
| Capsule | OpenClaw 知识胶囊 | CAPSULE-2026-07-22-001 |
| KnowledgeDoc | 参考文档 | feishu-image-upload-guide.md |
| Lesson | 提炼出的原则/教训 | 发布前先排空在飞抽取 |

## 关系名（只能用这 15 个，小写）

caused_by（Issue→原因）、fixed_by（Issue→Fix）、verified_by（Fix→验证它的 Observation/Capsule）、
references（提到）、modified（修改了 File）、depends_on（依赖）、supersedes（新 Fix 取代旧 Fix）、
derived_from（Concept/Lesson 来自某观测/会话）、occurred_in（发生在某 Session）、
belongs_to（属于某 Project）、extracted_from（Capsule 来自 Session）、relates_to（其他一切相关）、
documented_in（Concept 记录在 KnowledgeDoc）、diagnosed_by（Issue 在 Capsule 里被诊断）、
implemented_as（方案实现为 Fix）。

## 硬规定（违反会被服务端降级并计入纪律指标）

1. **不要把这些当实体**：commit 短哈希/完整 SHA、`/Users/...` `/volume...` 之类绝对路径、`T-0xxx` 任务号、
   端口号、单个数字、时间戳。它们写进 summary 或 fact 里即可。
2. **关系名只用上面 15 个**。想不出合适的就用 relates_to，不要自创 `IS_ANCESTOR_OF`、`BINDS` 这类名字。
3. **实体名用最常见的规范写法**，同一事物只出现一次；文件用相对路径或文件名，不带仓库前缀。
4. **一条观测通常 4–12 个实体、3–10 条关系**。超过 20 个实体说明抽得太碎。
5. **fact 必须是能独立成立的一句话**（谁对谁做了什么/是什么关系），不要只是复述标题；
   不要把瞬时状态（某次 DNS 失败、队列深度 5）写成长期关系。
6. **summary 一两句话**，说这个实体是什么、在这条观测里扮演什么角色；没信息就留空字符串。
7. 关系的 source/target 必须是本条 entities 里的 name（完全一致），否则这条边会被丢弃。
8. 观测确实没有可抽取的知识（纯噪声、空内容）时，entities 给空数组，不要硬凑。

## 输出格式

```json
{
  "id": 29995,
  "entities": [
    {"name": "model-gateway", "type": "Tool", "summary": "NAS 上的模型网关，暴露 /health/ready 与 /health/reconciliation"},
    {"name": "T-0167 前提证伪", "type": "Issue", "summary": "任务要求 /health/ready 暴露 provider_pending，实际在 /health/reconciliation"}
  ],
  "edges": [
    {"source": "T-0167 前提证伪", "name": "caused_by", "target": "model-gateway", "fact": "T-0167 的前提不成立，因为 model-gateway 把 provider_pending 放在 /health/reconciliation 而不是 /health/ready"}
  ]
}
```

多条观测一起输出时用 `{"items": [ {...}, {...} ]}`，每个 item 都带 `id`。

## v2 补充（2026-10-17，第 1 轮 claude-haiku-5-5 500 条复盘后定下）

第 1 轮实测：平均每条只抽 2.9 条关系（22% 的观测不足 3 条）；服务端按名字近似合并、不看类型，
短而有歧义的名字把 Issue 并进了 Config、Tool 并进了 Issue、文件并进了同名不同路径的另一个文件；
被后续观测推翻的临时判断和同一对象的互相矛盾状态原样入库。因此：

9. **关系下限是 3 条，不是上限。** 一条观测通常 3–10 条关系；只有 2 条时要在某条 fact 里说明
   这条观测只涉及两层关系；只有一句授权/确认语句、没有可落图知识的观测，entities 给空数组，不要硬凑。
10. **实体名要能独立区分同名不同物**（服务端合并时不看类型）：
    - 文件用完整相对路径（`deploy/nas/.env`、`src/server/worker-spawner.ts`），同名文件靠路径区分；
    - 配置键用原样键名（`KG_HUB_MODEL_GATEWAY_TOKEN`），配置文件用路径，两者不混；
    - 工具/服务用服务名（`connection-status 探针`），问题用完整句（`connection-status 探针长期误报`），
      两者不能同名；
    - 同一主题的不同事件要带日期或标识（`2026-09-06 事故复盘`、`2026-09-10 悬账事故`）。
11. **被后续观测推翻的判断不入库**：观测里明说「初判 X，后确认为 Y」时只抽 Y，X 最多写进 fact
    （「曾初判为 X，后推翻」）。同一对象在不同观测里状态不同（如 refinery 窗口 01:00–08:00 / 22:00–08:00 /
    22:00–10:00），fact 里必须写明时间或来源（「截至 2026-09-21 生产为 22:00–10:00」），不要写成无时间的断言。
12. **读全文再抽**：长正文不要截断看前几百字；Key facts 往往在后半段。
13. **自检后再提交**：每条边的 source/target 必须是本条 entities 里的 name 原文，不满足就修正，不要带着悬空边提交。

## v3 补充（2026-10-17，第 2 轮 claude-opus-5-5 517 条复盘后定下）

第 2 轮实测：跨类型误并没有了，但**同类型近名**仍误并 17 对——兄弟概念用「同一前缀 + 不同后缀」命名
（credvault dashboard 发布作业恢复 / 安全删除凭证）、只差一个字符或数字（test_phase1m / 1p、准则 19–22 / 26–28）、
跨仓库同名文件（kg-hub 与 credvault 各有 release.sh）、描述性名字被泛名吸进（max_tokens 路由上限裁剪 → max_tokens）；
跨批被后续观测推翻的判断漏了 5 组；回顾/复述类观测被硬凑出近义节点。服务端已把消解收紧（代码类名字只认完全相等、
只差数字的不复用、包含占比 0.8），执行侧还要做到：

14. **区分词放最前面**：兄弟概念不要共享长前缀，写成「发布作业恢复（credvault dashboard）」而不是
    「credvault dashboard 发布作业恢复」；描述性概念不要以某个泛名开头再加后缀，否则会被吸进泛名节点。
15. **只差动词/一个词的代码标识符**（mark/clear、activate/retire、same_x/x）只把一个作为实体，其余写进 fact。
16. **跨仓库同名或近名文件写成 `仓库名:路径`**（`kg-hub:deploy/nas/release.sh`、`credvault:deploy/nas-release.sh`），
    这是规则 3「不带仓库前缀」的唯一例外。
17. **事件名要带区别性描述**，不能只靠日期：「看板改路由致网关 503（2026-09-19）」与「两会话先后部署致 503（2026-09-18）」。
18. **回顾、复述、状态询问、授权、「说人话」类观测给空**；汇总类观测只抽相对前面观测**新增**的事实，可以少于 3 条边，
    在某条 fact 里注明「汇总观测，仅新增事实」。规则 9 的下限对这类观测不适用。
19. **用 batch 文件里的 `lookahead`**（下一批观测的标题）：若标题显示本条的判断随后被更正，本条只抽被确认的部分，
    初判写进 fact；写入前可用 `submit --dry-run` 预览每个实体会并到哪个旧实体，并错就改名再正式提交。
20. **同一会话用过的名字见 `names-<model>.tsv`**，复用时按原样写，不要改写法。
