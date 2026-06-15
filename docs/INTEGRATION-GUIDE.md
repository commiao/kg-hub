# kg-hub 跨平台 / 跨工具对接手册(实战版)

> 让 **同一个用户** 在 **不同设备(Mac / Windows / Linux)** 和 **不同工具(Claude Code / Cursor / Qoder桌面 / Qoder-IDEA插件 / Codex / OpenClaw)** 上,都接到 **自己的那一套中央 kg-hub**。
>
> 本手册是 **实测过的步骤 + 踩过的坑**,换设备 / 新用户照着做即可。最后一节有一键自检脚本。

---

## 0. 三个核心概念

**① 每用户一套中央仓库。** kg-hub 跑在一台常开机器(NAS / 轻量服务器),每个用户部署自己的一套(自己的 falkordb + server + token)。所有设备/工具都是**客户端**,统一指向你自己的 `KG_HUB_URL`。

**② 对接有三个「面」,可叠加:**

| 面 | 作用 | 怎么连 |
|---|---|---|
| **MCP 读** | 工具里直接调 `kg_search`/`kg_stats`/`kg_add_episode` | **统一经 `muxcp` 网关**(各工具 MCP 配置加一个 muxcp 即可)|
| **PUSH 注入** | 会话启动自动把相关 canonical 注入上下文 | `kg_push_hook.py --format <tool>`(纯 HTTP,零依赖)|
| **capture** | 工具的工作 → claude-mem 生成 obs → 同步进图 | 各工具的 claude-mem 钩子/插件 |

**③ 重活全在中央机本地,客户端只走一个可容忍的 HTTP 往返。**
血泪教训:**别让客户端直连 FalkorDB(6379)**——迁移后它只绑中央机 localhost、不对 tailnet 暴露,且 tailscale relay 会抖动。读/写/排名一律走 server 的 HTTP API(`:17171`),server 在本地查 FalkorDB。

```
   各设备/工具 ──MCP(muxcp)/HTTP(:17171, Bearer)──▶ 中央 kg-hub(常开)
                                                      falkordb(仅localhost) + server + ingester + watchdog
```

---

## 1. 前置:部署中央仓库(每用户一次)

1. 常开机克隆仓库 → `docker compose -p kg-hub up -d`(falkordb + kg_hub_server + ingester + watchdog)。
2. 设 `FALKORDB_PASSWORD`、`KG_HUB_API_TOKEN`(随机长串)。
3. 装 Tailscale;**server 端口经 tailscale 暴露**(本项目用 `127.0.0.1:17171→容器8080`,靠 NAS tailscale userspace 转发 inbound→localhost,所以 tailnet 设备用 `<tailscale-ip>:17171` 可达;falkordb 的 6379 **不**暴露)。
4. 验证:`curl http://<tailscale-ip>:17171/health` → `{"status":"ok"}`。

> 部署/持久化/监控细节见 `docs/incident-retrospective.md`。

---

## 2. 通用客户端配置(每设备一次)

所有工具共用一份 env:**`~/.claude-mem/.env`**(Windows:`%USERPROFILE%\.claude-mem\.env`)。

```ini
KG_HUB_URL=http://<tailscale-ip>:17171     # 中央机
KG_HUB_API_TOKEN=<你的-token>
KG_HUB_FEISHU_WEBHOOK=<可选:连不上时告警>
# 注:KG_HUB_FALKORDB_* 现在客户端基本不需要(读写都走 HTTP)
```

**muxcp 网关**(各工具 MCP 都连它):确认 `~/.config/muxcp/run-muxcp.sh` 存在且其上游含 `kg_hub`(指向本机 `mcp_server.py`,纯 HTTP 客户端)。

---

## 3. 按工具对接(实测步骤)

> 共性:**MCP** = 在该工具 MCP 配置加 `muxcp`;**PUSH/capture** = 各工具机制不同,见下。

### 3.1 Claude Code ✅
- **MCP**:启动参数/`~/.claude.json` 里带 `muxcp`(本项目已是)。
- **PUSH**:`~/.claude/settings.json`
  ```json
  "hooks": { "SessionStart": [ { "matcher": "startup|resume", "hooks": [
    { "type":"command","timeout":10,
      "command":"<venv>/bin/python /path/kg-hub/tools/kg_push_hook.py --format claude" } ] } ] }
  ```
- **capture**:claude-mem 插件(`enabledPlugins: {"claude-mem@thedotmack": true}`);如长期没产 obs,跑 `npx claude-mem@latest install` 重建 runtime。

