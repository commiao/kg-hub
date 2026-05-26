# muxcp 跨客户端 MCP 工具发现性问题 — 诊断、方案对比与架构决策

**日期**：2026-05-21
**作者**：jingmiao@liblib.ai
**关联决策**：kg-hub `DESIGN.md` 决策 17（Proposed / Pending validation）
**状态**：临时缓解已上线（Claude Code），长期方案待 Codex 实测后决定

---

## TL;DR

`muxcp`（第三方本地 MCP multiplexer）把多个上游 MCP 聚合到单一命名空间，导致三个层级的问题：

- 工具名长（70-90 字符），LLM 注意力稀释
- 客户端展示降级（Codex 端会截断 + 加 hash 后缀）
- LLM **静默漏触发**——用户和 LLM 都不知道某工具没被调用

**短期方案**（Claude Code 已上线、实测通过）：SessionStart hook 注入 muxcp 索引，让模型在用户提到 "SLS" 等关键词时主动 `ToolSearch` 加载工具 schema。

**长期方案**（Proposed）：引入中立 schema (`source.yaml`) 作为**配置 source of truth**；把 muxcp 从"运行时网关"降级为"可生成目标之一 + fallback gateway"；generator 输出各客户端的原生 MCP 配置，请求路径不再经过 muxcp。

---

## 1. 问题陈述

### 1.1 现象（按严重度递减）

**A. 静默漏触发**（最严重）

用户期望自动起作用的能力被悄悄跳过。例：

- 用户："查一下 Liblib 线上日志"
- LLM：直接回答"没有对应工具"或胡编日志格式
- 实际：muxcp 里有 18 个 `aliyun_observability` 工具，但 LLM 没意识到

失败无声，**用户事后才发现漏调**。

**B. 显式调用失败**

用户明确说"用 aliyun SLS 查 XX"，LLM 回"找不到该 MCP"——其实在 muxcp 子命名空间里。

**C. Codex 工具名展示降级**

```
原名:   mcp__muxcp__aliyun_observability__sls_translate_text_to_sql_query  (87 字符)
Codex:  aliyun_observability__sls_translate_tex_1d19bb73577d              (截断 + hash)
```

hash 后缀让 LLM 无法稳定记忆工具名，进一步降低自动调用率。

### 1.2 根因分析

| 因素 | 影响 |
|---|---|
| 工具名平均 70-90 字符，前缀冗余 (`mcp__muxcp__<server>__<tool>`) | LLM 注意力稀释 |
| 单一 `muxcp` 命名空间聚合 5 个上游、约 80 个工具 | "看到 muxcp 不知道里面有啥" |
| 客户端工具名长度限制 → 截断 + hash | 工具名不稳定不可读 |
| Claude Code 的 ToolSearch / deferred discovery 是客户端特性 | Cursor / Codex 没有等价机制 |
| 描述里的"触发关键词"无法跨客户端可靠传递 | 自动触发不可控 |

---

## 2. 用户场景

- **设备**：Mac (M-series)
- **客户端**：Claude Code（主力）、Cursor、Codex、OpenClaw（VPS）
- **muxcp 上游 MCP 数量**：5 个
  - `sequential_thinking` / `playwright` / `aliyun_observability` / `mcp_search` / `kg_hub`
- **总工具数**：~80
- **muxcp 配置同步**：WebDAV (`/Users/mac/public-sync/cc-switch-sync/mcp/`)

---

## 3. 调研过程

### 3.1 muxcp 二进制 + 配置实测

```bash
# 二进制：Mach-O arm64，CLI 仅暴露 -config
$ /Users/mac/.local/bin/muxcp --help
Usage of /Users/mac/.local/bin/muxcp:
  -config string

# 当前配置：30 行 YAML，仅 servers 列表，无 alias/profile/hide 字段
$ wc -l /Users/mac/public-sync/cc-switch-sync/mcp/muxcp/current.yaml
30

# 二进制 strings 探测：含 alias/prefix/mapping 关键字
$ strings ~/.local/bin/muxcp | grep -iE '^(alias|prefix|tools?|mapping)$' | sort -u
Alias / Prefix / Tool / Tools / alias / mapping / prefix / tools
```

