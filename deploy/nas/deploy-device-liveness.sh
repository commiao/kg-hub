#!/usr/bin/env bash
# 部署并试跑 NAS Tailscale 快照生产者（独立容器读取真实 LocalAPI）。
set -euo pipefail

NAS="${KG_HUB_NAS_SSH:-commiao@100.123.208.32}"
SRC="${KG_HUB_NAS_SRC:-/volume1/docker/kg-hub-src}"
REPO="${KG_HUB_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
REL="deploy/monitoring/nas/tailscale-liveness-snapshot.sh"
MAP_REL="deploy/monitoring/nas/device-liveness.json.example"
MAP_OUT="${KG_HUB_DEVICE_LIVENESS_CONFIG_HOST:-/volume2/4T/kg-hub-data/device-liveness/device-liveness.json}"
DEPLOY_FILES="topology.py tools/watchdog.py utils/device_liveness.py docker-compose.yml $REL $MAP_REL"

echo "[1/3] 安装公开设备身份配置（已存在则保留）"
cat "$REPO/$MAP_REL" | ssh -o BatchMode=yes -o ConnectTimeout=20 "$NAS" \
  "set -eu; install -d -m 0755 '$(dirname "$MAP_OUT")' && \
   if [ ! -f '$MAP_OUT' ]; then \
     cat > '$MAP_OUT.upload' && chmod 0640 '$MAP_OUT.upload' && mv '$MAP_OUT.upload' '$MAP_OUT'; \
   else cat >/dev/null; fi"

echo "[2/3] 同步 producer/server/watchdog/compose 并 recreate"
FILES="$DEPLOY_FILES" KG_HUB_NAS_SSH="$NAS" KG_HUB_NAS_SRC="$SRC" \
  KG_HUB_REPO="$REPO" bash "$REPO/deploy/nas/redeploy.sh"

echo "[3/3] 验证 producer 已生成快照"
ssh -o BatchMode=yes -o ConnectTimeout=20 "$NAS" \
  "test -s '/volume2/4T/kg-hub-data/device-liveness/tailscale-status.json' && \
   stat '/volume2/4T/kg-hub-data/device-liveness/tailscale-status.json'"
echo "device_liveness 容器每分钟刷新快照；连续 3 分钟无新 mtime 会自动降级 unknown。"
