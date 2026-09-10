#!/usr/bin/env bash
# 按 git commit 发布 kg-hub 到 NAS。取代被禁用的 redeploy.sh。
#
# ## 为什么要重写而不是解禁旧脚本
#
# 旧的 redeploy.sh 在 2026-09-08 被禁用（T-0046），理由是「回滚不安全」：Compose
# 会按标签找到重命名过的旧容器备份并在启动前删掉它，所以新版起不来时旧容器也回
# 不来。但那只是症状。**根子在 docker-compose.yml 把五个服务都指向
# `kg-hub-server:latest`** —— 可变标签，新镜像一 build 就把旧的覆盖成悬空层，于是
# 回滚时压根不存在「旧版本」这个东西，只好去抢救旧**容器**，才有了那套脆弱的
# 重命名备份。
#
# 这里换成按 commit 打不可变标签：`kg-hub-server:<sha>`。旧镜像永远留在盘上，
# 回滚 = 把 .env 里的标签指回去再 up 一次。kg-hub 的容器本身不持有状态（模型、
# 备份、refinery-state、gateway-usage、device-liveness 全是 bind mount，图库是
# 另一个不动的容器），所以「从旧镜像重建」对它就是精确恢复 —— 这一点和
# credvault 的模型网关不同，那边有部署身份/见证语义，必须恢复到同一个容器。
#
# ## 为什么源码走 git archive 而不是打包工作区
#
# 旧脚本 `tar -cf - -C "$REPO" <files>` 打的是**工作区**：本地哪个文件脏了就把脏的
# 传上去，发布物和仓库里的任何一个 commit 都对不上。这里用 `git archive <commit>`，
# 发布的字节严格等于那个 commit，事后可复现、可对账。NAS 上没有装 git，也不需要装。
#
# ## 用法
#
#   deploy/nas/release.sh                 # 发布 HEAD
#   deploy/nas/release.sh <commit>        # 发布指定 commit
#   deploy/nas/release.sh --rollback      # 回到上一次发布的标签
#   DRY_RUN=1 deploy/nas/release.sh       # 只打印要做什么，不碰 NAS
set -euo pipefail

NAS="${KG_HUB_NAS_SSH:-commiao@100.123.208.32}"
SRC="${KG_HUB_NAS_SRC:-/volume1/docker/kg-hub-src}"
DATA="${KG_HUB_DATA_ROOT_NAS:-/volume2/4T/kg-hub-data}"
DK="${KG_HUB_DOCKER:-sudo -n /var/packages/ContainerManager/target/usr/bin/docker}"
PROJECT="${KG_HUB_COMPOSE_PROJECT:-kg-hub}"
HEALTH="${KG_HUB_HEALTH_URL:-http://127.0.0.1:17171/health}"
REPO=$(cd "$(dirname "$0")/../.." && pwd)
# 共用同一个镜像的全部服务。falkordb 不在其中：它是数据面，发布不碰它。
SERVICES="${KG_HUB_SERVICES:-kg_hub_server device_liveness watchdog ingester refinery}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)
[ "${KG_HUB_SSH_ALLOW_PROXYJUMP:-0}" = 1 ] || SSH_OPTS+=(-o ProxyJump=none)

say() { printf '%s\n' "$*" >&2; }
die() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
on_nas() {
  if [ "${DRY_RUN:-0}" = 1 ]; then say "  [dry-run] ssh: $*"; return 0; fi
  ssh "${SSH_OPTS[@]}" "$NAS" "$@"
}

# ---- 1. 确定要发布的 commit，并要求它在仓库里可追溯 ----------------------
mode=release
target=HEAD
case "${1:-}" in
  --rollback) mode=rollback ;;
  "") ;;
  *) target=$1 ;;
esac

if [ "$mode" = release ]; then
  cd "$REPO"
  SHA=$(git rev-parse --short=12 "$target^{commit}") \
    || die "解析不了 commit: $target"
  # 发布物必须已经在远端：否则线上跑着的东西在任何人的仓库里都找不到，
  # 出事时无从对账，也无法从别的机器复现。
  git merge-base --is-ancestor "$SHA" origin/main 2>/dev/null \
    || die "$SHA 还没推到 origin/main；先 git push 再发布"
  say "发布 commit $SHA（$(git log -1 --format=%s "$SHA")）"
fi

# ---- 2. 记下当前标签，作为回滚点 ------------------------------------------
PREV=$(on_nas "grep '^KG_HUB_IMAGE_TAG=' $SRC/.env 2>/dev/null | cut -d= -f2-" || true)
PREV=${PREV:-latest}
say "当前线上标签：$PREV"

if [ "$mode" = rollback ]; then
  PRIOR=$(on_nas "grep '^KG_HUB_IMAGE_TAG_PREV=' $SRC/.env 2>/dev/null | cut -d= -f2-" || true)
  [ -n "$PRIOR" ] || die "没有记录上一次的标签，无法自动回滚"
  SHA=$PRIOR
  say "回滚到：$SHA"
fi

