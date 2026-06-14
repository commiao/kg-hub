# Agent tool discovery — MCP + Skill 双轨制盲区

> **Status**: 🚨 Canonical · Pinned via PUSH hook to all new sessions.
> **Scope**: 所有跑在 Claude Code（以及 Cursor / Codex / Qoder 等支持 MCP+Skill 的环境）之上的 AI agent，含 main agent 和 sub-agent。
> **本质**: 不是 kg-hub 内部架构，是 **agent 自我认知的强制规则**。

---

## 1. 核心规则（一句话）

**声明"机器没有 X 能力"前，必须先扫两个独立空间**：

1. **MCP 空间** — 用 `ToolSearch` 关键词查 deferred tools（工具名形如 `mcp__server__tool_name`）
2. **Skill 空间** — `ls ~/.claude/skills/` 看是否有相关 skill（或 ~/.codex/skills/、~/.cursor/skills/ 等多轨配置）

**任一空间命中都说明能力存在**。两个空间互不可见，**只搜其中一个 = 静默放弃**。

---

## 2. 为什么会盲

Claude Code 的 agent 能力扩展是双轨制：

| 轨道 | 注册位置 | 调用方式 | 工具列表 |
|---|---|---|---|
| **MCP tools** | mcp-config（HTTP / stdio MCP server） | `ToolSearch` 加载 schema 后直接调用 | 通过注入的 deferred tools 索引 |
| **Skills** | `~/.claude/skills/<name>/SKILL.md`（多为符号链接到 `~/.skillshub/`） | `/<name>` slash command 或 `Skill` 工具 | 通过 system-reminder messages 列出的 available skills |

两套机制完全独立：
- MCP 搜不到 ≠ Skill 也搜不到
- Skill 不在可见列表 ≠ 不存在（用户在消息里显式输入 `/<name>` 也算授权调用）
- **`ToolSearch` 只覆盖 MCP，不覆盖 Skill**

---

## 3. 真实案例（2026-06-14）

**起因**: 用户问能否读飞书文档（`https://my.feishu.cn/docx/XXX`）。

**错误路径**:
1. AI agent 用 `ToolSearch "feishu lark docx document"` → 返回 "No matching deferred tools found"
2. agent 声称"机器没有飞书 MCP 工具"，提议 fallback（装新 MCP / 让用户改文档权限 / 让用户贴正文）
3. 用户纠正："lark 是 skill 不是 MCP，比如 `/lark-doc`，全套官方飞书 skill"
4. 扫描 `~/.claude/skills/` 后发现：**30+ 个 lark-* skill 全部已装**（lark-doc / lark-mail / lark-sheets / lark-base / lark-wiki / lark-calendar 等），符号链接到 `~/.skillshub/`

**结果**:
- AI 用 `Skill("lark-doc")` 调用 → 内部用 `lark-cli docs +fetch --api-version v2` 成功拉取文档内容
- 错误判断浪费了若干轮对话 + 差点让用户做不必要的 fallback 工作

**根因**: agent 没扫 Skill 空间就静默放弃，违反"两个空间都要扫"规则。

---

## 4. 强制检查清单

声明"无 X 能力"前，按顺序确认：

- [ ] **MCP 空间**: 用 `ToolSearch` 查关键词（同义词都试）
- [ ] **Skill 空间**: `ls ~/.claude/skills/ | grep -i <关键词>`（macOS / Linux）
- [ ] **会话注入**: 看 SessionStart hook / system-reminder 里有没有相关工具列表
- [ ] **用户消息**: 用户消息里是否出现过 `/<skill-name>`（这本身是显式授权调用）
- [ ] **多 IDE 配置**: 在多 IDE 环境，每个 IDE 可能有自己的 skill 注册目录（`~/.codex/skills/`、`~/.cursor/skills/`、`~/.qoder/skills/` 等）

**4 个都过完仍未找到** → 才可以说"机器没有 X 能力"。

---

## 5. 多 IDE 多轨扩展

| IDE | MCP 配置位置 | Skill 注册位置 |
|---|---|---|
| Claude Code | `~/.claude/settings.json` 的 `mcpServers` 字段，或 `--mcp-config` 启动参数 | `~/.claude/skills/` |
| Cursor | `~/.cursor/mcp.json` | `~/.cursor/skills/` |
| Codex（CLI / 桌面版） | `~/.codex/config.toml` 的 `[mcpServers]` | `~/.codex/skills/` |
| Qoder | 经 muxcp 聚合 / Qoder 自家 mcp.json | `~/.qoder/skills/`（如存在） |

**跨 IDE 实测**: 同一台机器上多个 IDE 通常共享 `~/.skillshub/`，各自 IDE 目录通过符号链接复用——单次 `ls ~/.skillshub/` 能看到所有"潜在可用 skill"。

---

## 6. 关联实体

- **MCP**, **Skill**, **ToolSearch** — 三个核心机制
- **Claude Code**, **Cursor**, **Codex**, **Qoder** — agent 宿主
- **`~/.claude/skills/`**, **`~/.skillshub/`** — Skill 注册目录
- **`lark-doc`** skill, **`lark-cli`** binary — 案例关联
- **`muxcp`** — MCP 聚合代理（kg-hub / mcp_search 等都在这）

---

## 7. 元规则

这条 lesson 跟 kg-hub 内部架构无关，**它是 agent 通用行为准则**。归宿 kg-hub canonical 是因为 kg-hub 的 PUSH hook 已经接通所有 IDE 的 SessionStart 注入——这是事实上让"所有 agent 在所有 IDE 都默认知道"的最优路径。

未来类似的 meta lesson（如"区分 plugin vs MCP vs Skill 三种 Claude Code 扩展"、"agent 沙盒边界自检"等）也应作为 canonical 注入。
