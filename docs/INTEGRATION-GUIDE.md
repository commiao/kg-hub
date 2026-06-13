# kg-hub 跨平台 / 跨工具对接手册

> 让 **同一个用户** 在 **不同设备(Mac / Windows / Linux)** 和 **不同工具(Claude Code / Cursor / Qoder / Codex / OpenClaw)** 上,都接到 **自己的那一套中央 kg-hub**,实现跨设备、跨工具的统一知识图谱。
>
> 适用版本:server 含 `/api/canonical_context`(2026-06-12 起);PUSH hook 已 HTTP 化(纯 `urllib`,客户端零额外依赖)。

---

## 0. 先理解三件事

**① 每个用户一套自己的中央仓库。**
kg-hub 是「中央知识图谱」服务,跑在一台 **常开** 的机器上(推荐 NAS / 轻量服务器)。每个用户部署 **属于自己的** 一套(自己的 falkordb + server + token)。你所有的设备和工具,都只是 **客户端**,统一指向你自己的 `KG_HUB_URL`。换一个用户 = 换一套 URL + token,数据互不串。

**② 对接有三个「面」,按需选用(可叠加):**

| 面 | 用途 | 谁用 | 依赖 |
|---|---|---|---|
| **HTTP API** | 最通用:检索 / 写入 / 健康检查 | 任何能发 HTTP 的工具、脚本、CI | 无(curl 即可) |
| **MCP server** | 在 IDE 里直接调用 `kg_search` / `kg_add_episode` 等工具 | Claude Code / Cursor / Qoder / Codex 等支持 MCP 的 | 需克隆仓库 + venv |
| **PUSH hook** | SessionStart 自动把相关 canonical 内容注入上下文(无需 LLM 记得查) | 支持会话启动 hook 的工具 | 纯 `python3`(stdlib) |

**③ 跨设备靠私有网。**
各设备和中央机用 **Tailscale**(或同类)组到一个私网,`KG_HUB_URL` 填中央机的 tailscale IP(例:`http://100.123.208.32:8080`)。这样无论你在公司、家里还是外网,接的都是同一套图。**绝不把 token 放进 URL query,绝不把服务暴露公网。**

```
        你的设备(任意 OS / 任意工具)
   Mac笔记本   Windows台式   Linux服务器
      │            │             │
      └──── Tailscale 私网 ───────┘
                   │  HTTP(:8080, Bearer token)
                   ▼
        你的中央 kg-hub(常开 NAS/服务器)
        falkordb + server + ingester + watchdog
```

---

## 1. 前置:部署你自己的中央仓库(每用户一次)

在常开机器上(NAS / VPS / 一台老电脑):

1. 克隆仓库,`docker compose -p kg-hub up -d`(含 `falkordb` + `kg_hub_server` + `ingester` + `watchdog`)。
2. 设两个秘密:`FALKORDB_PASSWORD`、`KG_HUB_API_TOKEN`(随机长串)。
3. 装 Tailscale,记下本机 tailscale IP → 这就是别的设备要填的 `KG_HUB_URL` 主机。
4. 验证:`curl http://<tailscale-ip>:8080/health` → `{"status":"ok"}`。

> 详细部署/持久化/监控见仓库 `docs/incident-retrospective.md` 的「最终架构」一节。

---

## 2. 通用客户端配置(所有 OS、所有工具的公共第一步)

所有客户端都从一个 env 文件读连接信息。**约定路径:用户主目录下 `.claude-mem/.env`**(与 claude-mem 共用,省一份配置)。

```ini
# ==== 必填:连接你自己的中央 kg-hub ====
KG_HUB_URL=http://100.123.208.32:8080      # 你中央机的 tailscale IP:端口
KG_HUB_API_TOKEN=<你的-token>

# ==== 选填:仅当客户端要直连 falkordb(MCP 读)时需要 ====
KG_HUB_FALKORDB_HOST=100.123.208.32
KG_HUB_FALKORDB_PORT=6379
KG_HUB_FALKORDB_PASSWORD=<你的-falkordb-密码>

# ==== 选填:客户端连不上时推飞书告警 ====
KG_HUB_FEISHU_WEBHOOK=<你的-webhook>
```

| OS | env 文件路径 |
|---|---|
| macOS / Linux | `~/.claude-mem/.env` |
| Windows | `%USERPROFILE%\.claude-mem\.env`(即 `C:\Users\<你>\.claude-mem\.env`) |