**注意**：二进制里出现 alias / prefix 关键字**不能证明**功能已实现并可用——Go 二进制里这些词可能来自依赖、MCP SDK 或通用结构体。需要 maintainer 确认。

### 3.2 临时方案实测（Claude Code SessionStart hook）

1. 在 `~/.claude/settings.json` 注入 SessionStart hook
2. hook 用 `/usr/bin/python3` 把 `~/.claude/muxcp-index.md`（人工维护的索引 + 触发关键词）作为 `hookSpecificOutput.additionalContext` 注入
3. 新会话开头，索引自动出现在 system-reminder 里

**实测验证**（用户在新会话发起"用 SLS 查 Liblib 报错"）：

- ✅ Claude Code 主动调 `ToolSearch select:mcp__muxcp__aliyun_observability__sls_*`
- ✅ 正确识别 SLS 工具组
- ✅ 用 `sls_list_projects` 模糊匹配跨区域找到 `liblibai-arms-log`
- ✅ 用户提问中**未提及 `muxcp` 三字**，模型自动想到去 muxcp 里找

**局限**：只对 Claude Code 有效。Cursor / Codex 没有 SessionStart 等价机制。

---

## 4. 方案频谱

按"成本 - 受益 - 普适性"排列：

| Level | 方案 | 成本 | 受益 | 普适性 |
|---|---|---|---|---|
| 0 | 什么都不做 | 0 | 0 | — |
| 1 | SessionStart hook | 30 min | 高（Claude Code） | ❌ 单客户端 |
| 2 | 缩短 muxcp server name | 30 sec | 中（38% 字符省） | ✅ 跨客户端 |
| 3 | 等 muxcp upstream 加 alias | 0 | 高（如实现） | ✅ 跨客户端 |
| 4 | **绕开 muxcp 请求路径，只用配置同步** | 5-8 小时 | **极高** | ✅ 跨客户端 |
| 5 | 完全替换 muxcp | 不确定 | 不确定 | ✅ 跨客户端 |

**Level 4 是架构最优解**（见 §5 决策）。Level 1（已上线）+ Level 4（Proposed）是当前最务实组合。

---

## 5. 架构决策（kg-hub DESIGN #17 摘要）

> 完整决策记录见 `/Users/mac/workspace_claudeCode/kg-hub/DESIGN.md` § 决策 17

### 5.1 状态

🟢 **Proposed / Hybrid path VALIDATED (2026-05-21)** —— ephemeral 三路全通过，待生产安装（仍不是 Locked：Locked 需 Phase 3+4 完成）

**真正锁定**（更新于 2026-05-21 Codex 验证后）：
- ✅ Codex native stdio MCP 路径可行（多 server 共存 + 工具名展示干净均已实测）
- ✅ **muxcp 角色重定义**：从"全聚合运行时网关"→ **"SSE/legacy 协议适配兜底层"**
- ✅ **Hybrid migration 路径采纳**：stdio MCP 走 Codex native，SSE/经典协议 MCP 保留 muxcp_fallback

**未锁定**：
- ❌ 不锁定"全面迁移 native"（被 SSE 协议错位证伪）
- ❌ 不锁定"删除 muxcp"（muxcp 是协议适配层，长期保留）
- ❌ 不锁定 SSE bridge 具体实现方案
- ❌ 不锁定 schema 终稿

### 5.2 选择

把 muxcp 从"运行时网关"降级为"可生成目标"之一。引入中立 schema 作为 source of truth，generator 按 client 输出原生配置。

```
当前：Client → muxcp → upstream MCPs

目标：source.yaml + local.yaml → generator → cursor.mcp.json
                                            → claude.json
                                            → codex.config.toml
                                            → muxcp/current.yaml (fallback)
       Client → upstream MCPs（直连，无中间层）
```

**Scope（本决策不解决什么）**：

- ✅ 解决：muxcp 聚合带来的 namespace bloat（`mcp__muxcp__<server>__<tool>` → `mcp__<server>__<tool>`）
- ❌ **不解决：上游 MCP 工具自身命名差的问题**。Level 4 后 `sls_translate_text_to_sql_query` 仍叫这个名字——per-tool alias 是独立问题，需要 muxcp upstream 加 alias 能力，或客户端层（如 SessionStart hook 描述里加触发关键词）单独解决

> Level 4 removes muxcp-added namespace bloat, but does not replace per-tool aliasing if upstream tools are poorly named.

