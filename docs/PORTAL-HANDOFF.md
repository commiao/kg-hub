# kg-hub 报表门户 — 交接文档

> 给**新会话**：照这份就能接手门户开发，不需要之前的对话上下文。
> 配套：加报表的简版步骤见 `docs/REPORTS.md`；本文是完整背景 + 环境 + 坑。

---

## 1. 这是什么

> **架构已升级（2026-06-25）**：门户已拆成**独立服务 `report-portal`**。
> 统一跨源入口 = **`http://100.123.208.32:17172/portal`**（独立容器；代码在
> `/Users/mac/workspace_claudeCode/report-portal/`，见其 `README.md` +
> `docs/MANIFEST-CONTRACT.md`）。门户只做聚合导航，各数据源自持并渲染自己的看板，
> 只暴露一个 `/portal_manifest` 给门户抓取合并（"瘦门户 + manifest"模式）。
>
> **kg-hub 在新架构里的角色**：
> - (a) 暴露 `/portal_manifest`，列出自己的卡片，供独立门户聚合；
> - (b) **自持并渲染自己的看板** `/dashboard/*`（直读 NAS-local FalkorDB，
>   必须留在数据旁边，不能搬到门户）。
>
> kg-hub 仍保留本地视图 `/portal`，但它现在**冗余**（跨源入口是 :17172）。
> **本文档以下章节描述的都是 kg-hub 侧**（manifest + dashboards + 部署）。

kg-hub 把自己的报表/看板收拢成卡片，通过 `/portal_manifest` 交给独立门户聚合。

- 本源 manifest：`http://100.123.208.32:17171/portal_manifest`（JSON 卡片清单）
- 本地视图（冗余 fallback）：`http://100.123.208.32:17171/portal`
- 已有三个看板：
  - `/dashboard/capsules` —— 知识胶囊曝光 + 各 cwd 关键词下的实时排序与 top-3 注入
  - `/dashboard/usage` —— 胶囊累计注入排行 + 建议晋升 / 建议下线
  - `/dashboard/knowledge` —— 全图概览（Episode/实体/关系）+ 最近知识

---

## 2. 在哪运行 / 怎么访问

```
代码:  kg_hub_server.py（git main 已提交；NAS 上 /volume1/docker/kg-hub-src/kg_hub_server.py）
运行:  Docker 容器 kg-hub-server（镜像 kg-hub-server:latest）
        Container Manager「项目」名 = kg-hub
        端口: 容器内 8080 → 宿主 127.0.0.1:17171 → 经 tailscale 暴露给 tailnet
数据:  FalkorDB（同 NAS，只绑 127.0.0.1:6379，不对外）
```

- **本机仓库**：`/Users/mac/workspace_claudeCode/kg-hub`，git remote `git@github-commiao:commiao/kg-hub.git`，主分支 `main`。
- **访问门户**：tailnet 内任意设备浏览器开 `http://100.123.208.32:17171/portal`（无需 token）。
- **SSH 到 NAS**：`ssh commiao@100.123.208.32`（key-based，走 tailscale；本机已配好，无需密码）。
- **凭据/配置**：`~/.claude-mem/.env`，关键变量 `KG_HUB_URL`、`KG_HUB_API_TOKEN`、`KG_HUB_FALKORDB_*`。脚本/工具都从这里读。

---

## 3. 关键事实（架构 & 安全）

- **服务端渲染**：dashboard 处理器直读 FalkorDB（`get_status_driver()`），把数据 JSON 内嵌进 HTML 页面返回。**页面不做客户端 API 调用**，所以浏览器端不需要 token。
- **鉴权放行**：`BearerAuthMiddleware.dispatch` 放行 `/`、`/portal`、`/dashboard*`（只读、且 17171 仅 tailnet）。**`/api/*` 仍强制 Bearer token**——加报表时别破坏这条边界（已验证无 token 访问 `/api/*` 返回 401）。
- **源码 COPY 进镜像**：`deploy/nas/Dockerfile` 在 build 时 `COPY . /app`。**改完代码必须重建镜像 + 重启容器才生效**（不是改文件即时生效）。

---