### 3.2 Cursor ✅
- **MCP**:`~/.cursor/mcp.json` 加 `muxcp`。
- **PUSH + capture**:`<工程>/.cursor/hooks.json` 的 `beforeSubmitPrompt` 里挂两类——
  - claude-mem 的 `hook cursor session-init/observation/...`(capture,写规则文件机制);
  - `kg_push_hook.py --format cursor`(注入,写 `.cursor/rules/kg-hub-canonical.mdc`,`alwaysApply:true`)。

### 3.3 Qoder(桌面版 + IDEA 插件)✅
**两个 Qoder 共用同一后端** `~/.qoder/shared_client`,都读 `~/.qoder/settings.json`。配一次,两者通用。
- **MCP**:`~/.qoder/mcp.json` 加 `muxcp`(本项目已是)。
- **PUSH + capture**:`~/.qoder/settings.json` 的 `hooks`(Claude-Code 式:`SessionStart`/`UserPromptSubmit`/`PostToolUse`/`Stop`):
  - claude-mem 捕获钩子 **经 wrapper 调用** `tools/qoder_cm_hook.sh <mode>`(见下"坑3");
  - `SessionStart` 追加一组(**不设 matcher = match-all**)跑 `kg_push_hook.py --format claude`(Qoder 的 SessionStart source 值和 Claude Code 不同,matcher 写死会不触发)。
- ⚠️ **改完必须重启 Qoder 后端 daemon 才生效**(见"坑1")。

