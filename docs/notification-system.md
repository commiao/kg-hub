# kg-hub 通知与监控体系

> 飞书通知 + 多层监控。轻重分离:重的(图库/摄入)在 NAS,轻的(探针/通知)在别处。

## 总览(三层监控 + 一个积木)

```
[积木] feishu-notify skill        —— 任意会话/工具里发飞书(读配置,可复用)
[L1]   NAS sidecar watchdog       —— 容器内细粒度(server/falkordb/队列),NAS 活着时有效
[L2]   VPS 轻量探针 (cron+sh)     —— 异地常开,整体挂也能报(补盲区)← 最关键
[L3]   MCP 客户端预警             —— 用 kg-hub 时连不上就报(客户端视角)
```
同一个飞书群机器人 webhook,三层互补:任一层挂了还有别层兜底。

---

## 一、feishu-notify skill(发飞书的复用积木)

**位置**:`~/.claude/skills/feishu-notify/`(用户级,所有项目/会话可用)
**实现**:`scripts/send.py`,python3 纯标准库,零依赖。

### 用法
```bash
python3 ~/.claude/skills/feishu-notify/scripts/send.py "消息内容" [--webhook 名字或URL] [--title 标题]
```
- 在 Claude 会话里直接说「发飞书:xxx」即会触发该 skill。
- `--webhook`:可填配置里的**名字**(如 `kg-hub`)或完整 `https://...` URL;不填用 `default`。
- `--title`:可选标题行。
- 退出码 0 = 送达。

### 配置(加新群只改这里,免改代码)
`~/.config/feishu-notify/webhooks.json`(权限 600):
```json
{
  "default": {"url": "https://open.feishu.cn/open-apis/bot/v2/hook/XXXX"},
  "kg-hub":  {"url": "https://open.feishu.cn/open-apis/bot/v2/hook/YYYY", "secret": "仅当开启加签时填"}
}
```
- 有 `secret` 时脚本自动按飞书「加签」算法签名。
- 也可用环境变量 `FEISHU_WEBHOOK` 兜底。

### 例子
```bash
python3 ~/.claude/skills/feishu-notify/scripts/send.py "部署完成 ✅"
python3 ~/.claude/skills/feishu-notify/scripts/send.py "kg-hub 重建完成" --webhook kg-hub --title "🧪"
```

---

## 二、VPS 轻量探针(异地持续监控,L2)

**为什么**:NAS 内部的 watchdog 会和 kg-hub「同生共死」——整个 NAS/项目挂了,它也没了、发不出告警。所以需要一个**站在 NAS 外、永远在线**的探针。用常开的 VPS(`oc-vps-aliyun-us`),**纯 cron + sh + curl,零安装、几乎零资源**(监控 ≠ 部署服务,不需要 Docker)。

**位置(VPS 上)**:`/root/uptime/`
- `check.sh` —— 通用探针:逐个 curl 健康 URL,**边沿触发**(挂了报一次、恢复报一次,不刷屏),状态存 `state/`。
- `targets.conf` —— 监控目标清单(加一行即多监控一个服务):
  ```
  # name|health_url|feishu_webhook|fail_threshold(连续失败几次才报)
  kg-hub|http://100.123.208.32:8080/health|https://open.feishu.cn/open-apis/bot/v2/hook/XXXX|3
  ```
- cron:`* * * * * /root/uptime/check.sh`(每分钟)。

**告警**:连续 3 次(~3分钟)不可达 → 🔴;恢复 → ✅。
**复用/公有化**:监控任何其它服务,只需在 `targets.conf` 加一行。

---

## 三、NAS sidecar watchdog(内部细粒度,L1)

NAS compose 里的 `kg-hub-watchdog` 容器,每 ~90s 跑 `tools/watchdog.py`,**边沿触发**推飞书。检查项:
- `server_down` —— `/health` 不通
- `falkordb_slow` —— 探针 `/api/search` > 阈值(CPU 跑飞/查询堆积前兆)
- `falkordb_unreachable` —— 搜索报错
- `queue_backlog` / `stuck_jobs` / `recent_errors` —— 摄入队列积压/卡死/近 1h 报错(含具体错误样本)

---

## 四、MCP 客户端预警(L3)

`mcp_server.py` 的 kg-hub 工具(`kg_search` / `kg_add_episode` 等)在**连不上/超时**时,主动推飞书(客户端视角,跑在使用方机器上,天然在 NAS 外)。带冷却防刷屏。

---

## 关键端点 / 路径速查
| 项 | 值 |
|---|---|
| NAS hub 健康 | `http://100.123.208.32:8080/health`(经 Tailscale)|
| 飞书 skill | `~/.claude/skills/feishu-notify/scripts/send.py` |
| 飞书配置 | `~/.config/feishu-notify/webhooks.json` |
| VPS 探针 | `oc-vps-aliyun-us:/root/uptime/{check.sh,targets.conf}` |
| NAS 数据 | `/volume1/docker/kg-hub-data/`(falkordb / ingest-state / models / ingest-backup)|
---

## 通知策略更新(2026-06-12):变化触发 + 每日心跳

回填积压数据阶段「每 20 分钟必发」是合理的;数据稳定后改为**安静模式**——只在有变化时打扰,每天一条心跳确认存活。

| 脚本 | 频率 | 行为 |
|---|---|---|
| `progress.sh` | 每 20 分钟轮询(7/27/47) | **仅当 claude-mem / openclaw 计数增长时**才发:`🔄 kg-hub 新增: claude-mem=N(+d) openclaw=M(+d) 图节点=K`。无变化 → **静默**。状态存 `/root/uptime/state/progress-last.txt`,与上次对比 |
| `daily-summary.sh` | 每天 **22:00**(`0 22 * * *`) | **必发一条心跳**:有增量→`📊 今日新增 +d`;无增量→`📊 今日无新增…系统正常在线`;读不到 NAS→`⚠️ 无法读取 NAS`。基线存 `/root/uptime/state/daily-baseline.txt`(与 24h 前对比) |
| `check.sh` | 每分钟 | 不变,负责宕机/恢复(edge-triggered)告警 |

**设计要点**
- **变化触发**:平时数据静止 → 零打扰;有新 obs/胶囊进图 → 20 分钟内自动报增量。
- **每日心跳**:避免「全天无消息 ≠ 不知道死活」,22:00 固定一条;NAS 读不到时心跳变告警。
- **基线预置**:部署时把状态文件初始化为当前值,杜绝首次误报。
- **职责分离**:进度(progress)、心跳/日报(daily-summary)、存活/恢复(check)三者独立,互不掩盖。
