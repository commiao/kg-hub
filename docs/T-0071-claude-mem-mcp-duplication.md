# T-0071 前置调研结论：claude-mem mcp-search 双链路重复

- 调研日期：2026-09-17
- 上游任务：T-0070（muxcp 进程乘法治理）
- 状态：**已执行**（2026-09-17 21:10），待客户端重启回收存量进程

---

## 一句话

`mcp_search` 这一套工具现在挂了两条链路、两条都在跑。关掉插件那条，
16 个进程 / 353MB 降到 1 个 / ~25MB，功能不丢。唯一硬伤是插件升级会让开关静默复活。

---

## 1. 我们有自有 fork

| 目录 | origin | 说明 |
|---|---|---|
| `~/workspace_codex/claude-mem-fork-t0064` | `git@github.com:commiao/claude-mem.git` | **正牌 fork**，upstream=thedotmack，branch `codex/t0064-observer-reliability`，HEAD `805c6ca2`，0 未提交 |
| `~/workspace_codex/claude-mem-slot-fairness` | **本地** `~/.claude/plugins/marketplaces/thedotmack` | branch `fix/t0021-observer-slot-fifo`，**3 处未提交** —— 动手前需先处理 |
| `~/.claude/plugins/marketplaces/thedotmack` | `https://github.com/thedotmack/claude-mem.git` | 是 git repo，但 remote 指向 **upstream**。即：我们线上跑的是上游，不是 fork |
| `~/workspace_claudeCode/claude-mem`、`~/workspace_cursor/claude-mem-fork-push`、`~/workspace_codex/claude-mem-runtime-fix-20260910` | — | 不是 git repo（副本/解压目录） |

意义：改 wrapper 从"改别人的插件"变成"改自己的代码"，跟 muxcp fix 同一套流程。
但下面会看到，有比改 wrapper 更干净的路。

---

## 2. wrapper 是什么（T-0071 问题 ①）

声明位置：`plugin/.mcp.json` → `mcpServers["mcp-search"]`

```json
{ "type": "stdio", "command": "node", "args": ["-e", "<压缩 JS>"] }
```

那段 JS 是**版本发现器**：

1. 组装 6 个候选根目录（`CLAUDE_PLUGIN_ROOT` / cwd / codex cache / claude cache / marketplace），
   按版本号倒序，跳过带 `.orphaned_at` 标记的目录
2. 找第一个含 `scripts/mcp-server.cjs` 的根
3. `c.spawn(process.execPath, [cjs], { stdio: 'inherit' })` 拉起
4. 转发 `SIGTERM/SIGINT/SIGHUP`，透传退出码，然后**一直活着**

关键：`stdio: 'inherit'` 意味着 MCP 的 stdin/stdout 由子进程直接继承，
**数据根本不经过 wrapper**。它活着只为了转发信号和退出码 —— 是纯监护壳，纯浪费。

生成源：`scripts/build-hooks.js:116`（build-hooks 还有校验，见 :733-739，
强制要求这段 launcher 必须包含 codex/claude 两处 cache fallback）。
fork 与 marketplace 两份 `.mcp.json` **逐字节一致**。

---

## 3. 能不能本地改（问题 ②）—— 能，而且有官方开关

`src/services/worker/http/routes/SettingsRoutes.ts`

```ts
// :33-34  路由
app.get ('/api/mcp/status', this.handleGetMcpStatus.bind(this));
app.post('/api/mcp/toggle', validateBody(toggleMcpSchema), this.handleToggleMcp.bind(this));

// :259  实现
private toggleMcp(enabled: boolean): void {
  const mcpPath         = path.join(packageRoot, 'plugin', '.mcp.json');
  const mcpDisabledPath = path.join(packageRoot, 'plugin', '.mcp.json.disabled');
  if (enabled && existsSync(mcpDisabledPath))  renameSync(mcpDisabledPath, mcpPath);
  else if (!enabled && existsSync(mcpPath))    renameSync(mcpPath, mcpDisabledPath);
}
```

claude-mem **自带**"关掉我自己的 MCP"开关，受支持、非 hack。
实现就是把 `plugin/.mcp.json` 改名成 `.mcp.json.disabled`（Claude Code / Codex 扫不到就不注册）。

---

## 4. 会不会被升级覆盖（问题 ③）—— **会。这是唯一硬伤**

两个原因：

