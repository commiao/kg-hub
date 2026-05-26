# kg-hub Onboarding — how to plug a new tool in

> 给任何想接入 kg-hub 的新工具：照这份文档走，30 分钟内能写 + 读。
> 不需要懂内部架构，只需要选对路径 + 抄两段代码。

---

## TL;DR — 哪条路？

```
你的工具是不是 MCP-capable（能跑 Anthropic MCP 协议）?
├─ 是  → 走 [路径 A: MCP] → 配 muxcp 一行 → 自动得到 6 个工具
└─ 否  → 走 [路径 B: HTTP] → curl /api/ingest 写 / /api/search 读
```

**例子定位**：

| 工具 | 路径 |
|---|---|
| Claude Code | A（已自动接入 muxcp） |
| Cursor | A（已自动接入 muxcp） |
| Codex 桌面 | A（已自动接入 muxcp） |
| OpenClaw 小迪 | B（kg-query skill 已部署，见 `clawd/skills/kg-query/`） |
| 飞书 webhook | B |
| git pre-commit hook | B |
| Slack bot | B |
| 任意 Python / Node 脚本 | B |

---

## 路径 A：MCP 接入

### 接入步骤（如果 muxcp 已配好）

**零步骤**——你已经接 muxcp（跟 Claude Code / Cursor / Codex 共用 `~/public-sync/cc-switch-sync/mcp/muxcp/current.yaml`），就**自动**得到 6 个 kg-hub 工具：

```
mcp__muxcp__kg_hub__kg_search           语义搜索 facts（读）
mcp__muxcp__kg_hub__kg_node_neighbors   查实体邻居（读）
mcp__muxcp__kg_hub__kg_path_between     两实体路径（读）
mcp__muxcp__kg_hub__kg_episode_search   原文 episode 全文搜（读）
mcp__muxcp__kg_hub__kg_stats            实体/边/episode 总数（读）
mcp__muxcp__kg_hub__kg_add_episode      写入 episode（写, Phase 3 新增）
```

### 如果还没接 muxcp

在 IDE 的 MCP 配置文件（Cursor 是 `~/.cursor/mcp.json`、Codex 是 `~/.codex/mcp.json` 等）加：

```json
{
  "mcpServers": {
    "muxcp": {
      "command": "/Users/mac/.local/bin/muxcp",
      "args": ["-config", "/Users/mac/public-sync/cc-switch-sync/mcp/muxcp/current.yaml"]
    }
  }
}
```

重启 IDE → kg-hub 工具自动可见。

### 调用范例（自然语言给 LLM）

```
"看看知识图谱里有多少节点"        → AI 调 kg_stats
"查 Cron 通知失败的修复历史"      → AI 调 kg_search
"小迪 跟 飞书 是怎么关联的"        → AI 调 kg_path_between
"把'今天我们完成了 Phase 3'记进 KG" → AI 调 kg_add_episode
```

---

## 路径 B：HTTP 接入

### 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET  | `/health` | 存活探测（无需 auth） |
| GET  | `/api/search?q=...` | 语义搜索 |
| POST | `/api/ingest` | **写入 episode（默认异步）** |
| GET  | `/api/ingest/status?source_description=X&source_obs_id=Y` | 查写入进度 |
| GET  | `/api/queue_stats` | 队列健康总览 |

### 鉴权

所有写/查接口（除 `/health`）要 Bearer token：

```
Authorization: Bearer <KG_HUB_API_TOKEN>
```

Token 存哪：
- Mac 上是 `~/.claude-mem/.env` 里 `KG_HUB_API_TOKEN=...`
- OpenClaw VPS 上是 `~/.openclaw/env.sh` 里 `KG_HUB_TOKEN=...`
- 新接入工具：跟管理员拿 token，**不要硬编码进源码**

### 网络可达性

`kg-hub` 当前监听 **Mac 上 `0.0.0.0:8080`**：

- 本机：`http://127.0.0.1:8080`
- Tailscale 网内其它设备：`http://mac-office:8080`（或 IP `100.99.15.39`）

加入 Tailscale 才能跨设备访问（公网不开放）。

### 写入：POST /api/ingest 

**请求 schema（Decision 14）**：

```json
{
  "name": "<short identifier>",
  "episode_body": "<full natural-language text the LLM will extract entities from>",
  "source_description": "<who's writing — e.g. 'feishu-webhook', 'git-commit-hook'>",
  "reference_time": "<ISO 8601, e.g. '2026-05-19T03:00:00Z'>",
  "source_obs_id": "<unique-within-source ID for idempotency>",
  "sync": false
}
```

**默认 sync=false（异步）**：返回 `202` + `poll_url`，extraction 在后台跑。

