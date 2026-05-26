# muxcp 工具发现性问题 — 完整解决方案与安装指南

**当前状态**：🟢 **Phase 2 Hybrid Production Installed + Smoke Tested** (2026-05-22)
  — 绿色表示 Codex hybrid **生产安装与冒烟验证通过**，**不表示决策 Locked**（Locked 仍需 1 周观察 + Phase 3+4）
**版本**：v2.3 (revised 2026-05-22) — 记录生产安装、冒烟验证结果与 1 周观察期
**作者**：jingmiao@liblib.ai
**关联**：kg-hub `DESIGN.md` 决策 17

**v2.2 修订全集**：
- v2 (相对 v1)：
  - mcp_search 描述更准（区分 muxcp 路径 vs claude-mem plugin 路径）
  - `pw` 移出 v1 安装清单（未经 ephemeral 验证）—— 改为可选 add-on
  - `muxcp_fallback` 改用 wrapper（继承 env-loading 纪律，不直调二进制）
  - approval 配置补齐到所有已验证工具
  - 备份命令带时间戳
- v2.2 (相对 v2)：
  - 状态标题改"Hybrid Ephemeral Validated; Production Install Pending"——绿色不等于 Locked
  - Step 2.2 wrapper 验证改为**真 MCP initialize + tools/list smoke test**（旧的 `< /dev/null` exit code 检查不可靠）
  - Step 2.1 加显式禁令：**aliyun-only.yaml 不得复制到 WebDAV 同步目录**
  - Step 3 改为"**删除 `mcp_servers.muxcp` 命名空间下所有段落**"——避免子表残留
  - Step 3 末 `seq` pin 版本从"反应式"改为**Phase 2 安装后第一条 hardening**
  - Step 5 三个测试查询保存为**固定 prompt 集**，1 周观察期可复用
- v2.3 (相对 v2.2)：
  - 记录 2026-05-22 生产安装已完成，`~/.codex/config.toml` 已切到 hybrid
  - 记录生产 smoke test：`kg` / `seq` / `muxcp_fallback` 三条核心路径全部 PASS
  - 将 `mcp-search` 标记为 **inconclusive / blocked by user-cancelled tool approval**，不计入 hybrid 核心失败
  - 启动 1 周观察期；Locked 条件仍未满足

---

## 0. 一分钟读完

**问题**：muxcp（第三方 MCP multiplexer）把多个上游 MCP 聚合到单一命名空间 → 工具名长（70-90 字符）→ 客户端展示降级（Codex 截断 + hash）→ LLM **静默漏触发**。

**短期解（已上线，仅 Claude Code）**：SessionStart hook 注入工具索引 → LLM 主动 `ToolSearch`。

**长期解（Phase 2 已安装并冒烟通过）**：muxcp 降级为"SSE/legacy 协议适配兜底层" → stdio MCPs 走 Codex native，SSE 类 MCPs 保留 slim muxcp_fallback。

**实测**：
- 2026-05-21 Codex ephemeral 三路全通过
- 2026-05-22 Codex production install + smoke test 三路全通过

核心路径：
- `kg` native stdio
- `seq` native stdio
- `muxcp_fallback` Aliyun SSE

**当前任务**：进入 1 周观察期，重复跑固定 prompt 集，确认无静默漏触发、无 hash 截断回归、无 secrets 泄漏事故。

---

## 1. 现状速览

### 1.1 三个客户端表现差异

| 客户端 | 工具名展示 | 自动触发 | 兜底机制 |
|---|---|---|---|
| **Claude Code** | 完整长名（87 字符） | ToolSearch + deferred discovery + SessionStart hook 三层 | ✅ 已上线 |
| **Cursor** | 待实测 | 无 hook 等价机制 | ❌ |
| **Codex** | 截断 + hash（`...sls_translate_tex_1d19bb73577d`） | 无 ToolSearch | ❌ |

### 1.2 现有 `~/.codex/config.toml`（脱敏摘要）

```toml
model = "gpt-5.5"
model_reasoning_effort = "high"

[mcp_servers.muxcp]
type = "stdio"
command = "/Users/mac/.config/muxcp/run-muxcp.sh"

[mcp_servers.muxcp.tools.kg_hub__kg_stats]
approval_mode = "approve"

# ... (marketplaces, plugins, hooks — 与本次改动无关)
```