### 5.3 中立 Schema 草案

```yaml
# /Users/mac/public-sync/cc-switch-sync/mcp/source.yaml （WebDAV 同步）
schema_version: 1
servers:
  - id: obs
    display: Aliyun Observability
    transport: sse
    url_ref: aliyun_observability_url   # 引用 local.yaml，不存明文
    clients: [codex, cursor, claude]

  - id: kg
    display: kg-hub
    transport: stdio
    command_ref: kg_hub_command
    clients: [claude, codex]
```

```yaml
# ~/.config/ai-mcp/local.yaml （不进 WebDAV）
aliyun_observability_url: "http://192.168.10.113:18081/sse"
kg_hub_command: "/Users/mac/.config/muxcp/bin/run-kg-hub.sh"
```

### 5.4 目录结构（物理隔离 secrets）

```
WebDAV 同步根（公开）：
  /Users/mac/public-sync/cc-switch-sync/mcp/
  ├── source.yaml      # 中立 schema，全设备共享
  └── generator.py     # 生成器源码（多设备一致性 > 冲突风险）

本机私有（不进任何同步）：
  ~/.config/ai-mcp/
  ├── local.yaml       # 本机 URL / 路径 / 非敏感 override（不放 token）
  ├── secrets.env      # token / API key（**所有敏感凭证唯一存放点**）
  └── generated/       # 生成产物，集中存放后再安装/链接
      ├── cursor.mcp.json
      ├── claude.mcp.json
      ├── codex.config.toml
      └── muxcp/current.yaml
```

**关键原则**：
- **靠物理路径隔离 secrets**，不靠 ignore 规则或文件名约定（WebDAV/Synology Drive 的 ignore 实现不统一）
- **token 永远不放在任何 `.yaml` 里**——避免"yaml 里偶尔有 secret"成为习惯

**Generator 执行约束（供应链防护）**：

`generator.py` 同步进 WebDAV → 变成"会被同步且会被执行的代码"——同步链被污染就有供应链风险。强制约束：

| # | 约束 | 实现要求 |
|---|---|---|
| 1 | 执行前**强制 diff 预览** | `generator install` 必须先 `--dry-run` 输出 diff，让人类确认 |
| 2 | **不读取** `secrets.env` 内容 | generator 只检查 key 是否存在、**不读值**；secrets 由 wrapper/MCP 进程自己读 env |
| 3 | 版本受控 | git 管理 + 脚本头标 `__version__` + sha256 自检（启动时校验文件未被同步污染） |
| 4 | `install` 必须显式确认 | 默认输出到 staging 目录，不自动覆盖客户端正式配置 |
| 5 | 不带网络 IO | 纯本地变换 YAML → JSON/TOML，禁止任何 HTTP / Git / npm / pip 调用 |

### 5.5 前置验证（Gate to Locked）

| # | 验证点 | 通过标准 |
|---|---|---|
| 1 | 多 server 支持 | Codex 同时挂 2-3 个独立 MCP 全部可见 |
| 2 | **transport 混合** | stdio + SSE/HTTP 能在同一份 Codex 配置共存（**最大风险**；不支持需 generator emit stdio bridge） |
| 3 | 触发率提升（**量化**） | 见下方 Benchmark；目标：native 模式正确调用率 ≥ muxcp 模式 × 1.5 |

任一不通过 → 决策回到 Re-evaluate，**不进入实施**。

**Benchmark 设计**（不能凭主观判断"是否改善"）：

固定 prompt 集，每个 prompt 在 muxcp 模式 + native 模式各跑 3 次，对比指标。

| 类别 | 数量 | 示例 prompt |
|---|---|---|
| SLS 日志 | 10 | "查最近 1 小时 Liblib ERROR 日志"、"用 SQL 查包含 timeout 的日志" 等 |
| ARMS trace | 10 | "查最近的慢调用"、"找一下错误链路 trace"、"分析这个 trace 的瓶颈" 等 |
| Browser (Playwright) | 5 | "打开页面截图"、"模拟点击 form"、"等待元素出现" 等 |
| kg-hub | 5 | "之前讨论过 fastembed 问题吗"、"查 OpenClaw 相关知识" 等 |

每次记录：