> **推荐对接顺序**:先 HTTP(验证连通)→ 再挂 PUSH hook(自动注入,性价比最高)→ 需要交互式图查询再加 MCP。

---

## 3. 按操作系统的差异

绝大多数差异只是 **路径** 和 **定时器**。命令逻辑一致。

### macOS
- env:`~/.claude-mem/.env`;python:系统 `python3` 即可(PUSH hook 纯 stdlib)。
- 定时(如同步本机 claude-mem.db 到中央):`launchd`(`~/Library/LaunchAgents/*.plist`,`StartInterval`)。
- 传文件到群晖 NAS:**用 `cat | ssh "cat > tmp && mv -f tmp dst"` 管道,别用 scp**(macOS 新版 scp 走 SFTP,群晖 sshd 默认未开 → `subsystem request failed`)。

### Linux
- env:`~/.claude-mem/.env`;python:`python3`。
- 定时:`systemd --user` timer 或 `crontab -e`。
- 传文件:`scp`/`rsync` 一般可用;目标是群晖时同样建议 `cat|ssh` 管道。

### Windows
- env:`%USERPROFILE%\.claude-mem\.env`;python:`py -3` 或 `python`(装官方 Python,勾选 PATH)。
- 路径用反斜杠或加引号;hook 命令里写绝对路径 `C:\...\python.exe C:\...\kg_push_hook.py --format <tool>`。
- 定时:任务计划程序(Task Scheduler)。
- 传文件:WSL 里用 `cat|ssh`;或直接走 HTTP `/api/ingest` 免文件传输。
- 连通性:`curl.exe`(Win10+ 自带)或 PowerShell `Invoke-RestMethod`。

---

## 4. 按工具对接

> 每个工具最多三步:**① 填 env(见 §2)→ ② 挂 MCP(可选)→ ③ 挂 PUSH hook(可选,强烈推荐)**。

### 4.1 Claude Code(Mac / Win / Linux)

**MCP**(在 `~/.claude/settings.json`,Windows 在 `%USERPROFILE%\.claude\settings.json`):
```json
{
  "mcpServers": {
    "kg-hub": {
      "command": "/path/kg-hub/.venv/bin/python",
      "args": ["/path/kg-hub/mcp_server.py"]
    }
  }
}
```
> Windows 把 command 换成 venv 里的 `python.exe`,args 用 Windows 路径。MCP 暴露 `kg_search / kg_episode_search / kg_node_neighbors / kg_path_between / kg_stats / kg_add_episode`。

**PUSH hook**(同一 `settings.json` 的 `hooks`):
```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "startup|resume",
        "hooks": [ { "type": "command", "timeout": 10,
          "command": "python3 /path/kg-hub/tools/kg_push_hook.py --format claude" } ] }
    ]
  }
}
```

### 4.2 Cursor

**MCP**(`~/.cursor/mcp.json`):同 §4.1 的 `mcpServers.kg-hub` 写法。

**PUSH hook**:Cursor 的 `beforeSubmitPrompt` 不消费 hook stdout,真正注入机制是 **写规则文件**。PUSH hook 用 `--format cursor` 运行时,会把 canonical 内容写到当前工程的 `<cwd>/.cursor/rules/kg-hub-canonical.mdc`(`alwaysApply: true`),Cursor 每轮自动加载。触发方式:让 claude-mem 的 cursor 适配器或一个轻量 wrapper 在会话开始时跑:
```bash
python3 /path/kg-hub/tools/kg_push_hook.py --format cursor
```

### 4.3 Qoder

Qoder 是支持 MCP 的 AI IDE,按 **「MCP + 规则文件」** 套路接(同 Cursor 思路):
- **MCP**:在 Qoder 的 MCP 配置里加 `kg-hub` server(command/args 同 §4.1)。
- **自动注入**:若 Qoder 支持工程级 always-apply 规则文件,用 `--format cursor` 产出的 `.mdc`,或直接 `--format text` 把内容写进 Qoder 的规则/记忆文件。
- **兜底**:无论是否支持 hook,都能用 §5 的 HTTP API 在对话里手动检索。

### 4.4 Codex CLI

- **MCP**(`~/.codex/config.toml`):
  ```toml
  [mcp_servers.kg-hub]
  command = "/path/kg-hub/.venv/bin/python"
  args = ["/path/kg-hub/mcp_server.py"]
  ```
