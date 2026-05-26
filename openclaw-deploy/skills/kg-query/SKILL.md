---
name: kg-query
version: 1.0.0
description: 跨工具中央知识图谱查询 (kg-hub) - 当本地 MEMORY.md / memory_search 找不到答案时, 查 Mac 上 Cursor/Codex/Claude Code 累积的工程记忆
user-invocable: true
disable-model-invocation: false
metadata: {
  "clawdbot": {
    "emoji": "🧠",
    "category": "知识",
    "keywords": ["知识图谱", "kg-hub", "跨工具记忆", "cross-tool memory", "central memory"],
    "supportedPlatforms": ["feishu", "cli"],
    "requiresSetup": false
  }
}
---

# 🧠 kg-query — 跨工具中央知识图谱查询

查询 jingmiao 在 Mac 上 (Cursor / Codex / Claude Code) 工作累积的中央知识图谱 (kg-hub)。这是**你本地知识体系之外**的另一层记忆。

---

## 何时调用

✅ **应该调用**:
- 用户问及你本地 `MEMORY.md` / `memory/` / `notes/` 没有的工程话题
  - 例如: "kg-hub 现在的架构怎么样了"
  - 例如: "上次在 Cursor 里讨论 Phase 3 的方案是什么"
- 用户提到 "Mac 上" / "Cursor 里" / "Claude Code 里" 累积的事
- 你回答前不确定本地是否有答案 → 先 `memory_search` 本地, miss 后再调 kg-query

❌ **不要调用**:
- 闲聊 / 实时计算 / 你能直接回答的事
- 本地 MEMORY.md / memory_search 已经返回足够答案
- 用户问的事明显跟 Mac 工程无关 (例如北京户口、股票、记账)

---

## 调用方式

```bash
bash /home/admin/clawd/scripts/kg-query.sh "<你的问题>"
```

可选参数:
```bash
bash /home/admin/clawd/scripts/kg-query.sh "<问题>" <num_results>
# num_results 默认 10, 最多 30
```

---

## 返回格式

成功:
```json
{
  "status": "ok",
  "query": "Cron 通知失败",
  "results": [
    {
      "fact": "Cron is configured with 5 scheduled tasks for capsule operations",
      "source_node_uuid": "...",
      "target_node_uuid": "...",
      "valid_at": "2026-03-19T13:30:00+00:00"
    },
    ...
  ]
}
```

失败 (kg-hub 不可达):
- 退出码非 0
- stderr 有 curl 错误信息

---

## 失败兜底

kg-hub 不可达时 (Mac 关机 / Tailscale 断):
- **不要**假装查到了
- 告诉用户 "中央知识图谱当前不可达 (Mac 离线?)"
- 按你本地 MEMORY.md / 知识库回答

---

## 数据流向

```
你 (OpenClaw 小迪)
   │ "请查 kg-hub 关于 X"
   ▼
kg-query skill (本 SKILL.md)
   │
   ▼
bash /home/admin/clawd/scripts/kg-query.sh
   │ curl GET /api/search?q=X
   ▼
Tailscale 内网 → Mac (mac-office:8080)
   │
   ▼
kg-hub server (FastAPI)
   │
   ▼
FalkorDB → 返回相关 facts
```

---

## 关于 kg-hub 是什么 (上下文)

kg-hub 是 jingmiao 的"个人级 GraphRAG"——把多个工具(Claude Code / Cursor / OpenClaw / claude-mem)累积的工程记忆**聚合到一个中央图谱**。

**你 (OpenClaw) 是其中一个数据源**——你产生的胶囊会通过 rsync 同步进 kg-hub。同时你**可以反过来查 kg-hub**，看其它工具积累了什么。

这个反查能力, 就是本 skill 的全部目的。