- ✅ / ❌ 是否调用正确 MCP（**核心指标**）
- 工具名是否截断 / 加 hash
- 是否先请求缺失参数（vs 胡编参数）
- 是否误调用其他 MCP
- 失败 / 拒绝执行 / 胡编次数

**通过标准**：native 模式的"正确调用率"在所有类别上均 ≥ muxcp 模式 × 1.5。

### 5.5.1 Validation Results（2026-05-21 实测）

📄 详细报告：`/Users/mac/workspace_codex/muxcp-codex-native-validation-2026-05-21.md`

| Gate | 结果 | 证据 |
|---|---|---|
| #1 多 server 支持 | ✅ **PASS** | Codex ephemeral 同挂 `kg` + `seq` 两个 stdio MCP，均成功调用（`kg.kg_stats` 返回 `{entities:2734, edges:6407, episodes:1328}`） |
| #2 transport 混合 | ❌ **FAIL（协议错位，已识别根因）** | Codex `url=` 走 **streamable_http**；Aliyun MCP 只懂**经典 SSE**——握手失败 `Method Not Allowed`（错误由 Aliyun 服务端返回）。**这是协议错位，不是 Codex 缺远程 MCP 支持** |
| #3 工具名展示 | ✅ **PASS (stdio path)** | native 调用显示为 `server: kg, tool: kg_stats`，无 muxcp 长前缀、无 hash 截断 |
| Benchmark | ⏸ 未完成 | Gate #2 失败导致 native 模式无法覆盖 aliyun 类请求，Benchmark 因此未跑完整 30 条 prompt 集 |

**关键架构发现**：

`muxcp` 能正常服务 Aliyun，是因为它在内部承担了**协议适配**职责（经典 SSE ↔ MCP 客户端通用协议）。这意味着：

- ❌ 不能简单"绕开 muxcp"——会丢失协议适配能力
- ✅ `muxcp` 角色应**重定义为"SSE/legacy 协议适配兜底层"**——而不是被淘汰
- ✅ 长期方案需要**独立的 SSE bridge 设计**（见 §8.2 Phase 3 评估表）

**对实施计划的影响**：原计划"一次性 native generator"路径不可行；改为 **hybrid migration**（见 §8.2）。

### 5.5.2 Phase 2 Hybrid Validation Results（2026-05-21 ephemeral 实测）

📄 详细报告：`/Users/mac/workspace_codex/muxcp-codex-hybrid-validation-2026-05-21.md`

| 路径 | 结果 | 证据 |
|---|---|---|
| `kg` native stdio | ✅ **PASS** | `{"server":"kg","tool":"kg_stats","status":"completed"}` |
| `seq` native stdio | ✅ **PASS** | `{"server":"seq","tool":"sequentialthinking","status":"completed"}` |
| `muxcp_fallback`（Aliyun SSE） | ✅ **PASS** | `aliyun_observability__sls_get_current_time` 返回 `{"current_time":"2026-05-21 22:50:31","current_timestamp":1779375031}` |
| `pw` native stdio | ⏸ 未单独测 | 同 stdio 协议预期可工作，正式安装时一并确认 |
| `mcp_search` | N/A | 本期刻意不迁移 |

**结论**：hybrid 架构**端到端验证通过**——native + fallback 在同一 Codex 会话共存且互不干扰。可进入"生产安装"阶段。

### 5.6 Secrets 解析时机

| Transport | 解析时机 | 实现 |
|---|---|---|
| stdio | MCP 进程/wrapper 启动时从本机 env 读 | client config 只负责启动 command；wrapper 或 MCP 自身读 env，**不依赖客户端 env 替换** |
| SSE/HTTP | Generator 时硬替换 URL | URL 必须在配置里；生成文件 100% 在本机私有目录 |

### 5.7 升级 Locked 的条件

全部满足才升级：

1. ✅ 前置验证三项全通过
2. ✅ Generator 上线后 Codex native 跑稳 1 周
   - 工具列表稳定显示多个独立 server
   - 工具名截断/hash 消失或显著减少
   - 同类请求多次连续触发正确工具
   - 无 secrets 泄漏事故
3. ✅ 至少一个其他客户端（Cursor 优先）跑稳 3 天

任一未达 → 维持 Proposed 状态。

---

## 6. 已实施的临时缓解（Claude Code）

