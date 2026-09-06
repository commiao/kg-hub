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
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o ProxyJump=none)
# Complete minimal cutover set. The image imports all four Python modules and
# Compose needs both manifests before attaching model callers to the private
# gateway network. Unrelated worktree files stay out of deployment.
FILES="${FILES:-kg_hub_server.py graphiti_client.py kg_hub_env.py model_gateway_client.py docker-compose.yml deploy/model-gateway-network.override.yml}"

if [ "${KG_HUB_SKIP_SYNC:-0}" = 1 ]; then
  echo "[1/3] 源码已完成校验同步，跳过重复传输"
else
  echo "[1/3] 同步源码到 NAS（原子 tmp+mv）"
  for f in $FILES; do
    printf '      %s … ' "$f"
    # Stream into an isolated archive staging directory.  The target is only
    # replaced after tar has consumed the complete file, so an interrupted SSH
    # stream cannot leave a truncated or concatenated Python module in place.
    tar -cf - -C "$REPO" "$f" | ssh "${SSH_OPTS[@]}" "$NAS" \
      "set -eu; stage=\$(mktemp -d \"$SRC/.dep-stage.XXXXXX\"); tar -xf - -C \"\$stage\"; test -s \"\$stage/$f\"; mkdir -p \"$SRC/$(dirname "$f")\"; mv -f \"\$stage/$f\" \"$SRC/$f\"; echo ok"
  done
