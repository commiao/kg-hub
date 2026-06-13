# kg-hub 监控体系(单一真相源)

> 忘了"什么在哪、谁盯谁、怎么改"时,**看这一篇** + 跑一条全景命令:
> `tools/monitoring-status.sh`(或 VPS 上 `sh /root/uptime/status.sh`)。

## 拓扑(谁挂了谁来报)

```
VPS(oc-vps-aliyun-us, 常开)         NAS(home-nas-syno, 常开)
  check.sh ──监控──▶ kg-hub@NAS         watchdog(容器) ── kg-hub 内部(server/falkordb/队列)
  check.sh ──监控──▶ openclaw@本机        nas_probe(容器) ──监控──▶ openclaw@VPS(公网IP)
  progress.sh ── 摄入增量播报              ingester(容器) ── 一次性重建(已完成)
  daily-summary.sh ── 每日心跳
        ▲ 互盯:VPS↔NAS,任一整体挂,另一台飞书报
Mac: mcp_server.py ── 用 kg-hub 连不上时飞书预警(L3,客户端视角)
告警通道:飞书群机器人 webhook(真值只在各机 webhook.conf,不入库)
```

## 组件总表

| 组件 | 机器 | 路径 | 监控/作用 | 频率 | 配置 |
|---|---|---|---|---|---|
| `check.sh` | VPS | `/root/uptime/` | 探 kg-hub(NAS)+ openclaw(本机)健康,边沿触发宕机/恢复 | cron 每分钟 | `targets.conf`(目标)+ `webhook.conf` |
| `progress.sh` | VPS | `/root/uptime/` | 摄入计数变化才播报增量 | cron 7/27/47 | `webhook.conf` |
| `daily-summary.sh` | VPS | `/root/uptime/` | 每日 22:00 心跳/日报(读不到 NAS→告警) | cron `0 22 * * *` | `webhook.conf` |
| `openclaw-sync.sh` | VPS | `/root/uptime/` | clawd 胶囊 → NAS openclaw-src(持续同步) | cron `19 * * * *` | — |
| `status.sh` | VPS | `/root/uptime/` | 全景:汇总 VPS+NAS 所有探针/容器/进度 | 手动/被 `tools/monitoring-status.sh` 调用 | — |
| `nas_probe.py`+`loop.sh` | NAS | `/volume1/docker/nas-probe/` | 反向探 openclaw@VPS(公网),补"VPS 整体挂"盲区 | 容器 `kg-hub-nas-probe` 每 60s | `targets.conf` + `webhook.conf` |
| `watchdog.py` | NAS | 仓库 `tools/`,容器内 `/app` | kg-hub 内部:/health、falkordb 慢查询、队列积压/卡死/错误 | 容器 `kg-hub-watchdog` 每 ~90s | `/config/notify.json`(热读,见 `notify.json.example`) |
| MCP 预警 | Mac | `mcp_server.py` | kg-hub 连不上/超时主动飞书(冷却 10min) | 用时触发 | `KG_HUB_FEISHU_WEBHOOK` env |

## webhook 约定(防泄密)
- 真实飞书 webhook **只存在各机 `webhook.conf`**(权限 600),**已 .gitignore,绝不入库**。
- 仓库里只有 `webhook.conf.example`(占位)。所有脚本:targets.conf 的 webhook 列留空 → 回退读 `webhook.conf`。
- 换 webhook = 改各机 `webhook.conf` 一处即可。

## 从零部署/重装
**VPS**(`/root/uptime/`):放本目录 `vps/*`;`cp webhook.conf.example webhook.conf` 填真值(chmod 600);`crontab -e` 加:
```
* * * * * /root/uptime/check.sh
7,27,47 * * * * /root/uptime/progress.sh
0 22 * * * /root/uptime/daily-summary.sh >/dev/null 2>&1
19 * * * * /root/uptime/openclaw-sync.sh >> /root/uptime/openclaw-sync.log 2>&1
```
**NAS**(`/volume1/docker/nas-probe/`):放本目录 `nas/*`;`cp webhook.conf.example webhook.conf` 填真值;起独立容器(复用 kg-hub-server 镜像):
```
sudo docker run -d --restart unless-stopped --name kg-hub-nas-probe --user 0 \
  -v /volume1/docker/nas-probe:/probe kg-hub-server:latest sh /probe/loop.sh
```
**watchdog**:随 `docker-compose.yml` 的 `watchdog` 服务部署;`notify.json` 放挂载卷 `/volume1/docker/kg-hub-data/notify-config/`(参考 `notify.json.example`,热读)。