1. `toggleMcp()` **只做 rename，不写 settings.json** —— 没有任何持久化的"用户想关掉"标记
2. 文件在**版本号目录**下：`cache/thedotmack/claude-mem/13.24.23/.mcp.json`
   升到 13.25.x 会新建目录、带全新 `.mcp.json`

结果：升级后开关自动复活，而且**静默**。

对策必须是硬机制，不能靠"记得检查"（试金石：模型不做/做错，状态还对吗）。
建议做法见第 7 节。

---

## 5. 意外收获：mcp_search 是双链路重复

两条链路，同一套工具，**都在跑**：

| 链路 | 路径 | 工具前缀 |
|---|---|---|
| muxcp | muxcp 常驻实例 → `~/.config/muxcp/bin/claude-mem-proxy.py` → `mcp-server.cjs` | `mcp__muxcp__mcp_search__*` |
| 插件 | 客户端 → `node -e` 版本发现壳 → `mcp-server.cjs` | `mcp__plugin_claude-mem_mcp-search__*` |

`claude-mem-proxy.py` 是那段压缩 JS 发现逻辑的 **Python 重写版**，候选路径列表逐条对应。

muxcp 链路已实测可用：
- `list_corpora` → `[]`（正常空响应，握手通）
- `search("muxcp 进程乘法治理")` → 62 条真实结果（45 obs / 8 sessions / 9 prompts）

---

## 6. 实测进程账（2026-09-17 20:40）

7 对「壳 + 本体」，外加 2 个 Python 拉起的 cjs：

| 宿主 | 壳 pid | 壳 RSS | cjs pid | cjs RSS |
|---|---|---|---|---|
| Cursor Helper | 9426 | 5MB | 9427 | 17MB |
| codex | 54336 | 12MB | 54348 | 24MB |
| Application | 63410 | 12MB | 63423 | 24MB |
| Application | 64022 | 12MB | 64051 | 24MB |
| Application | 66778 | 12MB | 66803 | 24MB |
| codex | 74962 | 31MB | 74977 | 46MB |
| codex | 75290 | 29MB | 75303 | 51MB |
| **muxcp (python3)** | — | — | 9603 | 7MB |
| **muxcp (Python)** | — | — | 69559 | 24MB |

- 合计 **16 进程 / 353MB**
- 其中插件链路 **14 进程 / 322MB 可回收**
- 剩余 2 个 python 链路 cjs（31MB），待 Cursor / Claude.app 重启后收敛到 1 个 ≈24MB

---

## 7. 结论与建议

### 方案（方向 A 改良版）

不改 wrapper，直接用官方开关关掉插件侧 MCP 声明，全部走 muxcp 单实例。

**16 进程 / 353MB → 1 进程 / ~25MB**

需要同时处理 3 份 `.mcp.json`：

```
~/.codex/plugins/cache/claude-mem-local/claude-mem/13.24.23/.mcp.json
~/.claude/plugins/cache/thedotmack/claude-mem/13.24.23/.mcp.json
~/.claude/plugins/marketplaces/thedotmack/plugin/.mcp.json
```

### 遗留风险与对策

| 风险 | 对策 | 状态 |
|---|---|---|
| **单点**：mcp_search 全押 muxcp 常驻实例 | launchd `KeepAlive=true` + `mcp-guard` deep_probe 实调兜底 | 已有（T-0070） |
| **升级静默复活**（问题 ③） | `mcp-guard` 增加检查项 `plugin_mcp_resurrected`：发现 cache 目录下 `.mcp.json` 复活 → 告警 + 自动改名回 `.disabled` | **待做** |
| slot-fairness 工作区 3 处未提交，origin 指向本地 marketplace | 动 marketplace 目录前先处理 | **待做** |

### 为什么不选"改 wrapper 直连 cjs 绝对路径"

改了同样会被升级覆盖（同一个版本号目录），省的只是 7 个壳 112MB，
而关掉整条链路省 322MB。成本一样，收益差 3 倍。

---

## 附：调研中发现的其它问题

- **task-hub `task_show` / `task_log` 全部报 `错误: 'id'`**（服务端 KeyError），
  `task_list` 正常。本文档因此未能写入 T-0071 任务日志。需单独排查。


---

# 执行记录（2026-09-17 21:05-21:12）

## 执行前追加核实（用户质疑「claude-mem 与 muxcp 不是一个事情」引出）

用户的质疑成立，且逼出一个我原本没验的决定性问题。澄清与结论：

