# kg-hub → 群晖 NAS 迁移指南

把 KG Hub(FalkorDB + kg_hub_server)从 Mac 迁到群晖 NAS(i3 / 10G+),作为常驻、可持久化、跨设备的中心。

## 架构

```
群晖 NAS (always-on)
  ├─ falkordb       :6379  仅绑 127.0.0.1(不对外)+ AOF + 查询超时
  ├─ kg_hub_server  :17171  Bearer 鉴权,连 falkordb:6379(走容器内网/loopback)
  └─ 数据 ${KG_HUB_DATA_ROOT}/  + 群晖 Btrfs 快照
        ▲ 粗粒度 HTTP /api/ingest、/api/search,经 Tailscale
   各 Mac / 笔记本 / OpenClaw-VPS = 瘦客户端
```

碎流量(Graphiti↔FalkorDB)留在 NAS 内部;跨设备只走粗粒度 HTTP。

---

## 前置

1. DSM 7.2+,装 **Container Manager**(旧版叫 Docker)套件。
2. 共享文件夹用 **Btrfs**(支持快照)。本部署沿用唯一数据根
   `KG_HUB_DATA_ROOT=/volume2/4T/kg-hub-data`；创建
   `$KG_HUB_DATA_ROOT/falkordb`(图数据)和 `$KG_HUB_DATA_ROOT/models`(模型缓存)。
3. NAS 与各设备装 **Tailscale**(套件中心可装),`tailscale up` 入同一 tailnet。
4. NAS 能联网(首次构建镜像 + fastembed 首次下载模型需要)。

---

## 步骤

### 1. 拷代码到 NAS
把整个 `kg-hub/` 仓库放到 NAS,例如 `/volume1/docker/kg-hub-src/`。
(`.dockerignore` 已排除 `.venv`/`data`/`.git`/`logs`/`spike-graphiti`,上传可只传源码。)

### 2. 配置 .env
```sh
cd /volume1/docker/kg-hub-src
# 不要先 cp .env.example；helper 会用 0600 临时文件原子创建 deploy/nas/.env。
sh deploy/nas/configure-model-gateway-token.sh /absolute/path/to/caller-token-kg-hub
# 然后核对 KG_HUB_DATA_ROOT；保持 deploy/nas/.env 权限为 0600。
# ANTHROPIC_BASE_URL 只保留 model-gateway 服务 DNS，MODEL 必须是 kg_hub.* 业务 key。
# 禁止从 ~/.claude-mem/.env 复制任何模型/provider token。
```

helper 从 `.env.example` 初始化时会用系统 CSPRNG 自动生成彼此独立的
`FALKORDB_PASSWORD` 与 `KG_HUB_API_TOKEN`，并从给定的 0600 文件写入
`KG_HUB_MODEL_GATEWAY_TOKEN`。已有的强自定义密码/token 会原样保留；弱的非占位自定义值会让 helper 拒绝修改，需操作者明确更换后重跑。helper 不打印任何 secret。
若旧部署只有仓库根目录的 0600 `.env`，helper 会只迁移既有的
`FALKORDB_PASSWORD`、`KG_HUB_API_TOKEN` 和其他非 provider 配置；任何 provider API key
都会被删除。首次从旧根目录 `.env` 迁移时，旧的直连
`ANTHROPIC_BASE_URL`/`ANTHROPIC_MODEL` 会改成模板网关值；一旦
`deploy/nas/.env` 建立，helper 仅补缺失/占位配置，已有的数据根、网关地址、
业务 Key、私网名及其他非 provider 配置均原样保留。
为避免未同步轮换导致现有 FalkorDB 立即失联，首次迁移允许原样保留旧 `.env` 中可打印、
非占位但未达到新强度标准的既有服务密码，并只输出不含值的轮换提醒；一旦
`deploy/nas/.env` 建立，后续重跑仍严格拒绝弱值。旧 `.env` 本身保持不变，确认新部署
稳定后再单独安排密码轮换及归档/删除。