## 4. 代码位置（都在 `kg_hub_server.py`）

按符号找（行号会漂，用符号名）：

- `PORTAL_REPORTS` —— 门户卡片注册表（list of `{name,desc,url,icon,ready}`）。
- `_PORTAL_HTML` + `async def portal(request)` —— 门户页模板与处理器。
- `_DASH_CAPSULES_HTML` + `_DASH_KWS` + `async def dashboard_capsules(request)` —— 胶囊看板模板与处理器（**抄它做新报表**）。
- `CANONICAL_SCOPE` / `DEFAULT_SCOPE` / `SCOPE_MATCH_BONUS` / `SCOPE_OTHER_PENALTY` —— 胶囊打分用的常量（看板复用）。
- `BearerAuthMiddleware.dispatch` —— 鉴权放行逻辑（已含 `/ /portal /dashboard*`）。
- `app = Starlette(routes=[...])` —— 路由表（新报表在这加一行 `Route`）。
- 已有数据 API（可直接复用/参考）：`/api/usage_ranking`、`/api/canonical_context`、`/api/stats`、`/api/search`、`/api/node_neighbors`、`/api/path_between`。

---

## 5. 加一个报表（3 步代码 + 1 步部署）

**代码**（`kg_hub_server.py`）：
1. `PORTAL_REPORTS` 加一条：`{"name":"使用排行","desc":"...","url":"/dashboard/usage","icon":"📊","ready":True}`
2. 写 `async def dashboard_usage(request) -> HTMLResponse:`（抄 `dashboard_capsules`）：
   - 取数：`driver = get_status_driver()` → `rows,_,_ = await driver.execute_query("... Cypher ...")`
   - 渲染：模板用 `"""...__DATA__..."""` + `.replace("__DATA__", json.dumps(data, ensure_ascii=False))`。**别用 f-string**（CSS/JS 里的 `{}` 会和 f-string 冲突）。
3. 路由表加：`Route("/dashboard/usage", dashboard_usage, methods=["GET"])`（鉴权已放行 `/dashboard*`，不用动中间件）。

**部署**（一条命令，封装了所有 NAS 细节）：
```sh
deploy/nas/redeploy.sh
```
做：同步 `kg_hub_server.py` 到 NAS（原子 tmp+mv）→ `docker compose build kg_hub_server` → `docker compose -p kg-hub up -d --no-deps kg_hub_server watchdog ingester` → 探活。
多文件：`FILES="kg_hub_server.py 其他.py" deploy/nas/redeploy.sh`。

详见 `docs/REPORTS.md`。

---

## 6. 部署细节（脚本里已封装，排障时要知道）

```
NAS:           commiao@100.123.208.32
源码目录:      /volume1/docker/kg-hub-src/           （NAS 上无 .git，是拷贝；不能 git pull）
docker 命令:   sudo -n /var/packages/ContainerManager/target/usr/bin/docker   （需 sudo，免密已配）
compose 项目:  -p kg-hub                              （注意不是目录名 kg-hub-src！用错会撞容器名）
同镜像容器:    kg_hub_server / watchdog / ingester    （共用 kg-hub-server:latest）
不要动:        falkordb 容器（--no-deps 保证不碰它）
```

手动等价命令（脚本挂了时）：
```sh
cat kg_hub_server.py | ssh commiao@100.123.208.32 \
  'cat > /volume1/docker/kg-hub-src/.t && mv -f /volume1/docker/kg-hub-src/.t /volume1/docker/kg-hub-src/kg_hub_server.py'
ssh commiao@100.123.208.32 'cd /volume1/docker/kg-hub-src && \
  sudo -n /var/packages/ContainerManager/target/usr/bin/docker compose build kg_hub_server && \
  sudo -n /var/packages/ContainerManager/target/usr/bin/docker compose -p kg-hub up -d --no-deps kg_hub_server'
```

---

## 7. 验证