| 改动 | 位置 | 状态 |
|---|---|---|
| muxcp 速查索引 | `~/.claude/muxcp-index.md` (2.3 KB) | ✅ |
| SessionStart hook | `~/.claude/settings.json` | ✅ |
| 实测 | 新会话发起 SLS 查询 → 自动触发 | ✅ |

**hook 配置**：

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "/usr/bin/python3 -c 'import json,os; print(json.dumps({\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":open(os.path.expanduser(\"~/.claude/muxcp-index.md\")).read()}}))'",
        "timeout": 5
      }]
    }]
  }
}
```

每个会话开头注入 ~600 token 索引，hook 每次都读最新文件（无需重启）。

**索引文件内容结构**（节选）：

```markdown
# muxcp 子 MCP 速查索引

## 默认行为协议
1. 用户提到下方任一关键词，先用 ToolSearch 加载对应工具 schema 再回答
2. 调用 muxcp 工具失败时必须显式报告，禁止无声放弃
3. 当前 runtime worker：mcp_search 的 memory_search 不可用，走 plugin_claude-mem 替代

## 命名空间索引

### 阿里云可观测性 — mcp__muxcp__aliyun_observability__*
**触发词**：阿里云、SLS、ARMS、CMS、日志、trace、调用链、慢调用、错误链、火焰图、PromQL、应用监控、Liblib 线上排查
**核心工具**：sls_list_projects / sls_translate_text_to_sql_query → sls_execute_sql_query / ...

### 知识图谱（kg-hub） — mcp__muxcp__kg_hub__*
...
```

---

## 7. 反馈分发（按对象拆分）

诉求按收件人拆分。**muxcp maintainer 和 MCP 客户端团队是不同对象**，混在一起会让对方抓不到核心诉求。

### 7.1 给 muxcp maintainer 的反馈

**Feature Request**: Support tool aliases, hide rules, and shorter exposed names

**P0**：
1. **Document complete config schema**——当前 README 未明确列出 alias/prefix/hide 支持情况；若有，请文档化；若无，请明确说明
2. **Per-tool alias + 全局唯一性校验**——⚠️ alias **必须全局唯一**，启动时冲突直接报错（否则部署会埋雷）
3. **Hide/exclude tools**——大量低频工具稀释 LLM 注意力

**P1**：
4. Examples 字段或子工具：每个 server 注册若干高质量调用范例（对 LLM 工具选择决策帮助巨大）

**P2**：
5. Profiles / multi-instance 部署文档

**建议接口**：

```yaml
servers:
  - name: aliyun_observability
    expose_as: sls          # 暴露的 namespace 名（必须全局唯一）
    tools:
      aliases:               # 工具级别名（叠加 expose_as 后必须全局唯一）
        sls_translate_text_to_sql_query: generate_query
        sls_execute_sql_query: query
      hide:
        - sls_get_current_time