### 3.4 Codex ✅
- **MCP**:`~/.codex/config.toml` 的 `[mcp_servers.muxcp]`(本项目已是)。
- **PUSH**:Codex 无 Claude-Code 式 SessionStart 命令钩子 → 走 **pull**:在 `~/.codex/AGENTS.md` 加一段"会话开始用 muxcp 的 kg_hub 工具按项目名拉 canonical 上下文"。
- **capture**:用 **Codex 官方 CLI** 装 claude-mem 插件(钩子会自动解析到最新 13.6.0,安全):
  ```bash
  CX=/Applications/Codex.app/Contents/Resources/codex
  "$CX" plugin marketplace add ~/.claude/plugins/marketplaces/thedotmack   # 注册(双格式 marketplace)
  "$CX" plugin add claude-mem@claude-mem-local                              # installed, enabled, 13.6.0
  ```
  ⚠️ **不要**手动启用磁盘上缓存的 13.2.0(那是 #2188 烧 CPU 的版本)。

### 3.5 OpenClaw
胶囊系统,走写入:把 `capsule-*.md`/`CAPSULE-*.md`(≥1500B)同步到中央机摄入源目录(或 `POST /api/ingest`),ingester 自动按命名+水位线去重摄入。

---

## 4. ⚠️ 实战踩过的坑(换设备/重连必看)

**坑1:Qoder 改了 `settings.json` 不生效。** Qoder(含 IDEA 插件)的后端 `~/.qoder/shared_client/.../Qoder` 是**常驻 daemon**,只在 daemon 启动时读配置;关标签/新会话/Cmd+Q 都不一定重启它。改完要**结束该 daemon 进程**(`pkill -f '.qoder/shared_client/bin'`),它会在下次用 Qoder 时带新配置重生。

**坑2:`project` 被标成插件版本号(如 `13.6.0`)。** claude-mem 推导 project 是 `GEMINI_* ?? CLAUDE_PROJECT_DIR ?? process.cwd()`;某些工具(Qoder IDEA)调钩子时 cwd 落在插件目录 → 兜底成版本号。**修法**:用 `tools/qoder_cm_hook.sh` 包一层,从 `QODER_PROJECT_DIR`/payload.cwd 取真实项目 → 设 `CLAUDE_PROJECT_DIR`。

**坑3:claude-mem 钩子要用稳定路径。** 钩子命令应解析 `~/.claude/plugins/marketplaces/thedotmack/plugin`(版本无关的 marketplace 路径)或 cache 里**最新**版本,别写死某版本目录(否则升级即断)。

**坑4:Mac→群晖 传文件 `subsystem request failed`。** 新版 `scp` 走 SFTP,群晖 sshd 默认没开 → 改 `cat | ssh "cat>tmp && mv -f tmp dst"` 管道。

**坑5:客户端直连 FalkorDB 偶发超时。** falkordb 只绑中央机 localhost(6379 不对 tailnet 暴露)+ relay 抖动。**所有读/写/排名走 HTTP `:17171`**;MCP server 也已改纯 HTTP(不再 import graphiti/连 falkordb)。

**坑6:claude-mem worker 空转烧 CPU(#2188)。** bun-runner 收空 stdin 会死循环。已用 `tools/claude_mem_guard.sh` + launchd 兜底(累计 CPU>120s 的 claude-mem hook 进程自动清+告警)。换设备记得也装这个守护。

**坑7:claude-mem 不产新 obs。** 多半是 runtime 没装好 / 找不到 `claude` 可执行文件 → `~/.claude-mem/settings.json` 设 `CLAUDE_CODE_PATH`,并 `npx claude-mem@latest install` 重建 + 重启 worker。

---

## 5. HTTP API 速查(任意工具/脚本通用)

带 `Authorization: Bearer $KG_HUB_API_TOKEN`(`/health` 除外)。

```bash
curl $KG_HUB_URL/health
curl -H "Authorization: Bearer $TOKEN" "$KG_HUB_URL/api/search?q=关键词&num_results=5"            # 字面子串(快,probe 用)
curl -H "Authorization: Bearer $TOKEN" "$KG_HUB_URL/api/search_semantic?q=自然语言问题&num_results=5" # 向量语义(kg_search 走这条)
curl -H "Authorization: Bearer $TOKEN" "$KG_HUB_URL/api/canonical_context?kw=<项目名>&top_n=3&bump=1" # 取 canonical + 累计使用量
curl -H "Authorization: Bearer $TOKEN" "$KG_HUB_URL/api/stats"                                       # 实体/边/episode 计数
curl -H "Authorization: Bearer $TOKEN" "$KG_HUB_URL/api/usage_ranking?top_n=10"                      # 调用量排名(有价值胶囊)
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$KG_HUB_URL/api/ingest" \
  -d '{"name":"x","episode_body":"...","source_description":"manual","source_obs_id":"uniq-1","reference_time":"2026-01-01T00:00:00Z"}'
```
> server 端点全在 NAS 本地查 FalkorDB;`/api/ingest` 锁竞争会自动退避重试(不丢数据)。

---

## 6. 同一用户跨设备/跨工具:一致性约定

1. 所有设备 `.env` 指向**同一个** `KG_HUB_URL` + token → 天然聚合进你自己的中央图。
2. 写入两条路:交互记忆经 claude-mem→同步→ingester;主动沉淀直接 `POST /api/ingest`(稳定 `source_obs_id` 保证幂等)。
3. 读取统一:任意工具 `kg_search`(经 muxcp)或 HTTP `/api/search*` 查同一张图。
4. 安全红线:token 只放 `.env`(不进 URL query、不写进图);服务只在私网可达;客户端不直连别人的图。

---

## 7. 验证与自检

**一键快照**(用完某工具后跑,看哪条链路刚活动):
```bash
sh tools/verify_tool_links.sh     # 4工具 × 3链路(MCP/PUSH/capture)现状表
```

**手动核对信号**(中央机/Mac 终端):
```bash
tail -5 data/.push_hook.log                                            # PUSH 注入最近触发(fmt=claude/cursor/...)
python3 -m tools.usage_ranking | head -20                              # usage_count 排名 + 最近 bump
sqlite3 ~/.claude-mem/claude-mem.db \
  "SELECT platform_source,count(*),max(started_at) FROM sdk_sessions GROUP BY platform_source"  # capture:各工具会话
curl -s -H "Authorization: Bearer $TOKEN" $KG_HUB_URL/api/stats        # 图规模
```

| 现象 | 排查 |
|---|---|
| 工具调 kg_* 无返回 | `tailscale status`;`curl $KG_HUB_URL/health`;muxcp 是否在该工具 MCP 配置里 |
| PUSH 没注入 | `python3 tools/kg_push_hook.py --probe`;确认跑它的 python 能读到 `.env` |
| capture 无新会话 | 看坑1(Qoder 重启 daemon)/坑6/坑7;`sdk_sessions` 是否有该工具平台的新行 |
| project 标成版本号 | 坑2(wrapper 设 `CLAUDE_PROJECT_DIR`)|
| 传文件群晖失败 | 坑4(`cat\|ssh` 管道)|

---

### 一句话
> **一套中央仓库(每用户)+ 一份 `.env`(每设备)+ muxcp 统一 MCP + 各工具按机制挂 PUSH/capture**;跨网络重活全收敛到中央机,客户端只留一个可容忍的 HTTP 往返。换设备照本手册 §2→§3→§7 走一遍即可。