- claude-mem 干三件事：**采集**(hooks) / **存储**(worker+sqlite) / **搜索**(mcp-server.cjs)
- muxcp 只干一件事：把多个 MCP 服务器汇总成一个入口
- 本次处理只涉及**第三件**：搜索服务器这一个零件被注册了两遍

三项验证（全部通过）：

| 验证点 | 方法 | 结论 |
|---|---|---|
| 关掉会不会影响记忆采集 | `grep -c mcp plugin/hooks/hooks.json` | **0 次**，采集全是 `type=command` shell，不碰 MCP |
| 搜索服务器能否多客户端共用 | 扫 mcp-server.cjs 的 `process.env.*` | 只读 5 个全局项（CONFIG_DIR / DATA_DIR / SERVER_PORT / WORKER_PORT / WORKER_SCRIPT_PATH），**不读会话号、不读项目路径**；`mcp-server.ts:381` 要求 `session_start_context` 每次调用传 `project` → **无会话绑定，共用安全** |
| 两条链路是否同一份代码 | `ps` 解析实际加载路径 | 9 个 cjs 全是 **13.24.23**，仅分属 codex / claude 两个 cache 目录 |

同时修正了两处先前的错误陈述：
1. **有两个 muxcp 在跑**：pid 9431（Cursor Helper 拉起，用旧的 `current.yaml`）+ pid 69493（launchd，`shared.yaml`）。前者是 T-0070 残留，Cursor 重启即消失，**未强杀**（强杀会让 Cursor 当场失去 MCP）
2. **进程仍在增长**：调研期间 16/353MB → 执行前 18/385MB，Codex 又新起一个壳（pid 79257）

`claude-mem-proxy.py` 的 `filter_message()` 经查只剔除一个名为 `__IMPORTANT` 的工具，其余原样透传 —— 不影响那 15 个真工具，两条链路对等。

## 实际动作

1. **关闭三份插件侧 MCP 声明**（沿用官方 `.disabled` 后缀，可逆）
   ```
   ~/.codex/plugins/cache/claude-mem-local/claude-mem/13.24.23/.mcp.json.disabled
   ~/.claude/plugins/cache/thedotmack/claude-mem/13.24.23/.mcp.json.disabled
   ~/.claude/plugins/marketplaces/thedotmack/plugin/.mcp.json.disabled
   ```
   marketplace 是 git repo，工作区因此变脏（`D plugin/.mcp.json` + `?? plugin/.mcp.json.disabled`），属预期。

2. **写入意图文件** `~/.config/mcp-guard/claude-mem-mcp-disabled`
   claude-mem 官方 toggle 不持久化意图，这个文件补上，作为 reconciler 的唯一依据。

3. **mcp-guard 加防复活自愈**（414 → 496 行，备份 `.bak-20260917-210634`）
   - `plugin_mcp_candidates()` — 枚举所有可能位置，含**未来的新版本目录**
   - `reconcile_plugin_mcp()` — 意图存在则 `os.replace()` 改回 `.disabled`
   - `cmd_check()` **先自愈再采样**；自愈按「事件」上报，不走边沿触发（每次都报）
   - `sample()` / `status` 保持只读，便于排查
   - 自愈失败（权限/占用）才计为异常 `plugin_mcp_resurrected`
   - 阈值 `cjs_procs` 12 → **4**（目标态 1）

4. **实测自愈**：伪造一次版本升级复活 → check 输出
   `🔧 plugin_mcp_autohealed: ...已自动关闭 1 处`，文件改回 `.disabled`，
   `alerts.log` 留痕，原文件内容未损坏。

## 执行后核验

| 项 | 结果 |
|---|---|
| muxcp 链路搜索 | ✅ 返回 62 条真实结果（含本次会话 21:06 的观测） |
| 插件完整性 | ✅ plugin.json / hooks.json / package.json 在，20 skills、9 scripts |
| 采集 worker | ✅ pid 33640，已连续运行 5 天 19 小时，未受影响 |
| 三份声明 | ✅ 均为 `.disabled` |

## 尚未回收：存量 18 进程 / 385MB

已注册的进程属于**运行中的会话**，强杀会让那些会话当场失去工具。
新会话不再产生。回收时机：

- Codex（ChatGPT.app）：4 对 —— 用户任务跑完后重启
- Claude.app：3 对 —— 重启
- Cursor：1 对 + 那个残留 muxcp —— 重启后一并消失

重启完预期：claude-mem 搜索进程 **18 → 1**，muxcp 实例 **2 → 1**。
