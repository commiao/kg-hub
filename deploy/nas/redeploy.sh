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
SRC="${KG_HUB_NAS_SRC:-/volume1/docker/kg-hub-src}"
DK="${KG_HUB_DOCKER:-sudo -n /var/packages/ContainerManager/target/usr/bin/docker}"
REPO="${KG_HUB_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
FILES="${FILES:-kg_hub_server.py}"   # 报表跨多文件时用 FILES 环境变量覆盖

echo "[1/3] 同步源码到 NAS（原子 tmp+mv）"
for f in $FILES; do
  printf '      %s … ' "$f"
  cat "$REPO/$f" | ssh -o BatchMode=yes "$NAS" \
    "mkdir -p \"$SRC/$(dirname "$f")\" && cat > \"$SRC/.dep.tmp\" && mv -f \"$SRC/.dep.tmp\" \"$SRC/$f\" && echo ok"
done

echo "[2/3] 重建镜像 + 重启容器（project=kg-hub，不动 falkordb）"
# ⚠️ 四个已踩过的坑，都在这里一次性处理（2026-08-25 固化）：
#   ① device_liveness/watchdog/ingester/refinery 共用 kg-hub-server:latest 镜像，重建后它们会被
#      compose 留在 **Created** 未启动状态 —— 症状极具迷惑性：tailnet ping 通、
#      容器"存在"，但 HTTP 立即 000（连接拒绝而非超时），像是网络故障。
#   ② compose 重建偶尔留下重名幽灵容器（如 235aa24cc52e_kg-hub-refinery），
#      占着名字不干活，需 rm -f。
#   ③ refinery 此前不在重启列表里，改了 kg_refinery.py 却不生效。
#   ④ 清幽灵若放在 compose up 之后，up 一冲突就被 set -e 提前截断，清理逻辑
#      永远执行不到；2026-08-25 因此让 server/watchdog/ingester 全停在 Created。
ssh -o BatchMode=yes -o ConnectTimeout=20 "$NAS" "
  set -eu
  cleanup_ghosts() {
    ghosts=''
    for n in \$($DK ps -a --format '{{.Names}}'); do
      case \"\$n\" in ????????????_kg-hub-*) ghosts=\"\$ghosts \$n\" ;; esac
    done
    if [ -n \"\$ghosts\" ]; then
      echo \"      清幽灵:\$ghosts\"
      timeout -k 5 30 $DK rm -f \$ghosts >/dev/null 2>&1
    fi
  }
  start_created() {
    stuck=''
    for n in \$($DK ps -a --filter status=created --format '{{.Names}}'); do
      case \"\$n\" in kg-hub-*) stuck=\"\$stuck \$n\" ;; esac
    done
    if [ -n \"\$stuck\" ]; then
      echo \"      拉起 Created:\$stuck\"
      timeout -k 5 30 $DK start \$stuck >/dev/null 2>&1
    fi
  }
  recreate_service() {
    service=\"\$1\"
    case \"\$service\" in
      device_liveness) container='kg-hub-device-liveness' ;;
      kg_hub_server) container='kg-hub-server' ;;
      *) container=\"kg-hub-\$service\" ;;
    esac
    # 不用 compose ps：NAS 上该插件会被历史残留客户端锁住；raw docker ps 不受影响。
    if ! ids=\$(timeout -k 5 30 $DK ps -a --filter \"name=^/\$container\$\" -q); then
      echo \"      查询 \$service 超时，终止本轮部署\"
      return 1
    fi
    if [ -n \"\$ids\" ]; then
      echo \"      重建 \$service\"
      if ! timeout -k 5 30 $DK rm -f \$ids >/dev/null 2>&1; then
        echo \"      删除 \$service 超时，终止本轮部署\"
        return 1
      fi
    fi
    cleanup_ghosts
    log=\"/tmp/kg-hub-deploy-\$service.log\"
    if ! timeout -k 5 90 $DK compose -p kg-hub up -d --no-deps \"\$service\" >\"\$log\" 2>&1; then
      cat \"\$log\"; rm -f \"\$log\"
      echo \"      \$service 首次拉起失败，清理后重试\"
      cleanup_ghosts
      start_created
      if ! timeout -k 5 90 $DK compose -p kg-hub up -d --no-deps \"\$service\" >\"\$log\" 2>&1; then
        cat \"\$log\"; rm -f \"\$log\"
        return 1
      fi
    fi
    cat \"\$log\"; rm -f \"\$log\"
  }

  cd $SRC
  stale=\$(ps -ef | awk '
    \$8 ~ /docker/ && \$9 == \"compose\" {
      for (i = 10; i <= NF; i++) if (\$i == \"kg-hub\") found++
    }
    END { print found + 0 }
  ')
  if [ \"\$stale\" -gt 0 ]; then
    echo \"      发现 \$stale 个残留 kg-hub compose 客户端，部署前置检查失败\"
    exit 1
  fi

  build_log='/tmp/kg-hub-deploy-build.log'
  if ! timeout -k 10 300 $DK compose build kg_hub_server >\"\$build_log\" 2>&1; then
    cat \"\$build_log\"; rm -f \"\$build_log\"
    exit 1
  fi
  cat \"\$build_log\"; rm -f \"\$build_log\"

  # NAS Compose 批量 recreate 会在重命名 refinery 后卡死且不返回。显式逐个删除
  # 旧容器再创建，避免进入 rename 路径；server 接近最后处理，将 HTTP 中断压到最短；
  # watchdog 必须最后启动，避免它在 server 重建窗口发出部署诱发的 server_down。
  cleanup_ghosts
  trap 'cleanup_ghosts; start_created' EXIT
  for service in device_liveness ingester refinery kg_hub_server watchdog; do
    recreate_service \"\$service\"
  done
  cleanup_ghosts
  start_created
  trap - EXIT
  echo '      up done'
"

echo "[3/3] 探活"
# shellcheck disable=SC1090
if [ -r "$HOME/.claude-mem/.env" ]; then
  source "$HOME/.claude-mem/.env"
fi
URL="${KG_HUB_URL:-http://100.123.208.32:17171}"
sleep 4
for i in 1 2 3 4 5; do
  code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "$URL/health" || true)
  [ "$code" = "200" ] && break; sleep 3
done
portal=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$URL/portal" || true)
echo "      health=$code  portal=$portal"
echo "→ 打开 $URL/portal"
