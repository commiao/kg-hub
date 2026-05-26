# 新会话启动 prompt

> 把下面这段 **完整复制** 粘到新会话的第一条消息里。
> 不要省略，不要简写——目的就是让新会话的 AI 不用看上文也能上下文齐全。

---

## 复制这一段 ↓↓↓

```
我正在启动一个新项目 kg-hub（个人 AI 中央知识图谱）。

**项目位置**：`/Users/mac/workspace_claudeCode/kg-hub/`

**请你做的事**：
1. 读完下面四份文档（按顺序）：
   - `/Users/mac/workspace_claudeCode/kg-hub/README.md` — 项目入口
   - `/Users/mac/workspace_claudeCode/kg-hub/DESIGN.md` — 已做的设计决策（**不要重新讨论已锁定的事**）
   - `/Users/mac/workspace_claudeCode/kg-hub/ROADMAP.md` — 4 个 Phase 的任务清单
   - `/Users/mac/workspace_claudeCode/claude-mem/HANDOVER.md`（父项目，扫一遍知道现状即可）

2. 读完后回我一个**简短的确认**：
   - 项目目标一句话
   - 你理解的 Phase 0 是什么
   - 你看出来的最大风险是哪个
   - 你建议第一步具体做什么（不要超过 5 分钟）

3. 等我确认后，我们开始干 Phase 0（数据探索）。

**关键约束**：
- 这是规划阶段，目前**没有写任何代码**
- 父项目 claude-mem 已经稳定运行，**绝对不要动它的任何文件**
- **Phase 0 的数据源是 OpenClaw 胶囊导出**（不是 claude-mem obs），导出快照放在 `kg-hub/data/openclaw-snapshot-2026-05-14/`（如果还没导出，先提醒用户导出）
- claude-mem 的 SQLite 在 `~/.claude-mem/claude-mem.db`（**Phase 0 不用，Phase 2 才用**）
- LLM 走阿里百炼 qwen3.6-plus（凭证在 `~/.claude-mem/.env`，不要打印 token）
- 不要急着上技术栈，先把 Phase 0 的数据探索做扎实

**禁止行为**：
- ❌ 修改 claude-mem 的任何文件
- ❌ 修改 `~/.claude-mem/.env` / `~/Library/LaunchAgents/com.claude-mem.worker.plist`
- ❌ 重启 worker、kill 进程
- ❌ 修改 OpenClaw 导出的原始文件（snapshot 目录只读）
- ❌ 跑 `npm install` / `pip install` 之前要先问我
- ❌ Phase 0 还没做完不要进 Phase 1

请先读文档，回报理解，等我说"开干"再启动 Phase 0。
```

## ↑↑↑ 复制到这里结束

---

## 启动后你应该看到的样子

新会话的 AI 应该回你**类似这样**的内容（如果跑偏了，纠正它）：

```
✅ 已读完 4 份文档。

项目目标：把多源（OpenClaw 胶囊 + claude-mem obs）的工程知识汇聚成中央知识图谱，
让所有 AI 工具通过 MCP 做跨设备跨项目跨工具 RAG。

Phase 0：把用户从 OpenClaw 导出的 179 胶囊 + 36 知识文档 + MEMORY.md 映射进
schema v0.2，用 qwen 抽出隐式关系，复现 OpenClaw 给的 5 跳因果链样例；
通过门槛是 "≥150 胶囊入图 + ≥50 隐式关系 + ≥3 条 4+ 跳因果链 + 你认可"。

最大风险：Schema v0.2 还没经过真实数据冲击 — Phase 0 跑下来很可能要再调；
另一个风险是 OpenClaw 的隐式关系是否能被 qwen3.6-plus 稳定抽出。

建议第一步：先确认 data/openclaw-snapshot-2026-05-14/ 是否已存在，若无，
提醒你从 OpenClaw 导出 capsule-metadata.json + notes/ 全量。导出后我先扫描
inventory.json 看数据形态，再写 schema 映射脚本。
```

如果新 AI **跳过**了 Phase 0 直奔 Phase 1（要装 Memgraph 之类），**立刻打断它**，让它回去先做 Phase 0。

---

## 期间用得上的命令

启动新会话后你可能要执行这些（让 AI 给你跑或自己跑）：

```bash
# 看 OpenClaw 快照是否就位
ls -la /Users/mac/workspace_claudeCode/kg-hub/data/openclaw-snapshot-2026-05-14/

# 看胶囊总数
ls /Users/mac/workspace_claudeCode/kg-hub/data/openclaw-snapshot-2026-05-14/notes/capsules/*.md | wc -l

# 看 capsule-metadata.json 结构
jq 'to_entries | .[0]' \
  /Users/mac/workspace_claudeCode/kg-hub/data/openclaw-snapshot-2026-05-14/capsule-metadata.json

# 看 claude-mem obs 备用（Phase 2 才用）
sqlite3 ~/.claude-mem/claude-mem.db "SELECT COUNT(*) FROM observations;"
```

---

## 提示：开新会话最佳实践

- **开新 Claude Code 会话时**，先 `cd /Users/mac/workspace_claudeCode/kg-hub/` —— 让 Claude 自动把这个目录加入它的工作区，读文件更方便
- **不要在 claude-mem 的对话里继续聊 kg-hub** —— 上下文越混越乱
- **保留这个文件**：以后再开新会话还能用同一份 prompt