——所有 MCP 都通过这一个 muxcp 入口聚合。

---

## 2. 架构决策（DESIGN.md 决策 17 摘要）

**完整内容**：`kg-hub/DESIGN.md` § 决策 17

### 2.1 状态

🟢 **Phase 2 Hybrid Production Installed + Smoke Tested** (2026-05-22) — 生产配置已安装，三条核心路径冒烟验证通过

⚠️ **绿色 = Phase 2 生产安装与冒烟验证通过，不等于决策 Locked**。完整 Locked 仍需 1 周生产观察 + Phase 3 (SSE bridge 选项落地) + Phase 4 (完整 generator 上线) + 至少一个其他客户端迁移并跑稳——见决策 17 "Upgrade Locked 条件"全表。

### 2.2 锁定的内容

- ✅ Codex native stdio MCP 路径可行
- ✅ muxcp 角色重定义：**SSE/legacy 协议适配兜底层**（继续保留，不淘汰）
- ✅ Hybrid migration 路径采纳

### 2.3 选择

```
当前：Client → muxcp → upstream MCPs（所有请求被聚合）

目标：source.yaml + local.yaml → generator → cursor.mcp.json
                                            → claude.json
                                            → codex.config.toml
                                            → muxcp/current.yaml (fallback)
       Client → upstream MCPs（直连）
                + muxcp_fallback（仅 SSE/legacy）
```

### 2.4 实施 Phase 与当前进度

| Phase | 内容 | 状态 |
|---|---|---|
| 1 | Codex 前置验证 | ✅ 完成 (2026-05-21) |
| 2 | **Hybrid migration**（stdio native + aliyun muxcp_fallback） | 🟢 Production installed + smoke tested，**1 周观察中** |
| 3 | SSE bridge 独立评估（A/B/C/D 四选项） | ⏳ 未启动 |
| 4 | 完整 generator（配置编译器） | ⏳ 未启动 |
| 5 | Cursor / Claude Code 迁移 | ⏳ 未启动 |

---

## 3. 验证证据（双 Phase）

### 3.1 Phase 1：Codex Native 探测（2026-05-21）

📄 详细报告：`/Users/mac/workspace_codex/muxcp-codex-native-validation-2026-05-21.md`

| Gate | 结果 | 证据 |
|---|---|---|
| #1 多 server 支持 | ✅ PASS | Codex ephemeral 同挂 `kg` + `seq` 都成功 |
| #2 transport 混合 | ❌ FAIL（协议错位） | Codex `url=` 走 streamable_http；Aliyun MCP 只懂经典 SSE。**协议错位，不是 Codex 缺远程支持** |
| #3 工具名展示 | ✅ PASS (stdio) | `server: kg, tool: kg_stats`，无 muxcp 前缀，无 hash 截断 |

**关键洞察**：muxcp 在内部承担**协议适配**职责。"绕开 muxcp" 会丢这层能力——所以应保留为 fallback adapter。

### 3.2 Phase 2：Hybrid Ephemeral 验证（2026-05-21）

📄 详细报告：`/Users/mac/workspace_codex/muxcp-codex-hybrid-validation-2026-05-21.md`

| 路径 | 结果 | 证据 |
|---|---|---|
| `kg` native stdio | ✅ PASS | `{"server":"kg","tool":"kg_stats","status":"completed"}` |
| `seq` native stdio | ✅ PASS | `{"server":"seq","tool":"sequentialthinking","status":"completed"}` |
| `muxcp_fallback`（Aliyun SSE） | ✅ PASS | `aliyun_observability__sls_get_current_time` 返回 `{"current_time":"2026-05-21 22:50:31",...}` |
| `pw` native stdio | ⏸ 未单独测 | 同 stdio 协议预期可工作，安装后顺手验证 |
| `mcp_search` | N/A | 本期刻意不迁移（避免三路径冲突） |

**结论**：hybrid 架构**端到端通过**——native + fallback 在同一 Codex 会话共存且互不干扰。

### 3.3 Phase 2：Production Smoke Test（2026-05-22）

安装后按 `~/.config/ai-mcp/test-prompts/codex-hybrid-smoke.md` 跑了 4 条独立 `codex exec --ephemeral` 新会话验证。