fi

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
ssh "${SSH_OPTS[@]}" "$NAS" "
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
  staged=''
  rollback_staged() {
    [ -n \"\$staged\" ] || return 0
    echo '      新容器拉起失败，恢复部署前的原容器'
    rollback_failed=0
    # Free every original name/port before restoring any exact prior container.
    for record in \$staged; do
      container=\${record%%:*}
      if ids=\$(timeout -k 5 30 $DK ps -a --filter \"name=^/\$container\$\" -q) \
          && [ -n \"\$ids\" ]; then
        timeout -k 5 30 $DK rm -f \$ids >/dev/null 2>&1 || true
      fi
    done
    for record in \$staged; do
      container=\${record%%:*}
      remainder=\${record#*:}
      backup=\${remainder%%:*}
      was_running=\${remainder##*:}
      if ids=\$(timeout -k 5 30 $DK ps -a --filter \"name=^/\$backup\$\" -q) \
          && [ -n \"\$ids\" ]; then
        if ! timeout -k 5 30 $DK rename \$ids \"\$container\" >/dev/null; then
          echo \"      恢复 \$container 名称失败，保留 \$backup 供人工恢复\"
          rollback_failed=1
          continue
        fi
        if [ \"\$was_running\" = true ]; then
          if ! timeout -k 5 30 $DK start \"\$container\" >/dev/null; then
            echo \"      已恢复 \$container 但启动失败，需人工启动\"
            rollback_failed=1
          fi
        fi
      else
        echo \"      找不到回退容器 \${backup}，需人工恢复\"
        rollback_failed=1
      fi
    done
    staged=''
    [ \$rollback_failed -eq 0 ]
  }
  discard_staged() {
    discard_failed=0
    for record in \$staged; do
      remainder=\${record#*:}
      backup=\${remainder%%:*}
      if ! ids=\$(timeout -k 5 30 $DK ps -a --filter \"name=^/\$backup\$\" -q); then
        echo \"      无法查询已成功替换的暂存容器 \${backup}，保留供人工清理\"
        discard_failed=1
      elif [ -n \"\$ids\" ] \
          && ! timeout -k 5 30 $DK rm -f \$ids >/dev/null; then
        echo \"      无法删除已成功替换的暂存容器 \${backup}，保留供人工清理\"
        discard_failed=1
      fi
    done
    staged=''
    if [ \$discard_failed -ne 0 ]; then
      echo '      新容器已全部拉起，但有暂存容器需要人工清理'
    fi
    return 0
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
    backup=\"\$container.rollback-before-redeploy\"
    if ! backup_ids=\$(timeout -k 5 30 $DK ps -a \
        --filter \"name=^/\$backup\$\" -q); then
      echo \"      查询回退容器 \$backup 失败，终止本轮部署\"
      return 1
    fi
    if [ -n \"\$backup_ids\" ]; then
      echo \"      发现未处理的回退容器 \${backup}，拒绝覆盖\"
      return 1
    fi
    if [ -n \"\$ids\" ]; then
      echo \"      暂存旧容器并重建 \$service\"
      if ! was_running=\$(timeout -k 5 30 $DK inspect \
          -f '{{.State.Running}}' \$ids); then
        echo \"      查询 \$service 运行状态失败，终止本轮部署\"
        return 1
      fi
      if ! timeout -k 5 30 $DK stop \$ids >/dev/null 2>&1; then
        echo \"      停止旧 \$service 失败，终止本轮部署\"
        return 1
      fi
      if ! timeout -k 5 30 $DK rename \$ids \"\$backup\" >/dev/null; then
        if [ \"\$was_running\" = true ]; then
          timeout -k 5 30 $DK start \$ids >/dev/null 2>&1 || true
        fi
        echo \"      暂存 \$service 失败，原容器已恢复，终止本轮部署\"
        return 1
      fi
      staged=\"\$staged \$container:\$backup:\$was_running\"
    fi
    cleanup_ghosts
    log=\"/tmp/kg-hub-deploy-\$service.log\"
    if ! timeout -k 5 90 $DK compose --env-file deploy/nas/.env \
        -f docker-compose.yml -f deploy/model-gateway-network.override.yml \
        -p kg-hub up -d --no-deps \"\$service\" >\"\$log\" 2>&1; then
      cat \"\$log\"; rm -f \"\$log\"
      echo \"      \$service 首次拉起失败，清理后重试\"
      cleanup_ghosts
      start_created
      if ! timeout -k 5 90 $DK compose --env-file deploy/nas/.env \
          -f docker-compose.yml -f deploy/model-gateway-network.override.yml \
          -p kg-hub up -d --no-deps \"\$service\" >\"\$log\" 2>&1; then
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
  if ! timeout -k 10 300 $DK compose --env-file deploy/nas/.env \
      -f docker-compose.yml -f deploy/model-gateway-network.override.yml \
      -p kg-hub build kg_hub_server >\"\$build_log\" 2>&1; then
    cat \"\$build_log\"; rm -f \"\$build_log\"
    exit 1
  fi
  cat \"\$build_log\"; rm -f \"\$build_log\"

  # NAS Compose 批量 recreate 会在重命名 refinery 后卡死且不返回。逐个暂存
  # 原容器再创建；整批成功才删暂存，任何双重失败都恢复完全相同的旧容器。
  # 服务集与顺序取自 upstream：device_liveness 已从孤儿容器补成 compose 服务，
  # 漏掉它，一次带 --remove-orphans 的全量 up 会删掉正在正常工作的容器且无法重建；
  # watchdog 必须最后启动，避免它在 server 重建窗口发出部署诱发的 server_down。
  cleanup_ghosts
  trap 'status=\$?; rollback_staged || true; cleanup_ghosts || true; start_created || true; exit \$status' EXIT
  for service in device_liveness ingester refinery kg_hub_server watchdog; do
    recreate_service \"\$service\"
  done
  cleanup_ghosts
  start_created
  discard_staged
  trap - EXIT
  echo '      up done'
"

echo "[3/3] 探活"
# shellcheck disable=SC1090
if [ -r "${KG_HUB_ENV_FILE:-$REPO/deploy/nas/.env}" ]; then
  source "${KG_HUB_ENV_FILE:-$REPO/deploy/nas/.env}"
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
if [ "$code" != "200" ]; then
  echo "      最终健康检查失败（HTTP ${code}）" >&2
  exit 1
fi
