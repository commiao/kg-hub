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
NAS device_liveness 容器 ── 每分钟读取 host Tailscale LocalAPI ── 写在线态到 host `device-liveness/runtime/`(ro 消费)
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
| `watchdog.py` | NAS | 仓库 `tools/`,容器内 `/app` | kg-hub 本体健康(server/falkordb/入图队列/refinery/采集链路);另搭载网关状态与 NAS 盘温告警,边界见「watchdog 的归属与边界」 | 容器 `kg-hub-watchdog` 每 ~90s | `/config/notify.json`(热读,见 `notify.json.example`) |
| `tailscale-liveness-snapshot.sh` | NAS `device_liveness` 容器 | 镜像内 `/app/deploy/monitoring/nas/` | 独立判断采集设备 online/offline；校验后原子写 Tailscale JSON | 容器内每分钟 | `/volume2/4T/kg-hub-data/device-liveness/` |
| MCP 预警 | Mac | `mcp_server.py` | kg-hub 连不上/超时主动飞书(冷却 10min) | 用时触发 | `KG_HUB_FEISHU_WEBHOOK` env |

## watchdog 的归属与边界(2026-10-08 判定)

> 问过「watchdog 该不该迁到 fleet-ops」。结论:**留在 kg-hub**。写在这里免得重问,
> 也免得再往它身上加不属于 kg-hub 的职责。

### 为什么留在 kg-hub,不迁 fleet-ops

1. **判断逻辑是 kg-hub 的业务语义。** 入图队列、抽取失败、胶囊隔离区、refinery 停摆、
   采集链路三态这些判据只对 kg-hub 有意义;代码直接 import `utils.device_liveness` 与
   `kg_hub_env`,与 server 同一个镜像、随同一份 `docker-compose.yml` 部署。搬走等于把
   业务逻辑放进治理仓库,kg-hub 每改一次接口都要两边同改。
2. **fleet-ops 按自己的定义不收它。** fleet-ops README 第一句:装的是「管别的东西的那些
   东西,**本身没有常驻服务**」。watchdog 是 90 秒一轮的常驻容器。
3. **它必须跑在 NAS 里。** 它走 docker 内网直连 `kg_hub_server`;fleet-ops 跑在 Mac。
   2026-07 在 Mac 上经 Tailscale 轮询 NAS 的 watchdog 规律性假报超时,已经退役过一次。
4. **fleet-ops 准则 32 担心的问题另有解法。** 「检查不能依赖被检查项」:watchdog 与
   kg-hub 同镜像、同一台 NAS,NAS 整体挂了它也挂 —— 这个担心成立。但补这个盲区的是
   上文拓扑里异地独立的 VPS `check.sh`,不是把 watchdog 搬家。watchdog 的角色是
   「被检查对象活着时看细节」,细节判断必须贴着它。

### 它现在装了什么(22 种告警,按归属分)

| 归属 | 告警 | 定性 |
|---|---|---|
| kg-hub 本体(12) | `server_down` `queue_backlog` `stuck_jobs` `recent_errors` `extraction_failing` `capsule_stale` `falkordb_slow` `falkordb_unreachable` `refinery_stalled` `capture_blocked` `capture_probe_stale` `capture_monitor_unhealthy` | 本职 |
| kg-hub 作为网关消费方(2) | `gateway_consumer_config_drift` `gateway_consumer_contract_unhealthy`(`check_model_gateway_consumer_contract`) | 本职:核对的是 kg-hub 自己的调用合同 |
| 模型网关自身(7) | `gateway_monitor_unhealthy` 与 `GATEWAY_ALERTS` 六项(`gateway_not_ready` … `gateway_provider_circuit_open`),经 kg-hub 拓扑取数(`check_gateway_monitor`) | **搭车**:网关属 credvault(T-0046);09-08 接入 readiness 告警,09-18 加上自动断路告警,都是为复用现成的飞书通道 |
| NAS 宿主机(1) | `disk_temp_high` | **搭车**:2026-08-16 过热停机 24 小时无人知,临时加入 |

另有一处**不是监控**的搭车:compose 里 watchdog 的循环顺带跑 `tools.export_gateway_usage`
(成本看板的数据导出),为的是不新起一个常驻服务。

这张表的数字以 `tools/watchdog.py` 为准 —— **增删告警时回来改这里**,否则它会变成
一张说谎的清单。

### 现在不拆,但要知道代价