| 路径 | 结果 | 证据 |
|---|---|---|
| `kg` native stdio | ✅ PASS | 调用 `kg.kg_stats`，返回实体数 `2848` |
| `seq` native stdio | ✅ PASS | 调用 `seq.sequentialthinking`，返回 `1+1=2` |
| `muxcp_fallback`（Aliyun SSE） | ✅ PASS | 调用 `muxcp_fallback.aliyun_observability__sls_get_current_time`，返回 `2026-05-22 13:56:48` / `1779429408` |
| `mcp-search`（claude-mem plugin） | ⚠️ INCONCLUSIVE | 新会话能看见并尝试调用 `mcp-search.search` / `observation_search` / `memory_search`，但均返回 `user cancelled MCP tool call`。该项不计入 hybrid 核心路径失败 |

**结论**：Phase 2 hybrid 生产安装核心路径冒烟验证通过。进入 1 周观察期。

**非阻塞噪声**（单独跟踪，不阻塞 Phase 2）：
- Codex 远程 plugin sync 被 Cloudflare `403` 拦截
- `claude-mem/SKILL.md` 缺 YAML frontmatter
- 新会话工具加载阶段偶发非法 UTF-8 环境变量导致 Rust panic，但未阻止三条核心 MCP 调用完成

---

## 4. 安装前行为变化预告

| 行为 | 安装前 | 安装后 |
|---|---|---|
| Codex 工具名 | `mcp__muxcp__aliyun_observability__sls_*`（87 字符，截断+hash） | `mcp__kg__kg_stats`、`mcp__seq__sequentialthinking` 等（短、干净） |
| `kg-hub` 访问 | via muxcp | native stdio |
| `sequential_thinking` | via muxcp | native stdio |
| `playwright` | via muxcp | **v1 不安装**（未经 ephemeral 验证）—— 见 §5 Step 3.5 可选 add-on |
| `aliyun_observability` | via muxcp（全聚合） | via muxcp_fallback wrapper（slim） |
| **`mcp_search`** | 双路径：via muxcp **+** via `claude-mem@claude-mem-local` plugin | ⚠️ 失去 muxcp 这一路；**claude-mem plugin 的 `mcp-search` 仍挂载**（已在你 `~/.codex/config.toml` 确认） |

⚠️ **关于 `mcp_search`**：通过 muxcp 的路径会断；但 claude-mem plugin 自带的 `mcp-search` 仍然可用——所以 Codex 不会完全失去 mcp_search 能力，只是工具触发签名变了。建议安装前跑一次 `codex mcp list --json` 确认 claude-mem 的 `mcp-search` 在列表里。

---

## 5. 生产安装指南

### Step 0：预检查

```bash
# 确认这些文件/路径存在
ls ~/.codex/config.toml                              # 现有 Codex 配置
ls ~/.config/muxcp/bin/run-kg-hub.sh                 # kg-hub MCP 启动脚本
ls ~/.local/bin/muxcp                                # muxcp 二进制
```

### Step 1：备份（必须，时间戳防重复执行覆盖）

```bash
TS=$(date +%Y%m%d-%H%M%S)
BAK=~/.codex/config.toml.bak.${TS}-pre-hybrid
cp ~/.codex/config.toml "$BAK"
echo "Backup: $BAK"
shasum -a 256 ~/.codex/config.toml "$BAK"
```

**记住 `$BAK` 的完整路径**——下面 Step 3 的注释里要回填、Step 6 回滚也要用。

### Step 2.1：创建 slim muxcp config

新建文件 `/Users/mac/.config/muxcp/aliyun-only.yaml`：

```bash
cat > /Users/mac/.config/muxcp/aliyun-only.yaml <<'EOF'
# Slim muxcp config for Codex hybrid migration.
# 用途：作为 muxcp_fallback 后端，仅承载需要协议适配的 SSE/legacy MCPs。
# 当前只挂 aliyun_observability（经典 SSE）。
# 创建依据：kg-hub DESIGN.md 决策 17 Phase 2 / 验证日期 2026-05-21。
transport: stdio
servers:
  - name: aliyun_observability
    transport: sse
    url: "http://192.168.10.113:18081/sse"
EOF
```

⚠️ **本文件不得复制到 WebDAV 同步根**（`/Users/mac/public-sync/cc-switch-sync/...`）。它含本机内网 IP，属于"local-only"层。如果未来为了多设备同步把它放回 WebDAV，secrets/local 分层（DESIGN.md 决策 17 关键约束）会被破坏。