```

最终工具名变成 `sls__generate_query` / `sls__query`，跨客户端均稳。

**兼容性建议**：alias 期间旧名可选保留（"optionally keep original names alongside aliases"），不强制硬切。

### 7.2 给各 MCP 客户端团队（Cursor / Codex / Claude Code）的反馈

**Issue**: Tool discoverability degrades when MCP servers expose long namespaced tool names

当上游 MCP 通过 multiplexer 聚合时，最终工具名会变长（例：`mcp__muxcp__aliyun_observability__sls_translate_text_to_sql_query`，87 字符）。客户端的处理方式直接决定 LLM 工具选择能否成功：

| 客户端 | 当前表现 | 期望改进 |
|---|---|---|
| **Codex** | 截断 + hash 后缀（`aliyun_observability__sls_translate_tex_1d19bb73577d`），LLM 无法稳定记忆 | 提高工具名长度上限，或允许保留语义后缀的智能截断 |
| **Cursor** | 待实测——是否截断、是否做发现性辅助？ | 至少与 Claude Code 持平的工具名展示能力 |
| **Claude Code** | ToolSearch + deferred discovery + SessionStart hook 三层兜底，体验最好 | 维持现状；建议把 deferred / SessionStart 模式形成标准化协议供其他客户端参考 |

**对所有客户端的通用建议**：
- 长工具名场景下保留**人类可读后缀**，避免截断后变成 hash
- 提供 MCP 发现 hint 机制（类似 Claude Code 的 ToolSearch / additionalContext 注入）

---

## 8. 当前状态与下一步

### 8.1 已完成（内部架构记录）
- ✅ Claude Code SessionStart hook 上线并实测通过
- ✅ 内部架构决策已记录（kg-hub project, DESIGN.md 决策 17, **Proposed / 部分验证通过** 状态）
- ✅ **Phase 1 Codex 验证执行完毕（2026-05-21，partial pass，详见 §5.5.1）**
- ✅ **Phase 2 Hybrid ephemeral 验证通过（2026-05-21，三路全通过，详见 §5.5.2）**
- ✅ `codex.hybrid.preview.toml` 已生成于 `docs/`
- ✅ 本反馈文档完成

### 8.2 实施 Phase（更新于 2026-05-21 Codex 验证后）

| Phase | 内容 | 状态 | 预估 |
|---|---|---|---|
| 1 | Codex 前置验证 | ✅ **已完成 2026-05-21**（partial pass，详见 §5.5.1） | — |
| 2 | **Hybrid migration**：stdio MCP（`kg` / `seq` / 可能 `playwright`）原生化；aliyun_observability 暂留精简版 muxcp_fallback | 🟢 **Ephemeral validated 2026-05-21**，待生产安装（详见 §5.5.2） | 1-2 工作日 |
| 3 | **SSE bridge 独立设计**：评估四个选项（见下方），选定后实施 | 待启动 | 0.5-1 工作日（评估） + 实施另算 |
| 4 | 完整 generator（**仅在 hybrid 跑稳后**） | 远期 | 3-5 工作日 |
| 5 | Cursor / Claude Code 迁移 | Phase 4 完成后陆续 | — |

#### Phase 2 落地顺序（重要）

**不要直接改 `~/.codex/config.toml`**。按以下顺序：

1. ✅ 更新 DESIGN.md 决策 17（本次会话完成）
2. ✅ 更新本文档（§5.1 / §5.5.1 / §8.2）
3. ⏳ 生成 `codex.hybrid.preview.toml` 预览文件
4. ⏳ `codex exec --ephemeral --ignore-user-config -c '...'` 再验证一次（与 Phase 1 ephemeral 同样手法）
5. ⏳ 最终安装到 `~/.codex/config.toml`

#### Phase 2 注意事项

- ⚠️ `mcp_search` 是否原生化**要谨慎**——当前已有 `plugin_claude-mem_mcp-search` 和 `muxcp__mcp_search` 两条路径，再加 codex native 会成三条，**容易混乱**。建议 Phase 2 阶段**不动 `mcp_search`**，等路径收敛策略明确后再处理
- aliyun_observability **暂保留 muxcp_fallback**，不直接配 Codex 远程 url

#### Phase 3: SSE Bridge 备选评估

| 选项 | 工程成本 | 优势 | 风险 |
|---|---|---|---|
| A. classic SSE → stdio bridge（本地小进程） | 中（自写 / 找现成方案） | 完全 native 化 aliyun，工具名干净 | 多一个进程要维护 |
| B. classic SSE → streamable_http bridge | 高 | 协议向新转型 | Codex/Cursor 长期都向 streamable_http 倾斜 |
| C. 继续 muxcp_fallback | 0 | 已有方案 | 永远多一层间接 |
| D. 推动 Aliyun MCP 加 streamable_http endpoint | 不可控 | 根本解决 | 时间不可控，依赖上游 |

#### Phase 4: Generator 实现（仅在 hybrid 跑稳后）

⚠️ Generator 不是普通脚本，是**小型配置编译器**——承担多客户端兼容、secret 引用解析、本机路径差异、transport 差异、安装/链接生成产物、回滚、diff 预览、schema migration。**必须拆为子命令**：

| 子命令 | 用途 |
|---|---|
| `generator generate` | 读 source.yaml + local.yaml，输出到 `~/.config/ai-mcp/generated/` |
| `generator validate` | schema 校验、引用解析、alias collision 检查 |
| `generator install --dry-run` | 输出 diff 预览（**必须先跑**） |
| `generator install` | 把 generated/ 应用到客户端正式位置（**显式确认**） |
| `generator rollback` | 从上次 install 的备份恢复 |
| `generator doctor` | 健康检查：secrets 是否齐、所有 ref 是否能解析、目标客户端是否可写 |

### 8.3 外发前脱敏要求

本文档含**内部信息**，不能原样外发：

| 需脱敏内容 | 处理建议 |
|---|---|
| 作者邮箱 | 改成职位描述或 GitHub handle |
| 本机绝对路径（`/Users/mac/...`） | 改成 `~` 或 `<user-home>/...` |
| WebDAV 路径 | 改成 `<sync-root>/...` |
| 内网地址（`192.168.x.x`） | 改成 `<internal-server>` |
| kg-hub 内部决策编号 | 删除或改成"internal architecture decision" |
| Claude Code hook 完整 JSON 实现 | 给 muxcp maintainer 时简化为"a SessionStart hook injects a tool index"；不必全代码 |
| Liblib 业务场景 | 改成"a typical multi-MCP observability scenario" |

**建议拆分外发**：

- **给 muxcp maintainer**：脱敏版的 §1, §3.1, §7.1 + 附录 B
- **给 MCP 客户端团队**（Cursor / Codex / Claude Code）：脱敏版的 §1, §7.2 + 最小复现步骤
- **内部参考**：原文（本文档）

不要混发——三类对象关注点不同，混在一起反而抓不到重点。

---

## 9. 附录

### A. 工具名长度对比

```
Level 0 (现状):     mcp__muxcp__aliyun_observability__sls_translate_text_to_sql_query   (87 字符)
Level 2 (短名):     mcp__muxcp__obs__sls_translate_text_to_sql_query                    (61 字符)
Level 2 加 client:  mcp__m__obs__sls_translate_text_to_sql_query                        (54 字符)
Level 3 (alias):    mcp__muxcp__obs__sls_generate_query                                 (47 字符)
Level 4 (native):   mcp__obs__sls_generate_query                                        (39 字符)
最理想:             obs__sls_generate_query                                             (27 字符)
```

### B. muxcp 当前 config（脱敏）

```yaml
transport: stdio

