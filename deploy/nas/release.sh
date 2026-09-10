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
# 部署锁。这个工作区是多 actor 的（claude-code / codex / 用户本人），两个发布同时
# 跑会互相覆盖 .env、抢同一批容器。mkdir 在同一文件系统上是原子的，够用。
LOCK="$SRC/.release.lock"
producers_stopped=0
restore_producers() { :; }   # 真正的实现在排空那一步覆盖它；trap 早于它设置
acquire_lock() {
  [ "${DRY_RUN:-0}" = 1 ] && return 0
  ssh "${SSH_OPTS[@]}" "$NAS" "set -eu
    if mkdir '$LOCK' 2>/dev/null; then
      printf '%s %s %s\n' \"\$(date -Iseconds)\" '$(hostname -s)' \"\$\$\" > '$LOCK/owner'
      exit 0
    fi
    # 超过 40 分钟的锁判为上一次发布崩了留下的：构建最长也就十几分钟。
    if [ -n \"\$(find '$LOCK' -maxdepth 0 -mmin +40 2>/dev/null)\" ]; then
      rm -rf '$LOCK'; mkdir '$LOCK'
      printf '%s %s %s (抢占了超时的旧锁)\n' \"\$(date -Iseconds)\" '$(hostname -s)' \"\$\$\" > '$LOCK/owner'
      exit 0
    fi
    echo \"另一个发布正在进行：\$(cat '$LOCK/owner' 2>/dev/null)\" >&2
    exit 75" || die "拿不到部署锁"
  # 退出时释放锁；生产者若还停着也一并起回来（die 会走到这里）。
  trap 'restore_producers 2>/dev/null || true
        ssh "${SSH_OPTS[@]}" "$NAS" "rm -rf '"'$LOCK'"'" >/dev/null 2>&1 || true' EXIT
}
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

acquire_lock

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
  say "[1/6] git archive $SHA → $SRC"
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
say "[2/6] 兜住 .env（关掉「变量缺失」窗口）+ 准备数据目录"
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
  say "[3/6] 构建 kg-hub-server:$SHA（不动 latest）"
  on_nas "cd $SRC && $DK build -t kg-hub-server:$SHA -f deploy/nas/Dockerfile ." \
    || die "构建失败；线上未改动"
else
  say "[3/6] 回滚不重建，直接用盘上已有的 kg-hub-server:$SHA"
  on_nas "$DK image inspect kg-hub-server:$SHA >/dev/null" \
    || die "回滚目标镜像 kg-hub-server:$SHA 已不在盘上"
fi

# ---- 4.5 排空：先停生产者，等在飞的抽取跑完 -------------------------------
# 直接 `up -d` 会把正在跑的抽取连容器一起换掉。抽取是流式的，中途断开会在网关
# 留下 `unknown` 记录——那种记录**永远不会过期**（网关只删 completed/error，因为
# 过期不能证明供应商没扣过钱），每一条都挡住下一次发布。2026-09-10 六小时攒了
# 171 条正是这么来的：发布本身会制造挡住下次发布的东西。
# 排空等的是**在飞的抽取**，不是积压。积压躺在库里和水印里，refinery 停了就停，
# 回来接着跑，一条不丢。在飞的条数由 INGEST_CONCURRENCY 封顶（现为 2），每条约
# 3 分钟，所以正常情况下最坏等 3 分钟左右。
say "[4/6] 排空：停生产者，等在飞的抽取归零（等的是在飞，不是积压）"
producers_stopped=0
# 生产者一旦停下，就必须保证它们能起回来：发布在这之后任何一步失败而没人管，
# 整条采集就静悄悄停了，比发布失败本身严重得多。
restore_producers() {
  [ "$producers_stopped" = 1 ] || return 0
  say "  恢复生产者 refinery / ingester"
  on_nas "cd $SRC && $DK compose -p $PROJECT start refinery ingester" \
    || say "  ⚠ 生产者没起回来，需要人工：docker compose -p $PROJECT start refinery ingester"
  producers_stopped=0
}
if on_nas "cd $SRC && $DK compose -p $PROJECT stop -t 30 refinery ingester"; then
  producers_stopped=1
else
  say "  （生产者没停成，继续——最坏是多等一会儿）"
fi
drained=0
for _ in $(seq 1 60); do
  [ "${DRY_RUN:-0}" = 1 ] && { drained=1; break; }
  # 用 python 解 JSON 而不是 sed 抠字符串：字段缺失和值为 0 必须能分清，
  # 前者说明线上还是旧版本（要盲等），后者才是真的排空了。
  n=$(on_nas "curl -fsS -m 5 '$HEALTH' 2>/dev/null" 2>/dev/null | python3 -c '
import json,sys
try: print(json.load(sys.stdin)["active_extractions"])
except Exception: pass' 2>/dev/null || true)
  if [ -z "$n" ]; then
    # 线上还是旧版本，没有这个字段。只能盲等一个路由超时（150s）+ 余量。
    say "  线上版本还没有 active_extractions，改为盲等 180s（下次发布起就精确了）"
    sleep 180; drained=1; break
  fi
  if [ "$n" = 0 ]; then say "  在飞抽取已归零"; drained=1; break; fi
  say "  还有 $n 条在飞，等…"
  sleep 5
done
if [ "$drained" != 1 ]; then
  # 排空不掉就**别发**。硬换会掐断在飞的流式抽取，在网关留下永不过期的记录 ——
  # 那正是这一步要避免的东西，为了赶一次发布去制造它不划算。此刻源码已同步、
  # 镜像已构建，但容器还没换，中止是干净的：过会儿重跑即可。
  restore_producers
  [ "${KG_HUB_FORCE_SWAP:-0}" = 1 ] \
    || die "5 分钟没排空干净，已中止（生产者已恢复）。确认可以掐断就 KG_HUB_FORCE_SWAP=1 重跑"
  say "  ⚠ KG_HUB_FORCE_SWAP=1：明知会掐断仍继续"
fi

# ---- 5. 切标签 + 起容器 ----------------------------------------------------
say "[5/6] 切到 $SHA 并启动 $SERVICES"
on_nas "set -eu
  cd $SRC
  # .env 里同时记住上一次的标签：回滚不需要人去翻历史。
  tmp=\$(mktemp '$SRC/.env.XXXXXX')
  grep -v '^KG_HUB_IMAGE_TAG' .env > \"\$tmp\" 2>/dev/null || true
  printf 'KG_HUB_IMAGE_TAG=%s\nKG_HUB_IMAGE_TAG_PREV=%s\n' '$SHA' '$PREV' >> \"\$tmp\"
  chmod 600 \"\$tmp\"; mv -f \"\$tmp\" .env
  $DK compose -p $PROJECT up -d --no-deps --no-build $SERVICES"

# ---- 6. 验收；不合格自动回到上一个标签 ------------------------------------
# curl /health 只能证明"有个东西在听"，证明不了跑的是我们刚建的那个镜像
# （compose 可能压根没重建容器）。所以先比对镜像 ID。
say "[6/6] 验收：镜像 ID 比对 + 健康检查"
if [ "${DRY_RUN:-0}" != 1 ]; then
  want=$(on_nas "$DK image inspect -f '{{.Id}}' kg-hub-server:$SHA" 2>/dev/null || true)
  got=$(on_nas "$DK inspect -f '{{.Image}}' kg-hub-server" 2>/dev/null || true)
  if [ -z "$want" ] || [ "$want" != "$got" ]; then
    say "  ⚠ 容器跑的不是 kg-hub-server:$SHA（want=$want got=$got）"
    ok=0
  fi
fi
# 镜像 ID 不符时上面已把 ok 置 0；这里不要覆盖掉那个判决。
image_ok=${ok:-1}
ok=0
[ "$image_ok" = 0 ] && say "  镜像 ID 不符，跳过健康检查直接回滚"
for _ in $(seq 1 30); do
  [ "$image_ok" = 0 ] && break
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

producers_stopped=0   # 上一步的 up -d 已经把它们带起来了
say "✅ 发布完成：kg-hub-server:$SHA（上一个 $PREV 仍在盘上，可 --rollback）"