设备 B 需要类似配置时，**单独在设备 B 上重新执行 Step 2.1**——内网 IP 可能本来就不同。

### Step 2.2：创建 muxcp_fallback wrapper（继承 env-loading 纪律）

⚠️ 不要让 Codex 直调 `muxcp` 二进制——会绕过 `local.env` 加载（当前 local.env 没敏感 token，但保留此纪律避免未来加 token 时事故）。

```bash
cat > /Users/mac/.config/muxcp/run-muxcp-aliyun-only.sh <<'EOF'
#!/usr/bin/env bash
# Wrapper for muxcp_fallback (Codex hybrid migration / kg-hub DESIGN 决策 17 Phase 2).
# Mirrors run-muxcp.sh env-loading pattern; uses slim aliyun-only.yaml.
set -euo pipefail

LOCAL_ENV="$HOME/.config/muxcp/local.env"
CONFIG_PATH="$HOME/.config/muxcp/aliyun-only.yaml"
MUXCP_BIN="$HOME/.local/bin/muxcp"

if [ -f "$LOCAL_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$LOCAL_ENV"
  set +a
fi

exec "$MUXCP_BIN" -config "$CONFIG_PATH"
EOF
chmod +x /Users/mac/.config/muxcp/run-muxcp-aliyun-only.sh
```

**Smoke test wrapper**（发真 MCP initialize + tools/list，验证是否能正常工作）：

```bash
/Users/mac/.pyenv/shims/python3 <<'PY'
import json, subprocess, sys, time

proc = subprocess.Popen(
    ['/Users/mac/.config/muxcp/run-muxcp-aliyun-only.sh'],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)
msgs = [
    {'jsonrpc':'2.0','id':1,'method':'initialize','params':{
        'protocolVersion':'2024-11-05','capabilities':{},
        'clientInfo':{'name':'wrapper-smoke','version':'0'}}},
    {'jsonrpc':'2.0','method':'notifications/initialized','params':{}},
    {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},
]
try:
    out, err = proc.communicate(
        '\n'.join(json.dumps(m) for m in msgs) + '\n',
        timeout=20,
    )
except subprocess.TimeoutExpired:
    proc.kill(); out, err = proc.communicate()

# 解析响应（每行一个 JSON）
tools = []
for line in out.splitlines():
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r.get('id') == 2 and 'result' in r:
        tools = [t.get('name') for t in r['result'].get('tools', [])]

print(f'returned {len(tools)} tools, sample:', tools[:3])
assert any('sls' in t or 'aliyun' in t.lower() for t in tools), \
    f'expected aliyun/sls tools, got: {tools}'
print('OK — wrapper works, aliyun tools reachable')
PY
```

期望输出含 `sls_*` / `aliyun_*` 类工具名 + `OK`。失败常见原因：
- 网络不通到 `192.168.10.113:18081`（Tailscale 没起）
- `local.env` 损坏导致 `set -a` 失败
- muxcp 二进制版本与 aliyun-only.yaml schema 不兼容

### Step 3：修改 `~/.codex/config.toml`（v1 = 3 个已验证 server）

#### Step 3.1：先扫所有 `mcp_servers.muxcp.*` 段（避免子表残留）

⚠️ **不要只手动替换"看得见的那两段"**——未来 `[mcp_servers.muxcp.tools.*]` 子表可能不止 `kg_hub__kg_stats`。**删除整个 `mcp_servers.muxcp` 命名空间的所有段**才是干净操作。

先扫一遍，看实际有哪些段需要删：

```bash
grep -nE '^\[mcp_servers\.muxcp(\.|\])' ~/.codex/config.toml
```

期望输出（当前已知）：
```
4:[mcp_servers.muxcp]
8:[mcp_servers.muxcp.tools.kg_hub__kg_stats]
```

**如果有更多行**（例如 `[mcp_servers.muxcp.tools.XXX]`），它们**全部要删**——hybrid 安装后 `muxcp` 这个 namespace 不再存在，留任何子表都是"指向不存在的父表"的脏配置。

#### Step 3.2：删除 + 替换

**删除所有以 `[mcp_servers.muxcp` 开头的段及其内容**（从段标题到下一个段标题之前）。最容易出错处：每段后的空行也要一并清理，避免文件出现连续空行。