servers:
  - name: sequential_thinking
    transport: stdio
    command: npx
    args: [-y, "@modelcontextprotocol/server-sequential-thinking"]

  - name: playwright
    transport: stdio
    command: npx
    args: [-y, "@playwright/mcp"]

  - name: aliyun_observability
    transport: sse
    url: "http://<internal-ip>:18081/sse"

  - name: mcp_search
    transport: stdio
    command: "${HOME}/.config/muxcp/bin/claude-mem-proxy.py"

  - name: kg_hub
    transport: stdio
    command: "${HOME}/.config/muxcp/bin/run-kg-hub.sh"
```

### C. 决策演进六轮迭代

| 轮次 | 来源 | 关键贡献 |
|---|---|---|
| 1 | 初版方案 | Hook + 短名 + alias |
| 2 | 第一次反馈 | 拆 facade、多 MCP server、tool 命名优化 |
| 3 | 第二次反馈 | 修正"已内置 alias"过度推断、加 per-client 区分 |
| 4 | 第三次反馈 | 短 server name 是性价比最高的立刻可做项 |
| 5 | 第四次反馈 | Level 4（绕开 muxcp）是真正最优解 |
| 6 | 第五次反馈 | secrets 物理隔离、generator.py 进同步、SSE 风险标 P0 |
| 7 | 第六次 ADR 评审 | Level 4 Scope 边界、generator 拆子命令 + 供应链防护、Gate #3 量化 Benchmark、外发脱敏 |
| 8 | **Codex Phase 1 实测 (2026-05-21)** | Gate #2 协议错位证伪"全面 native"；muxcp 角色重定义为"SSE/legacy 协议适配兜底"；hybrid migration 路径采纳 |
| 9 | **Codex Phase 2 Hybrid Ephemeral 实测 (2026-05-21)** | 三路（kg native + seq native + muxcp_fallback Aliyun SSE）端到端全通过，架构验证完成；状态升级为 Hybrid path VALIDATED |

每一轮迭代都修正了上一轮的盲点。第 8-9 轮**用真实数据替代了纯架构推断**，是质变。

### D. 参考资料

- Claude Code SessionStart hook docs: `~/.claude/settings.json` schema 参考
- MCP protocol spec: https://modelcontextprotocol.io/

---

**END OF DOCUMENT**