```sh
set -a; source ~/.claude-mem/.env; set +a
curl -s -o /dev/null -w "health=%{http_code}\n"  "$KG_HUB_URL/health"
curl -s -o /dev/null -w "portal=%{http_code}\n"  "$KG_HUB_URL/portal"
curl -s -o /dev/null -w "dash=%{http_code}\n"    "$KG_HUB_URL/dashboard/capsules"
curl -s -o /dev/null -w "api(无token,应401)=%{http_code}\n" "$KG_HUB_URL/api/usage_ranking"
# 看数据是否真渲染进页面:
curl -s "$KG_HUB_URL/dashboard/capsules" | grep -oE "canonical_total|rankings|注入" | sort -u
```
也可用瘦客户端 CLI：`python3 -m tools.capsules`（同样查线上，命令行版）。

---

## 8. 坑（必读）

1. **改完代码不部署不生效**——源码 build 进镜像（§3、§6）。
2. **compose project 必须 `-p kg-hub`**——否则 compose 不认现有容器，会新建并撞名报错。
3. **NAS 整机有时 tailscale 抖动/查询偶慢**——`canonical_context` 这类重查询~4s，curl 给足超时（脚本已处理）。NAS 本身 always-on（别再误判"睡着"）。
4. **FalkorDB 只在 NAS localhost**——本机连不上 6379；所有读必须经 server HTTP 或在 NAS 上。
5. **模板别用 f-string**——CSS/JS 的 `{}` 冲突；用 `__DATA__` 占位 + `.replace`。
6. **别给 `/api/*` 去鉴权**——只放行 `/ /portal /dashboard*`。
7. **深浅色**：页面用系统色 `Canvas`/`CanvasText`/`color-mix`；徽章用自带深字浅底固定色块（两模式都可读）。
8. **改完一定 commit + push，别只改 NAS**：`redeploy.sh` 只把本地同步到 NAS，不碰 git。若只部署不提交，NAS 与 git 会漂移，下个会话部署时会覆盖你的工作（已发生过一次，靠 diff 抢救回来）。流程：改代码 → `git commit && git push` → `redeploy.sh`。部署前可校验 `NAS 的 kg_hub_server.py sha == git HEAD`。

---

## 9. 现状 & 建议下一步

- ✅ 已上线（kg-hub 侧）：`/portal_manifest`、`/dashboard/capsules`、`/dashboard/usage`、
  `/dashboard/knowledge`、本地 `/portal`（冗余）、`deploy/nas/redeploy.sh`、`docs/REPORTS.md`。
- ✅ **独立门户 `report-portal` @ :17172** 按 manifest 聚合各源卡片（含 kg-hub 三张卡）；
  kg-hub 的 `PORTAL_REPORTS` 一改，门户抓 manifest 自动显示，无需改门户代码。
- 🔜 顺手能加的看板（数据已有）：
  - **监控状态** `/dashboard/health` ← 容器状态/图节点数（数据见 `deploy/monitoring/`、`/api/stats`）
- **加数据源**：给 **kg-hub** 加看板 = `PORTAL_REPORTS` 加一条 + 写 `/dashboard/*`（见 §5、`docs/REPORTS.md`），
  会同时进本地 `/portal` 和 `/portal_manifest`，独立门户自动聚合。
  **非 kg-hub 的全新数据源**：在 `report-portal` 的 `PORTAL_SOURCES` 加一条（见 report-portal 仓
  `README.md` + `docs/MANIFEST-CONTRACT.md`），该源自己实现 `/portal_manifest` 即可——不用动 kg-hub。

---

## 10. 相关索引

- `kg_hub_server.py` —— 服务端（门户/看板/API 全在这）
- `docs/REPORTS.md` —— 加报表简版步骤
- `deploy/nas/redeploy.sh` —— 一键部署
- `deploy/nas/MIGRATION.md` —— NAS 部署/架构背景
- `tools/capsules.py` —— 胶囊清单+排序的 CLI 瘦客户端
- `docs/SELF-EVOLVING.md` / `docs/CONTRIBUTION-SIGNAL.md` —— 胶囊打分/自进化的设计与"为何不过度自动化"的结论（看板展示的 score 来源背景）
- auto-memory：`~/.claude/projects/-Users-mac-workspace-claudeCode/memory/kg-hub-capsule-ranking.md`（项目长期状态）