在原 muxcp 段的位置**插入**以下内容（**先把 `<BACKUP-PATH>` 替换成 Step 1 输出的 `$BAK`**）：

```toml
# === Hybrid migration installed YYYY-MM-DD (kg-hub DESIGN decision 17 Phase 2) ===
# Previous: single [mcp_servers.muxcp] aggregating all upstream MCPs.
# Now: stdio MCPs (kg, seq) native; Aliyun via slim muxcp_fallback wrapper.
# Playwright (pw) deliberately deferred — not ephemeral-validated yet (see Step 3.5).
# Validated 2026-05-21 ephemeral; reports in workspace_codex/.
# Rollback: cp <BACKUP-PATH> ~/.codex/config.toml

[mcp_servers.kg]
type = "stdio"
command = "/Users/mac/.config/muxcp/bin/run-kg-hub.sh"

[mcp_servers.kg.tools.kg_stats]
approval_mode = "approve"

[mcp_servers.seq]
type = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-sequential-thinking"]

[mcp_servers.seq.tools.sequentialthinking]
approval_mode = "approve"

[mcp_servers.muxcp_fallback]
type = "stdio"
command = "/Users/mac/.config/muxcp/run-muxcp-aliyun-only.sh"

[mcp_servers.muxcp_fallback.tools.aliyun_observability__sls_get_current_time]
approval_mode = "approve"
```

**其他段保持不动**：`model` / `model_reasoning_effort` / `[marketplaces.*]` / `[features]` / `[plugins.*]` / `[hooks.state.*]` / `[projects.*]` 全部原样保留。

⚠️ **关于 `npx -y` 的供应链波动**：

`npx -y @modelcontextprotocol/server-sequential-thinking` 每次启动会从 npm 拉取最新版本，受上游更新影响。

**Phase 2 安装后第一条 hardening**（不是"如果出问题再说"，是计划必做）：

| 方案 | 改动 | 推荐度 |
|---|---|---|
| Pin 版本 | `args = ["-y", "@modelcontextprotocol/server-sequential-thinking@<具体版本号>"]` | ⭐ 最简单，先用这个 |
| 本地全局安装 | `npm i -g @modelcontextprotocol/server-sequential-thinking`，再 `command = "mcp-server-sequential-thinking"` | 更稳，但要维护本机安装 |
| 本地 wrapper | 写个 `run-seq.sh`，内部 pin 版本 + 处理 cache | 最灵活，过度工程 |

**操作建议**：本次 v1 安装为了匹配 ephemeral 验证命令暂保留 `npx -y`；**安装跑稳一两天后立刻 pin 版本**（查当前 npm `@latest` 版本号填进去）。等 1 周观察期结束时，`npx -y` 不应再出现在生产配置里。

### Step 3.5（可选）：加入 Playwright

⚠️ **`pw` 未在 Phase 2 ephemeral 验证范围内**——v1 安装清单刻意不含它。

如果你要装，先用 ephemeral 单独验证 Playwright MCP：

```bash
codex exec --ephemeral --ignore-user-config \
  -c 'default_tools_approval_mode="approve"' \
  -c 'mcp_servers.pw.command="npx"' \
  -c 'mcp_servers.pw.args=["-y","@playwright/mcp"]'
# 提示："用 pw 打开 https://example.com 并截图"，看是否成功调用 browser_navigate / browser_take_screenshot
```

通过则在 Step 3 的 TOML 里追加：

```toml
[mcp_servers.pw]
type = "stdio"
command = "npx"
args = ["-y", "@playwright/mcp"]

[mcp_servers.pw.tools.browser_navigate]
approval_mode = "approve"
```

（同样的 `npx -y` 供应链注意事项适用）

### Step 4：验证 TOML 合法性

```bash
# 用 python 校验 TOML 语法（macOS 系统 python3 是 3.9，没有 tomllib——下面用 tomli）
/Users/mac/.pyenv/shims/python3 -c "
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open('/Users/mac/.codex/config.toml', 'rb') as f:
    cfg = tomllib.load(f)
servers = list(cfg.get('mcp_servers', {}).keys())
print('mcp_servers:', servers)
expected = {'kg', 'seq', 'muxcp_fallback'}        # v1 不含 pw
assert expected.issubset(set(servers)), f'missing: {expected - set(servers)}'
assert 'muxcp' not in servers, 'old muxcp still present!'
print('OK — v1 install valid')
"
```