### 3. 构建并启动
```sh
cd /volume1/docker/kg-hub-src
# 该 external network 必须由操作者预先创建；Compose 不会自动创建或发布 model-gateway。
docker network create model-gateway-private
docker compose --env-file deploy/nas/.env \
  -f docker-compose.yml -f deploy/model-gateway-network.override.yml \
  -p kg-hub build
docker compose --env-file deploy/nas/.env \
  -f docker-compose.yml -f deploy/model-gateway-network.override.yml \
  -p kg-hub up -d falkordb # 先只起 DB,准备导入数据(见步骤 4)
```
> 若 external network 已存在，`docker network create` 返回“already exists”后直接继续。
> 所有 Compose 命令都必须从仓库根目录运行，并显式带同一 env-file 与两个 compose 文件。

### 4. 迁移本机重建好的图数据(免在 NAS 重跑 LLM)
本机图重建完成后,把数据文件整目录拷过去(含 RDB **和** AOF,缺一不可——开了 AOF 时 redis 优先从 AOF 恢复):

**在 Mac 上:**
```sh
# 落盘并停掉本机 DB,保证文件一致
docker exec kg-hub-falkordb redis-cli --no-auth-warning -a "$FALKORDB_PASSWORD" SAVE
docker stop kg-hub-falkordb
# 整目录拷到 NAS(dump.rdb + appendonlydir)
KG_HUB_DATA_ROOT=/volume2/4T/kg-hub-data
rsync -av /Users/mac/workspace_claudeCode/kg-hub/data/falkordb/ \
  "<nas-tailscale>:$KG_HUB_DATA_ROOT/falkordb/"
```
**在 NAS 上:** 确保拷入后,重启 falkordb 让其加载:
```sh
docker compose --env-file deploy/nas/.env \
  -f docker-compose.yml -f deploy/model-gateway-network.override.yml \
  -p kg-hub restart falkordb
# 校验节点数应与本机一致
docker exec kg-hub-falkordb redis-cli --no-auth-warning -a "$FALKORDB_PASSWORD" \
  GRAPH.QUERY kg_hub "MATCH (n) RETURN count(n)"
```

### 5. 起 server
```sh
docker compose --env-file deploy/nas/.env \
  -f docker-compose.yml -f deploy/model-gateway-network.override.yml \
  -p kg-hub up -d
curl -s http://localhost:17171/health
```

### 6. 切客户端到 NAS
- 查询客户端(`openclaw-deploy/scripts/kg-query.sh` 等):`KG_HUB_URL=http://<nas-tailscale>:17171`,`KG_HUB_TOKEN` 用同一 token。
- claude-mem 摄入:让 hook 把新 observation **POST 到 `http://<nas-tailscale>:17171/api/ingest`**(碎活落 NAS)。
  > 注:当前 `ingesters/claude_mem_obs.py` 是**直连 FalkorDB**(`add_episode`),不是走 HTTP。持续摄入要改成 POST `/api/ingest`,或把 claude-mem.db 同步到 NAS 在本地跑 ingester。此项作为迁移后的收尾任务单独做。
- 退役 Mac 本机的 falkordb 容器。

### 7. 安全
- FalkorDB 6379 已 `127.0.0.1` 绑定,**不对外**。
- 对外端口 **17171**(容器内仍 8080)**只经 Tailscale 访问**:已 `127.0.0.1:17171` 绑定环回,Tailscale 用户态自动转发 `tailscale-ip:17171 → 127.0.0.1:17171`;**不要**在路由器做端口转发。
- API token 与 FalkorDB 密码定期轮换。

### 8. 备份(补上 DESIGN 里没做完的"中央 KG 备份策略")
- DSM **快照副本(Snapshot Replication)**:对 `$KG_HUB_DATA_ROOT` 设每日快照 + 保留策略。
- 或 **Hyper Backup** 定时备份该目录到外部/云。
- 因为开了 AOF,崩溃最多丢 1 秒写入;快照提供时间点回滚。

---

## 回滚
迁移失败时,Mac 端 `docker start kg-hub-falkordb` 即恢复(本机数据目录未动)。确认 NAS 正常后再退役本机。

## 现状基线(2026-06-08)
- 数据真源:`~/.claude-mem/claude-mem.db`(4632 obs),图为其派生,可重建。
- 本机已修复:挂载 → `/var/lib/falkordb/data`、RDB + AOF、查询超时 5s/10s。
- 依赖锁定:见 `requirements.txt`(Python 3.13,graphiti-core 0.29.0,fastembed 0.8.0)。
