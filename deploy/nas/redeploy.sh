#!/usr/bin/env bash
# 一键把本地源码部署到 NAS 的 kg-hub-server 容器并重启、探活。
#
# 为什么需要它：kg_hub_server.py 的源码是 build 时 COPY 进 Docker 镜像的，
# 改完代码必须「同步到 NAS → 重建镜像 → 重启容器」才生效。这个脚本把那串
# NAS 细节（主机、路径、project 名 kg-hub、ContainerManager 的 docker 路径、
# sudo、--no-deps 不动 falkordb）封一次，以后加报表/改 server 只跑这一条。
#
# 用法：
#   deploy/nas/redeploy.sh                 # 同步默认文件 + 重建重启 + 探活
#   FILES="kg_hub_server.py schema.py" deploy/nas/redeploy.sh   # 多文件
set -euo pipefail

NAS="${KG_HUB_NAS_SSH:-commiao@100.123.208.32}"
SRC="/volume1/docker/kg-hub-src"
DK="sudo -n /var/packages/ContainerManager/target/usr/bin/docker"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
FILES="${FILES:-kg_hub_server.py}"   # 报表跨多文件时用 FILES 环境变量覆盖

echo "[1/3] 同步源码到 NAS（原子 tmp+mv）"
for f in $FILES; do
  printf '      %s … ' "$f"
  cat "$REPO/$f" | ssh -o BatchMode=yes "$NAS" \
    "mkdir -p \"$SRC/$(dirname "$f")\" && cat > \"$SRC/.dep.tmp\" && mv -f \"$SRC/.dep.tmp\" \"$SRC/$f\" && echo ok"
done

echo "[2/3] 重建镜像 + 重启容器（project=kg-hub，不动 falkordb）"
ssh -o BatchMode=yes -o ConnectTimeout=20 "$NAS" \
  "cd $SRC && $DK compose build kg_hub_server >/dev/null 2>&1 && \
   $DK compose -p kg-hub up -d --no-deps kg_hub_server watchdog ingester >/dev/null 2>&1 && echo '      done'"

echo "[3/3] 探活"
# shellcheck disable=SC1090
source "$HOME/.claude-mem/.env" 2>/dev/null || true
URL="${KG_HUB_URL:-http://100.123.208.32:17171}"
sleep 4
for i in 1 2 3 4 5; do
  code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "$URL/health" || true)
  [ "$code" = "200" ] && break; sleep 3
done
portal=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$URL/portal" || true)
echo "      health=$code  portal=$portal"
echo "→ 打开 $URL/portal"