如果 Python tomllib/tomli 都不可用，跳过本步 → 直接 Step 5 用 codex 实际测试。

### Step 5：新会话验证

先确认 server 列表对齐：

```bash
codex mcp list --json | /Users/mac/.pyenv/shims/python3 -c "
import json, sys
data = json.load(sys.stdin)
names = [s.get('name') for s in (data if isinstance(data, list) else data.get('servers', []))]
print('codex 看到的 server：', names)
print()
print('期望含：kg / seq / muxcp_fallback / mcp-search (claude-mem plugin)')
print('期望不含：muxcp (已被 hybrid 替代)')
"
```

**固定测试 prompt 集**（安装时 + 1 周观察期复用）：

| # | Prompt | 期望工具调用 | 期望关键返回字段 |
|---|---|---|---|
| 1 | "查 kg-hub 的实体总数" | `server: kg, tool: kg_stats` | `entities`, `edges`, `episodes` |
| 2 | "用 sequentialthinking 推理一下 1+1" | `server: seq, tool: sequentialthinking` | `thoughtNumber`, `nextThoughtNeeded` |
| 3 | "用 SLS 查阿里云当前时间" | `server: muxcp_fallback, tool: aliyun_observability__sls_get_current_time` | `current_time`, `current_timestamp` |
| 4 (可选) | "之前讨论过 fastembed 问题吗" | `claude-mem` plugin 的 `mcp-search` | 返回相关 observation/episode |

把这 4 条 prompt **存成本机文件**，方便后续观察期复用：

```bash
mkdir -p ~/.config/ai-mcp/test-prompts
cat > ~/.config/ai-mcp/test-prompts/codex-hybrid-smoke.md <<'EOF'
# Codex Hybrid 安装/观察期固定测试 prompt 集

1. kg native stdio:        查 kg-hub 的实体总数
2. seq native stdio:       用 sequentialthinking 推理一下 1+1
3. muxcp_fallback SSE:     用 SLS 查阿里云当前时间
4. (可选) mcp-search:      之前讨论过 fastembed 问题吗

每次跑这 4 条对比工具触发是否正确 / 工具名是否截断 / 调用是否静默漏触发。
EOF
```

1-3 都通过 → v1 安装成功。第 4 条用于确认 claude-mem plugin 的 `mcp-search` 没受 muxcp 路径下线连带影响。

---

## 6. 回滚步骤

如果出问题（工具调用失败、token 异常、Codex 不识别 server 等）：

```bash
# 找到 Step 1 的备份（按时间倒排列出所有备份）
ls -t ~/.codex/config.toml.bak.*-pre-hybrid | head -5

# 恢复（用最近一次）
LATEST=$(ls -t ~/.codex/config.toml.bak.*-pre-hybrid | head -1)
cp "$LATEST" ~/.codex/config.toml
echo "Restored from: $LATEST"
# 重启 codex
```

**slim aliyun-only.yaml 和 run-muxcp-aliyun-only.sh 不需要删**——下次重新安装可直接复用。

---

## 7. 安装后观察清单（1 周）

| 检查项 | 标准 |
|---|---|
| Codex 工具列表 | 稳定显示 3 个 hybrid server（`kg` / `seq` / `muxcp_fallback`）+ 原有 `claude-mem` plugin 工具（含 `mcp-search`） |
| 工具名展示 | 不再出现 hash 截断（除 muxcp_fallback 下的 aliyun 工具，仍是长名但限定在 fallback 命名空间） |
| 静默漏触发 | 跑 §3.1 风格的请求（"用 SLS 查 XX"）能稳定触发 muxcp_fallback |
| Secrets / 凭证 | aliyun_observability 仍正常连接（验证 wrapper 读 local.env 没断）—— 即使 local.env 当前没敏感 token，这条纪律值得验证一次 |
| `mcp-search`（claude-mem plugin） | 仍可触发——确认 muxcp 路径断开后没有连带影响到 claude-mem plugin 的 `mcp-search` |
| `npx -y` 拉新版稳定性 | `seq` 偶尔失败可疑似 npm 上游变化——若发生，按 §5 Step 3 末尾说明 pin 版本 |