**返回（异步）**：
```json
{
  "status": "accepted",
  "source_description": "feishu-webhook",
  "source_obs_id": "msg-1234567",
  "poll_url": "/api/ingest/status?source_description=feishu-webhook&source_obs_id=msg-1234567",
  "hint": "extraction running in background; check status via poll_url or just kg_search later"
}
```

**返回（幂等命中 = 同一 source_obs_id 已成功）**：
```json
{ "status": "skipped", "reason": "duplicate", "episode_uuid": "..." }
```

**返回（同一 source_obs_id 正在跑）**：
```json
{ "status": "in_progress", "source_description":"...", "source_obs_id":"..." }
```

### 重要：source_obs_id 必须**幂等键**

- 同源数据再次推送（重试 / 网络抖动复发）必须用**同一个** `source_obs_id`，server 会去重
- 不同源数据用**不同**的，否则会被误判重复

幂等键好的选择：
- 飞书消息：`message_id`
- git commit：`commit_sha`
- claude-mem obs：`obs_id`（数据库行 PK）
- Cursor 手动写：内容的 SHA256 / 时间戳-纳秒

### 查询：GET /api/search

```bash
curl -G "$KG_HUB_URL/api/search" \
  --data-urlencode "q=Cron 通知失败" \
  --data-urlencode "num_results=10" \
  -H "Authorization: Bearer $KG_HUB_TOKEN"
```

返回 `results: [{fact, source_node_uuid, target_node_uuid, valid_at, created_at}, ...]`。

### Python 示例（写 + 读）

```python
import httpx, os, hashlib

KG_HUB_URL = os.environ["KG_HUB_URL"]      # e.g. http://mac-office:8080
TOKEN = os.environ["KG_HUB_API_TOKEN"]

def write(text: str, source: str, idempotency_key: str = None):
    """Push an episode. Returns immediately (async by default)."""
    key = idempotency_key or hashlib.sha256(text.encode()).hexdigest()[:16]
    r = httpx.post(
        f"{KG_HUB_URL}/api/ingest",
        json={
            "name": text[:60],
            "episode_body": text,
            "source_description": source,
            "reference_time": "2026-05-19T00:00:00Z",
            "source_obs_id": key,
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=30.0,
    )
    return r.json()

def search(query: str, n: int = 10):
    r = httpx.get(
        f"{KG_HUB_URL}/api/search",
        params={"q": query, "num_results": n},
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=15.0,
    )
    return r.json()["results"]
```

### Bash 示例（同上的 curl 版）

参考 `clawd/scripts/kg-query.sh` 那一份 production-ready 实现——含错误码、env 加载、参数校验。

---

## 数据模型（你不一定要懂，但好奇时看）

```
你 push → /api/ingest
  ├─ IngestedKey (k:IngestedKey) 节点记账: status pending/ok/error
  └─ graphiti.add_episode (后台):
      ├─ LLM 抽 Entity / Edge
      ├─ MERGE (n:Entity) MERGE (a)-[:RELATES_TO]->(b)
      └─ 写入 (e:Episodic {content=原文, uuid=...})

查询 /api/search:
  └─ 语义搜 facts (RELATES_TO 边的 .fact 属性) + 返回 source/target Entity UUID
```

详细 schema 见 `DESIGN.md` §4 (v0.2: Entity / Capsule / Issue / Fix / Concept / ... 13 节点类型)。

---

## 异步语义解释

| 情况 | 客户端看到 |
|---|---|
| 全新写入 | `202 accepted` 立即返回, **后台 ~30-200s 跑 LLM extraction**, 完成后 IngestedKey 状态 `ok` |
| 重复写入(同 sid 已 ok) | `200 skipped` 立即返回 |
| 重复写入(同 sid 还在 pending) | `202 in_progress` 立即返回 |
| 上次失败的 sid 重试 | `409 previous_attempt_failed`, 调用方需先删 IngestedKey 才能重试 |
| writer.lock 拿不到 | 后台任务 180s 后内部超时, IngestedKey 进入 `error` 状态 |

→ **客户端从不阻塞超过 ~5 秒**。要等结果的话用 `poll_url`。

### "我怎么知道我的 episode 真进了图谱？"

3 种确认方式：

1. **轮询** `poll_url` 直到 `status: ok`
2. **直接搜**：等 10-60s 后 `/api/search?q=...你的关键词...`
3. **看监控**：`/api/queue_stats` 看 `ok_total` 涨了

---

## 监控（写完后想知道有没有坏）

### 主动告警（watchdog 自动跑）

不用你查——`com.kg-hub.watchdog` 每 10 分钟自动巡检，**只在状态变化时**通知：

