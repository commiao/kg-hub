# 如何往报表门户加一个报表

门户在常开 NAS 的 `kg-hub-server` 容器里（不是独立服务）。入口：
`http://100.123.208.32:17171/portal`（tailnet 内任意设备）。

源码是 build 时 COPY 进 Docker 镜像的，所以**改完代码要重建镜像+重启容器才生效**。

## 三步代码（都在 `kg_hub_server.py`）

1. **注册表加一条** —— `PORTAL_REPORTS`：
   ```python
   {"name": "使用排行", "desc": "胶囊累计注入次数排行", "url": "/dashboard/usage", "icon": "📊", "ready": True},
   ```
2. **写处理器** —— 照抄 `dashboard_capsules`，换查询和渲染：
   ```python
   async def dashboard_usage(request: Request) -> HTMLResponse:
       driver = get_status_driver()
       rows, _, _ = await driver.execute_query("... 你的 Cypher ...")
       data = {...}
       return HTMLResponse(_MY_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))
   ```
   - 取数：`get_status_driver()` 直读 FalkorDB（服务端渲染，数据内嵌进页面，**不需要客户端 token**）。
   - 模板：用 `"""...__DATA__..."""` + `.replace("__DATA__", json.dumps(...))`，**别用 f-string**（CSS/JS 的 `{}` 会冲突）。
3. **注册路由** —— `app = Starlette(routes=[...])` 里加：
   ```python
   Route("/dashboard/usage", dashboard_usage, methods=["GET"]),
   ```
   > 鉴权中间件已放行 `/ /portal /dashboard*`，这步不用动；`/api/*` 仍要 Bearer。

## 一步部署

```sh
deploy/nas/redeploy.sh
```
它做：同步 `kg_hub_server.py` 到 NAS → 重建镜像 → 重启 `kg_hub_server`（project `kg-hub`，不动 falkordb）→ 探活。跑完打开 `…/portal` 就能看到新卡片。

报表跨多个文件时：`FILES="kg_hub_server.py 其他.py" deploy/nas/redeploy.sh`。

## 约定 & 提示

- **一个报表 = 注册表一条 + 一个 `/dashboard/*` 处理器**，永远出现在同一个门户。
- 想自动刷新：模板 `<head>` 里加 `<meta http-equiv=refresh content=60>`。
- 颜色用系统色（`Canvas`/`CanvasText`/`color-mix`）自动适配深浅色；徽章用自带深字浅底的固定色块（两种模式都可读）。
- 文件长了想拆：可把各 `_DASH_*_HTML` 模板和处理器拆到单独模块再 import，门户/路由结构不变。