2026-05-22 已启动 1 周生产观察期。若一周稳定 → 满足 DESIGN.md 决策 17 "Upgrade Locked 条件 #2"，可启动 Phase 3 (SSE bridge 评估)。

⚠️ **注意**：一周稳定只满足 Locked 的**其中一项**条件，**不等于决策 17 进入 Locked 状态**。完整 Locked 还需 Phase 3 (SSE bridge 选项落地) + Phase 4 (完整 generator 上线) + 至少一个其他客户端（Cursor 优先）迁移并跑稳——见决策 17 "Upgrade Locked 条件"全表。

---

## 8. 当前操作状态

✅ **已安装并冒烟通过**（2026-05-22）：
- `~/.codex/config.toml` 已从单 `muxcp` 聚合入口切换到 hybrid
- `kg` native stdio 调用 `kg_stats` 成功
- `seq` native stdio 调用 `sequentialthinking` 成功
- `muxcp_fallback` 调用 Aliyun `sls_get_current_time` 成功

⚠️ **仍需观察**：
- `mcp-search` 通过 claude-mem plugin 路径可见，但冒烟验证被 `user cancelled MCP tool call` 阻塞，当前状态为 inconclusive
- `seq` 仍使用 `npx -y @modelcontextprotocol/server-sequential-thinking`，安装跑稳一两天后需要 pin 版本
- 非阻塞噪声（Cloudflare `403` plugin sync、`claude-mem/SKILL.md` frontmatter、非法 UTF-8 env panic）单独跟踪

🔁 **回滚条件**：
- 任一核心路径（`kg` / `seq` / `muxcp_fallback`）在日常使用中持续失败
- Codex 工具名/hash 截断问题没有改善
- 发现 secrets/local-only 文件误同步

回滚命令：

```bash
cp /Users/mac/.codex/config.toml.bak.20260522-124700-pre-hybrid ~/.codex/config.toml
```

---

## 9. 后续工作（Phase 3-5）

### Phase 3：SSE bridge 评估（0.5-1 工作日）

**触发条件**：Phase 2 安装且跑稳 1 周。

四个备选方案：

| 选项 | 工程成本 | 优势 | 风险 |
|---|---|---|---|
| A. classic SSE → stdio bridge | 中 | 完全 native 化 aliyun | 多一个进程要维护 |
| B. classic SSE → streamable_http bridge | 高 | 协议向新转型 | 收益长期 |
| C. 继续 muxcp_fallback | 0 | 已有方案 | 永远多一层间接 |
| D. 推动 Aliyun MCP 加 streamable_http | 不可控 | 根本解决 | 时间不可控 |

### Phase 4：完整 generator（3-5 工作日）

**仅在 Phase 3 落地后**，再做配置编译器：

| 子命令 | 用途 |
|---|---|
| `generator generate` | 读 source.yaml + local.yaml → generated/ |
| `generator validate` | schema 校验、引用解析、alias collision 检查 |
| `generator install --dry-run` | diff 预览（必须先跑） |
| `generator install` | 应用到客户端正式位置（显式确认） |
| `generator rollback` | 从上次 install 备份恢复 |
| `generator doctor` | 健康检查 |

### Phase 5：Cursor / Claude Code 迁移

按 Phase 4 完成后陆续推进。Claude Code 已有 SessionStart hook 兜底，不急。

---

## 10. 相关文件索引