# ---- 3. 源码：git archive → NAS（发布物严格等于那个 commit）---------------
if [ "$mode" = release ]; then
  say "[1/5] git archive $SHA → $SRC"
  if [ "${DRY_RUN:-0}" = 1 ]; then
    say "  [dry-run] 会传 $(cd "$REPO" && git archive "$SHA" | tar -t | wc -l | tr -d ' ') 个条目"
  else
    (cd "$REPO" && git archive --format=tar "$SHA") | ssh "${SSH_OPTS[@]}" "$NAS" \
      "set -eu
       stage=\$(mktemp -d '$SRC/.rel-stage.XXXXXX')
       trap 'rm -rf \"\$stage\"' EXIT
       tar -xf - -C \"\$stage\"
       test -s \"\$stage/kg_hub_server.py\"
       test -s \"\$stage/docker-compose.yml\"
       # 逐文件原子替换：中断的传输不会留下截断的 .py
       (cd \"\$stage\" && find . -type f -print) | while read -r f; do
         mkdir -p \"\$(dirname \"$SRC/\$f\")\"
         mv -f \"\$stage/\$f\" \"$SRC/\$f\"
       done
       echo '      源码就位'"
  fi
fi

# ---- 3.5 立刻让 .env 里有一个可用的标签 -----------------------------------
# 新的 compose 用 ${KG_HUB_IMAGE_TAG:?} 硬失败（不接受隐式 latest）。源码一换上去，
# 在本脚本写入新标签之前会有一个窗口：**任何人**（另一个 actor、NAS 上的脚本）在这
# 段时间里跑 docker compose 都会因为变量缺失而失败。构建要几分钟，窗口不能这么长。
# 所以源码落地后立刻把当前正在跑的标签补进 .env —— 值不变、行为不变，只是把窗口
# 关掉。真正的切换仍在第 4 步。
say "[2/5] 兜住 .env（关掉「变量缺失」窗口）+ 准备数据目录"
on_nas "set -eu
  cd $SRC
  grep -q '^KG_HUB_IMAGE_TAG=' .env 2>/dev/null || {
    tmp=\$(mktemp '$SRC/.env.XXXXXX')
    cat .env > \"\$tmp\" 2>/dev/null || true
    printf 'KG_HUB_IMAGE_TAG=%s\n' '$PREV' >> \"\$tmp\"
    chmod 600 \"\$tmp\"; mv -f \"\$tmp\" .env
    echo '      .env 已补上当前标签 $PREV'
  }"

# ---- 4. 新卷 + 构建不可变镜像 ---------------------------------------------
on_nas "mkdir -p '$DATA/breakers' && chmod 700 '$DATA/breakers'"

if [ "$mode" = release ]; then
  say "[3/5] 构建 kg-hub-server:$SHA（不动 latest）"
  on_nas "cd $SRC && $DK build -t kg-hub-server:$SHA -f deploy/nas/Dockerfile ." \
    || die "构建失败；线上未改动"
else
  say "[3/5] 回滚不重建，直接用盘上已有的 kg-hub-server:$SHA"
  on_nas "$DK image inspect kg-hub-server:$SHA >/dev/null" \
    || die "回滚目标镜像 kg-hub-server:$SHA 已不在盘上"
fi

# ---- 5. 切标签 + 起容器 ----------------------------------------------------
say "[4/5] 切到 $SHA 并启动 $SERVICES"
on_nas "set -eu
  cd $SRC
  # .env 里同时记住上一次的标签：回滚不需要人去翻历史。
  tmp=\$(mktemp '$SRC/.env.XXXXXX')
  grep -v '^KG_HUB_IMAGE_TAG' .env > \"\$tmp\" 2>/dev/null || true
  printf 'KG_HUB_IMAGE_TAG=%s\nKG_HUB_IMAGE_TAG_PREV=%s\n' '$SHA' '$PREV' >> \"\$tmp\"
  chmod 600 \"\$tmp\"; mv -f \"\$tmp\" .env
  $DK compose -p $PROJECT up -d --no-deps --no-build $SERVICES"

# ---- 6. 验收；不合格自动回到上一个标签 ------------------------------------
say "[5/5] 健康验收"
ok=0
for _ in $(seq 1 30); do
  if [ "${DRY_RUN:-0}" = 1 ]; then ok=1; break; fi
  if on_nas "curl -fsS -m 5 '$HEALTH' >/dev/null 2>&1"; then ok=1; break; fi
  sleep 2
done

if [ "$ok" != 1 ]; then
  say "健康检查未通过 —— 自动回到 $PREV"
  on_nas "set -eu
    cd $SRC
    tmp=\$(mktemp '$SRC/.env.XXXXXX')
    grep -v '^KG_HUB_IMAGE_TAG=' .env > \"\$tmp\" 2>/dev/null || true
    printf 'KG_HUB_IMAGE_TAG=%s\n' '$PREV' >> \"\$tmp\"
    chmod 600 \"\$tmp\"; mv -f \"\$tmp\" .env
    $DK compose -p $PROJECT up -d --no-deps --no-build $SERVICES" \
    || die "回滚也失败了；线上需要人工介入（旧镜像 kg-hub-server:$PREV 仍在盘上）"
  die "发布失败，已回到 $PREV"
fi

say "✅ 发布完成：kg-hub-server:$SHA（上一个 $PREV 仍在盘上，可 --rollback）"
