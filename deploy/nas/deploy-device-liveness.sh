#!/usr/bin/env bash
# 部署并试跑 NAS host-side Tailscale 快照生产者（不在容器里伪造在线态）。
set -euo pipefail

NAS="${KG_HUB_NAS_SSH:-commiao@100.123.208.32}"
SRC="${KG_HUB_NAS_SRC:-/volume1/docker/kg-hub-src}"
REPO="${KG_HUB_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
REL="deploy/monitoring/nas/tailscale-liveness-snapshot.sh"
MAP_REL="deploy/monitoring/nas/device-liveness.json.example"
OUT="${KG_HUB_DEVICE_LIVENESS_PATH:-/volume2/4T/kg-hub-data/device-liveness/tailscale-status.json}"
MAP_OUT="${KG_HUB_DEVICE_LIVENESS_CONFIG_HOST:-/volume2/4T/kg-hub-data/device-liveness/device-liveness.json}"
REMOTE_SUDO="${KG_HUB_NAS_SUDO-sudo -n}"
TAILSCALE_BIN="${KG_HUB_TAILSCALE_BIN:-}"
JSON_PYTHON="${KG_HUB_JSON_PYTHON:-}"
INSTALL_DIR="${KG_HUB_LIVENESS_INSTALL_DIR:-/usr/local/libexec/kg-hub}"
INSTALL_PATH="$INSTALL_DIR/tailscale-liveness-snapshot.sh"
DEPLOY_FILES="topology.py tools/watchdog.py utils/device_liveness.py docker-compose.yml $REL $MAP_REL"

echo "[1/3] root 安装 producer 与公开设备身份配置"
cat "$REPO/$REL" | ssh -o BatchMode=yes -o ConnectTimeout=20 "$NAS" \
  "$REMOTE_SUDO install -d -o root -g root -m 0755 '$INSTALL_DIR' && \
   $REMOTE_SUDO tee '$INSTALL_PATH.upload' >/dev/null && \
   $REMOTE_SUDO install -o root -g root -m 0755 '$INSTALL_PATH.upload' '$INSTALL_PATH' && \
   $REMOTE_SUDO rm -f '$INSTALL_PATH.upload'"
cat "$REPO/$MAP_REL" | ssh -o BatchMode=yes -o ConnectTimeout=20 "$NAS" \
  "$REMOTE_SUDO install -d -o root -g root -m 0755 '$(dirname "$MAP_OUT")' && \
   if [ -d '$MAP_OUT' ]; then $REMOTE_SUDO rmdir '$MAP_OUT'; fi && \
   if [ ! -f '$MAP_OUT' ]; then \
     $REMOTE_SUDO tee '$MAP_OUT.upload' >/dev/null && \
     $REMOTE_SUDO install -o root -g root -m 0640 '$MAP_OUT.upload' '$MAP_OUT' && \
     $REMOTE_SUDO rm -f '$MAP_OUT.upload'; \
   else cat >/dev/null; fi"

echo "[2/3] 立即采样并验证输出"
ssh -o BatchMode=yes -o ConnectTimeout=20 "$NAS" \
  "if [ -d '$OUT' ]; then $REMOTE_SUDO rmdir '$OUT'; fi && \
   $REMOTE_SUDO env KG_HUB_DEVICE_LIVENESS_PATH='$OUT' \
   KG_HUB_TAILSCALE_BIN='$TAILSCALE_BIN' KG_HUB_JSON_PYTHON='$JSON_PYTHON' \
   '$INSTALL_PATH' && \
   test -s '$OUT' && stat '$OUT'"

echo "[3/3] 同步 server/watchdog/compose 并 recreate"
FILES="$DEPLOY_FILES" KG_HUB_NAS_SSH="$NAS" KG_HUB_NAS_SRC="$SRC" \
  KG_HUB_REPO="$REPO" bash "$REPO/deploy/nas/redeploy.sh"

echo
echo "一次性在 DSM 控制面板 → 任务计划新增 root『用户定义的脚本』，每分钟执行："
echo "  KG_HUB_DEVICE_LIVENESS_PATH='$OUT'${TAILSCALE_BIN:+ KG_HUB_TAILSCALE_BIN='$TAILSCALE_BIN'}${JSON_PYTHON:+ KG_HUB_JSON_PYTHON='$JSON_PYTHON'} '$INSTALL_PATH'"
echo "容器只读消费 $OUT；连续 3 分钟无新 mtime 会自动降级 unknown。"