三处搭车都是刻意的(复用告警去重与飞书通道、少一个常驻服务),拆出去要各自重建
告警通道,所以**暂不动**。代价是一个真实的准则 32 风险:**网关的 7 种告警寄生在
kg-hub 的镜像里,kg-hub 一次坏部署会让它们一起哑掉。** 目前 Mac 侧
`com.credvault.connection-status` 对网关连通性有部分兜底,所以不紧急。

### 什么时候重新评估

- 又要往 watchdog 里接一样**不属于 kg-hub** 的东西时 —— 先读这一节;第四样搭车就该
  另起家了;
- 出现一次「kg-hub 部署坏了,网关同时出事却没人报」;
- 要动盘温告警时(比如散热治理改阈值),先想清它该归 NAS 宿主机层,而不是继续留在这里。

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
相同：探针的 OS 主机名会被 macOS 自动改号（曾是 `MacBook Pro (3)`，后来变成
`MacBook-Pro-4`），所以自 2026-09-03 起探针优先上报固定的 `KG_HUB_CAPTURE_HOST`
（Mac 上设为 `mac-office`，与 Tailscale DNSName 一致），OS 主机名只作回退。
`device-liveness.json` 因此写成：

```json
{
  "capture_probe_hosts": ["mac-office"],
  "capture_device_aliases": {
    "mac-office": ["MacBook Pro (3)", "MacBook-Pro-4"]
  }
}
```

`capture_probe_hosts` 必须等于探针**实际上报**的 host：watchdog 按它去找快照，
别名只用于判在线、不用于找快照。这里原先写的是旧名 `MacBook-Pro-4`，与探针上报的
`mac-office` 对不上，`capture_probe_stale` 曾因此持续假红（2026-09-07，T-0059 修复）。
别名里保留 HostName 与旧名，是为了快照 stale 时仍能靠 Tailscale 身份判到在线。
`deploy-device-liveness.sh` 只在线上**没有**该文件时才用示例初始化，已存在则保留；
所以改示例只影响全新安装，改线上要直接改 `/volume2/4T/kg-hub-data/device-liveness/device-liveness.json`。

完整示例见 `deploy/monitoring/nas/device-liveness.json.example`。部署命令：

```sh
bash deploy/nas/deploy-device-liveness.sh
```

脚本会依次完成：以 NAS 登录用户生成公开身份配置；调用
`deploy/nas/redeploy.sh` 同步 producer、`topology.py`、`watchdog.py`、共享解析模块和 compose，
重建/recreate `device_liveness`、server 与 watchdog；最后校验首份快照。producer
以 NAS 登录用户的 UID/GID 运行，root filesystem 只读、无网络、丢弃全部 capabilities，
并启用 `no-new-privileges`；只挂载单个 Tailscale socket，且只有 `runtime/` 快照
子目录可写，身份映射与阈值配置不进入 producer。
producer 会用 Python 完整解析 JSON，并校验 `BackendState` 与 `Peer` 最小 schema，
再做原子替换。CLI 失败、JSON 损坏或 schema 不符都保留 last-good；mtime 超过
180 秒后消费者将设备态降级为 `unknown`，旧 `Online: true` 不会冒充实况。
连续三次采样失败会让 producer 退出并由 Docker 自动重启；watchdog 另外触发
`capture_monitor_unhealthy`，明确提示监控证据源失效，而不是误报某台 Mac 探针。

server 把 host `runtime/` 单独只读挂到 `/device-liveness`，producer atomic rename
后的新 inode 立即可见；可信配置文件另行只读挂到 `/device-liveness-config`，且不挂
notify-config。watchdog 同时只读挂这两处与 `/config`，后者只放 `notify.json`。
因此 producer 不能改告警策略，webhook 不进入 dashboard 容器；producer 执行的也是
镜像内已构建脚本，而不是 `commiao` 可写的 git checkout。

`capture_stale_after_min`、host 清单、身份映射和 liveness 新鲜度都只以公开
`device-liveness.json` 为准；dashboard 与 watchdog 每轮热读同一文件。

状态矩阵：设备 offline/sleep + 旧采集快照 = 看板断线、不告警；设备 fresh online
+ 快照超过 30 分钟 = `capture_probe_stale`；快照新鲜且有 red blocker = 仍告警；
工具长期无新数据 = amber/idle，不告警。只有 Tailscale 信号或 topology API 本轮
unknown 时业务告警沿用上一轮状态，同时单独报 `capture_monitor_unhealthy`；明确
offline/sleep 会清除该 host 的旧 stale/blocker。
多 host 按“配置清单 ∪ 已有快照”聚合，offline 不会盖掉另一台的 fresh blocker 或
unknown 状态。
