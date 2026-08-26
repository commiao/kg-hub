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
NAS device_liveness 容器 ── 每分钟读取 host Tailscale LocalAPI ── 写真实设备在线态到 /device-liveness(ro 消费)
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
| `tailscale-liveness-snapshot.sh` | NAS `device_liveness` 容器 | 镜像内 `/app/deploy/monitoring/nas/` | 独立判断采集设备 online/offline；校验后原子写 Tailscale JSON | 容器内每分钟 | `/volume2/4T/kg-hub-data/device-liveness/` |
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
**watchdog**:随 `docker-compose.yml` 的 `watchdog` 服务部署;`notify.json` 放挂载卷 `/volume2/4T/kg-hub-data/notify-config/`(参考 `notify.json.example`,热读)。设备 host/身份映射与在线阈值只放公开目录 `/volume2/4T/kg-hub-data/device-liveness/device-liveness.json`；`notify.json` 不得覆盖这些 capture 判据。

### 采集设备在线信号（NAS 独立 producer）

watchdog 容器与 dashboard 所在 server 容器都不读取 Tailscale socket。独立的
`device_liveness` 容器只读挂载 NAS 的 CLI 与 LocalAPI socket，查询真实 tailnet，
再把同一份动态快照只读挂入两个消费者；静态配置只声明
“capture host 对应哪台 Tailscale 设备”，不能声明 online。这里不能假设两套名字
相同：当前探针 uname 是 `MacBook-Pro-4`，Tailscale 实际是 HostName
`MacBook Pro (3)` / DNSName `mac-office...`，因此 `device-liveness.json` 必须包含：

```json
{
  "capture_probe_hosts": ["MacBook-Pro-4"],
  "capture_device_aliases": {
    "MacBook-Pro-4": ["MacBook Pro (3)", "mac-office"]
  }
}
```

完整示例见 `deploy/monitoring/nas/device-liveness.json.example`。部署命令：

```sh
bash deploy/nas/deploy-device-liveness.sh
```

脚本会依次完成：以 NAS 登录用户生成公开身份配置；调用
`deploy/nas/redeploy.sh` 同步 producer、`topology.py`、`watchdog.py`、共享解析模块和 compose，
重建/recreate `device_liveness`、server 与 watchdog；最后校验首份快照。producer
以 NAS 登录用户的 UID/GID 运行，root filesystem 只读、无网络、丢弃全部 capabilities，
并启用 `no-new-privileges`；只有公开快照目录可写。
producer 会用 Python 完整解析 JSON，并校验 `BackendState` 与 `Peer` 最小 schema，
再做原子替换。CLI 失败、JSON 损坏或 schema 不符都保留 last-good；mtime 超过
180 秒后消费者将设备态降级为 `unknown`，旧 `Online: true` 不会冒充实况。

server 只读挂载整个公开 `/device-liveness` 目录，使 host atomic rename 后的新 inode
立即可见；它不挂 notify-config。watchdog 同时只读挂公开目录与 `/config`，后者
只放 `notify.json`。因此 webhook 不进入 dashboard 容器，producer 执行的也是镜像内
已构建脚本，而不是 `commiao` 可写的 git checkout。

`capture_stale_after_min`、host 清单、身份映射和 liveness 新鲜度都只以公开
`device-liveness.json` 为准；dashboard 与 watchdog 每轮热读同一文件。

状态矩阵：设备 offline/sleep + 旧采集快照 = 看板断线、不告警；设备 fresh online
+ 快照超过 30 分钟 = `capture_probe_stale`；快照新鲜且有 red blocker = 仍告警；
工具长期无新数据 = amber/idle，不告警。只有 Tailscale 信号或 topology API 本轮
unknown 时才沿用上一轮状态；明确 offline/sleep 会清除该 host 的旧 stale/blocker。
多 host 按“配置清单 ∪ 已有快照”聚合，offline 不会盖掉另一台的 fresh blocker 或
unknown 状态。