- **PUSH hook**:仓库已备 `plugin/hooks/codex-hooks.json`(`SessionStart` → `kg_push_hook.py --format codex`)。Codex 通过 **plugin/marketplace** 机制加载 hook,需把 kg-hub 注册为本地插件(详见 `docs/codex-push-integration.md`)。MCP 多上游聚合/发现性问题见 `docs/muxcp-resolution-and-install.md`。

### 4.5 OpenClaw

OpenClaw 不是 IDE,是「胶囊」知识系统,走 **写入** 路径:
- 把胶囊(`capsule-*.md` / `CAPSULE-*.md`,≥1500B)放进中央机的摄入源目录,或直接 `POST /api/ingest`。
- 中央机的 `ingester` 循环按命名约定 + 水位线去重,自动把新胶囊抽取入图(LLM 在中央机跑,客户端只传小文件)。
- 跨设备:各设备的 OpenClaw 胶囊定期同步到中央机(`cat|ssh` 管道或 HTTP ingest)即可。详见 `docs/openclaw-push-integration.md`。

---

## 5. HTTP API 速查(任意工具/脚本通用)

所有请求带 `Authorization: Bearer $KG_HUB_API_TOKEN`(`/health` 除外)。

```bash
# 健康检查
curl $KG_HUB_URL/health

# 语义检索(边事实)
curl -H "Authorization: Bearer $TOKEN" "$KG_HUB_URL/api/search?q=tailscale&num_results=5"

# 取某工程相关的 canonical 上下文(并自动累计使用量;bump=0 只读不计数)
curl -H "Authorization: Bearer $TOKEN" "$KG_HUB_URL/api/canonical_context?kw=kg-hub&top_n=3&bump=0"

# 写入一条 episode(幂等:同 source 不重复)
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "$KG_HUB_URL/api/ingest" \
  -d '{"name":"my-note","episode_body":"...正文...","source_description":"manual","source_obs_id":"uniq-1"}'

# 摄入队列健康(监控用)
curl -H "Authorization: Bearer $TOKEN" "$KG_HUB_URL/api/queue_stats"
```
> Windows 用 `curl.exe` 或 PowerShell `Invoke-RestMethod -Headers @{Authorization="Bearer $env:TOKEN"}`。

---

## 6. 同一用户、跨设备 / 跨工具:一致性约定

1. **所有设备的 `.env` 指向同一个 `KG_HUB_URL` + 同一个 token** → 全部聚合进你自己的中央图,天然跨设备、跨工具。
2. **写入两条路**:
   - 交互产生的工程记忆 → 各设备本机 claude-mem.db,定期同步到中央机由 ingester 摄入;
   - 主动沉淀 → 直接 `POST /api/ingest`(带稳定的 `source_obs_id` 保证幂等,不会重复)。
3. **读取统一**:任意工具用 MCP `kg_search` 或 HTTP `/api/search` 都查同一张图。
4. **安全红线**:token 只放 `.env`(不进 URL query、不写进图);服务只在私网(Tailscale)可达,不暴露公网;客户端绝不直接拿别人的 token。

---

## 7. 验证与故障速查

| 现象 | 排查 |
|---|---|
| 连不上 | `tailscale status` 看设备在线;`curl $KG_HUB_URL/health`;token 是否正确 |
| `/health` 通但工具没反应 | 工具的 hook/MCP 命令里 python 路径、脚本路径是否绝对且存在 |
| PUSH 没注入 | 跑 `python3 tools/kg_push_hook.py --probe` 看是否有候选;确认该 python 能读到 `.env`(`KG_HUB_URL`/`KG_HUB_API_TOKEN`) |
| 传文件到群晖失败 `subsystem request failed` | 群晖未开 SFTP,改用 `cat \| ssh "cat>tmp && mv -f tmp dst"` 管道,或改走 HTTP ingest |
| 直连 falkordb 偶发慢/超时 | tailscale relay 抖动;**优先走 HTTP**(读写都收敛到中央机本地),少用客户端直连 falkordb |
| usage_count 不涨 | 确认走的是 `/api/canonical_context?bump=1`(服务端自增),不是客户端直连写 |

---

### 一句话总结
> **一套中央仓库(每用户)+ 一份 `.env`(每设备)+ 三个对接面(HTTP / MCP / PUSH,按需)**。跨设备跨工具,接的都是你自己那张图;所有跨网络的重活(检索、写入、计数)都收敛在中央机本地,客户端只留一个可容忍的 HTTP 往返。
