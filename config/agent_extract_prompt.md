# kg-hub 预抽取：实体/关系抽取规则（prompt_version = agent-v1）

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