| 文件 | 状态 | 角色 |
|---|---|---|
| `kg-hub/DESIGN.md` § 决策 17 | 🟢 Hybrid VALIDATED | 内部架构决策档案 |
| `kg-hub/docs/muxcp-discoverability-2026-05-21.md` | 🟢 完整 ADR（9 轮迭代） | 历史演进 + 完整设计 |
| `kg-hub/docs/codex.hybrid.preview.toml` | 🟢 EPHEMERAL VALIDATED | 预览配置（与本文 §5 Step 3 等价） |
| `kg-hub/docs/muxcp-resolution-and-install.md` | 📌 **本文档** | 决策 + 安装一体化 |
| `~/.claude/muxcp-index.md` | 🟢 运行中 | Claude Code 短期兜底索引 |
| `~/.claude/settings.json` SessionStart hook | 🟢 运行中 | 索引自动注入 |
| `~/.codex/config.toml` | 🟢 Hybrid installed + smoke tested | 生产 Codex 配置 |
| `~/.codex/config.toml.bak.20260522-124700-pre-hybrid` | 🟢 已创建 | 安装前回滚备份 |
| `~/.config/muxcp/aliyun-only.yaml` | 🟢 已创建（local-only） | slim muxcp config（Step 2.1） |
| `~/.config/muxcp/run-muxcp-aliyun-only.sh` | 🟢 已创建并验证 | muxcp_fallback wrapper（继承 env 加载，Step 2.2） |
| `~/.config/ai-mcp/test-prompts/codex-hybrid-smoke.md` | 🟢 已创建 | 固定测试 prompt 集（安装时 + 1 周观察期复用，Step 5） |
| `workspace_codex/muxcp-codex-native-validation-2026-05-21.md` | 🟢 已存档 | Phase 1 实测证据 |
| `workspace_codex/muxcp-codex-hybrid-validation-2026-05-21.md` | 🟢 已存档 | Phase 2 实测证据 |
| `workspace_codex/muxcp-hybrid-validation-aliyun-only.yaml` | 🟢 已存档 | Phase 2 验证用 slim config |

---

## 附录 A：工具名长度对比

```
现状（muxcp 全聚合）：
  mcp__muxcp__aliyun_observability__sls_translate_text_to_sql_query   (87 字符)

Hybrid 安装后（本文 §5）：
  mcp__kg__kg_stats                                                   (17 字符)
  mcp__seq__sequentialthinking                                        (28 字符)
  mcp__muxcp_fallback__aliyun_observability__sls_*                    (~50 字符，仍长但限定在 fallback)

理想（远期，Phase 3 SSE bridge 后）：
  mcp__obs__sls_query                                                 (~19 字符)
```

## 附录 B：决策演进 12 轮回顾

| 轮次 | 关键贡献 |
|---|---|
| 1 | 初版方案：Hook + 短名 + alias |
| 2 | 拆 facade、多 MCP server、命名优化 |
| 3 | 修正"已内置 alias"过度推断 |
| 4 | 短 server name 是性价比最高的立刻可做项 |
| 5 | Level 4（绕开 muxcp）是真正最优解 |
| 6 | secrets 物理隔离、generator.py 进同步、SSE 风险标 P0 |
| 7 | Level 4 Scope 边界、generator 拆子命令、Gate #3 量化 Benchmark、外发脱敏 |
| 8 | **Codex Phase 1 实测**——Gate #2 协议错位证伪"全面 native"；muxcp 角色重定义 |
| 9 | **Codex Phase 2 Hybrid 实测**——三路全通过；状态升级 VALIDATED |
| 10 | **生产安装评审 (2026-05-22)**——v1→v2：mcp_search 描述精准化、pw 移出 v1、muxcp_fallback 加 wrapper、approval 补齐、备份带时间戳 |
| 11 | **生产安装收紧 (2026-05-22)**——v2→v2.2：状态标题改 "Ephemeral Validated; Production Install Pending"（绿色不等于 Locked）；wrapper 验证改真 MCP initialize+tools/list；Step 3 改 TOML-aware 删除（grep 扫所有子表）；aliyun-only.yaml 加 WebDAV 禁令；seq pin 版本升为第一条 hardening；测试 prompt 集存盘可复用 |
| 12 | **生产安装 + 冒烟验证 (2026-05-22)**——v2.2→v2.3：正式写入 `~/.codex/config.toml`；`kg` / `seq` / `muxcp_fallback` 三条核心路径 PASS；`mcp-search` 标为 inconclusive；进入 1 周观察期 |

第 8-9 轮**用真实数据替代了纯架构推断**，第 10-11 轮**把 ephemeral 验证版打磨到生产可执行版**，第 12 轮完成生产安装与冒烟验证。本文档收口于第 12 轮。

## 附录 C：muxcp 当前 config（参考，本次安装不动它）

```yaml
# /Users/mac/public-sync/cc-switch-sync/mcp/muxcp/current.yaml（WebDAV 同步）
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

Hybrid 安装后这份配置**不再被 Codex 引用**（被 slim aliyun-only.yaml 替代），但 Claude Code 仍可经原 muxcp 使用全部 5 个 server——所以 mcp_search 在 Claude Code 里还能用。

---

**END OF DOCUMENT**