- 飞书 webhook（如果 env 设了 `KG_HUB_FEISHU_WEBHOOK`）
- macOS 通知中心（兜底）
- `~/.kg-hub/logs/alerts.log`（永久审计）

告警事件：
- `server_down` → /health 不可达
- `queue_backlog` → pending > 5
- `stuck_jobs` → 有任务 pending > 30 min
- `recent_errors` → 上一小时 error 数 > 0

### 主动查询

```bash
# 服务死活
curl http://mac-office:8080/health

# 队列状态
curl -H "Authorization: Bearer $TOKEN" http://mac-office:8080/api/queue_stats

# 单个写入的进度
curl -H "Authorization: Bearer $TOKEN" \
  "http://mac-office:8080/api/ingest/status?source_description=X&source_obs_id=Y"
```

### 日志

```
~/.kg-hub/logs/server.out.log        FastAPI + uvicorn + [ingest:*] 生命周期
~/.kg-hub/logs/server.err.log        异常栈
~/.kg-hub/logs/watchdog.out.log      巡检每次的简短输出
~/.kg-hub/logs/alerts.log            告警审计（边沿触发）
~/.kg-hub/logs/claude-mem-ingest.*   定时 ingester
~/.kg-hub/logs/openclaw-sync.*       定时 OpenClaw 同步
```

---

## 常见问题

### Q1: 我重复推送同一条会有重复实体吗？

**不会**。只要 `source_obs_id` 一样，server 端 MERGE 去重；不一样的 `source_obs_id` server 当作新 episode 处理。

### Q2: 我的写入返回 202，但 10 分钟后 search 还查不到怎么办？

按这个顺序排查：
1. `curl /api/ingest/status?source_description=X&source_obs_id=Y` 看 status 是 `pending`/`ok`/`error`
2. `pending` 持续 > 30 min → server 出了大问题（watchdog 会告警）
3. `error` → 看 `error_message`
4. `ok` 但搜不到 → 关键词跟实际 facts 不匹配，**改用 `kg_episode_search` 或 `kg_node_neighbors` 用具体 Entity 名查**

### Q3: 我的 server_obs_id 可以多长？

任意字符串都行，**保持稳定 + 全 source 内唯一**即可。常用：UUID / 哈希 / 业务系统的主键。

### Q4: kg-hub 当前数据规模？

跑一次 `kg_stats` 自己看。截至 2026-05-19 大约 1900 entities / 4300 edges / 880 episodes。

### Q5: 我能批量推送吗？

**目前没有 batch endpoint**。一次一个 POST。需要批量场景请单独提，可以加 `/api/ingest/batch`。

### Q6: 我的工具的"manual write"应该怎么走？

| 场景 | 走哪条 |
|---|---|
| Claude Code 自动捕获（你不主动写） | 已经在跑（claude-mem hook）, 你什么都不用做 |
| Cursor 让 AI 主动"记一下" | A 路径 `kg_add_episode` MCP 工具 (本地 Mac) |
| OpenClaw 小迪自动产生胶囊 | 已经在跑（rsync pull, Phase 2）, 你什么都不用做 |
| 任意非 Mac 工具想推数据 | B 路径 HTTP POST /api/ingest |

详细决策见 `DESIGN.md` 决策 16。

---

## 路径 C：内嵌 ingester（高复杂度，仅限 claude-mem 这种特例）

只当你的工具有**自己的本地数据库**且需要**批量 / 调度 / 复杂的源数据清洗**时再走这条。参考 `ingesters/claude_mem_obs.py` 完整实现。**绝大多数新工具不需要这条路**。

---

## 接入后通知人

新接入一个源后，**写一条 capsule 进 kg-hub** 自己 broadcast：

```json
{
  "name": "Source X integrated",
  "episode_body": "Source 'X' is now writing to kg-hub via path B. source_description='X', expected volume ~10/day, primary fields: ...",
  "source_description": "X",
  "reference_time": "<now>",
  "source_obs_id": "kg-hub-integration-announcement"
}
```

这样别人查 `kg_search "what sources feed kg-hub"` 就能发现新增。

---

## 设计原则速查

1. **接入协议统一**：所有写入走 `/api/ingest` schema（决策 14）
2. **本地优先**：每个源在自己环境内本地化（决策 1）
3. **幂等去重**：`(source_description, source_obs_id)` 唯一键（决策 14）
4. **异步默认**：写入立即返回，extraction 后台跑（决策 16）
5. **并发安全**：writer.lock 跨进程串行化（决策 12）
6. **跨设备通过 Tailscale**：不开公网（决策 7）

完整决策见 `DESIGN.md` §3。

---

## 维护者联系

- 项目根：`/Users/mac/workspace_claudeCode/kg-hub/`
- 维护者：jingmiao@liblib.ai
