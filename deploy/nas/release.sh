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
#   deploy/nas/release.sh --refinery-window-22-08 [<commit>]
#                                         # 发布时仅把已验证的 22:00–10:00 改为 22:00–08:00
#   DRY_RUN=1 deploy/nas/release.sh       # 只打印要做什么，不碰 NAS
set -euo pipefail

NAS="${KG_HUB_NAS_SSH:-commiao@100.123.208.32}"
SRC="${KG_HUB_NAS_SRC:-/volume1/docker/kg-hub-src}"
DATA="${KG_HUB_DATA_ROOT_NAS:-/volume2/4T/kg-hub-data}"
DK="${KG_HUB_DOCKER:-sudo -n /var/packages/ContainerManager/target/usr/bin/docker}"
PROJECT="${KG_HUB_COMPOSE_PROJECT:-kg-hub}"
HEALTH="${KG_HUB_HEALTH_URL:-http://127.0.0.1:17171/health}"
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# 共用同一个镜像的全部服务。falkordb 不在其中：它是数据面，发布不碰它。
SERVICES="${KG_HUB_SERVICES:-kg_hub_server device_liveness watchdog ingester refinery}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)
[ "${KG_HUB_SSH_ALLOW_PROXYJUMP:-0}" = 1 ] || SSH_OPTS+=(-o ProxyJump=none)

say() { printf '%s\n' "$*" >&2; }
# 部署锁。这个工作区是多 actor 的（claude-code / codex / 用户本人），两个发布同时
# 跑会互相覆盖 .env、抢同一批容器。mkdir 在同一文件系统上是原子的，够用。
LOCK="$SRC/.release.lock"
producers_stopped=0
lock_acquired=0
window_change_requested=0
# 受控窗口变更的事务记录和备份都固定在 NAS 源码目录内。不要把备份路径通过
# SSH stdout 回传给调用端：网络可能在远端已原子写完 .env 后才断开，那时本地变量
# 为空，反而找不到唯一能恢复的旧配置。锁保证这组固定名字同一时刻只属于一个发布。
REFINERY_WINDOW_TXN_DIR="$SRC/.release-window-transaction"
REFINERY_WINDOW_TXN_RECORD="$REFINERY_WINDOW_TXN_DIR/active"
REFINERY_WINDOW_TXN_BACKUP="$REFINERY_WINDOW_TXN_DIR/.env.before"
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
  lock_acquired=1
  # 退出时释放锁；生产者若还停着也一并起回来（die 会走到这里）。
  # release_exit 还负责在失败时先恢复受控的 refinery 窗口配置。
  install_release_exit_traps
}
die() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
on_nas() {
  if [ "${DRY_RUN:-0}" = 1 ]; then say "  [dry-run] ssh: $*"; return 0; fi
  ssh "${SSH_OPTS[@]}" "$NAS" "$@"
}

# `--refinery-window-22-08` 是一个很窄的、随发布提交的配置变更。普通发布绝不
# 触碰这两个键；也不接受“顺手改成别的窗口”。备份的是整个 .env，因为同一发布还会
# 写镜像标签，失败时只有恢复完整旧文件才能让旧镜像按原配置启动。
#
# 事务记录内容是固定备份位置；它必须在 .env 被替换前落盘。若 SSH 在远端提交后
# 才断开，后续连接不依赖调用端内存，仍能按这个记录恢复。
recover_pending_refinery_window_transaction() {
  [ "${DRY_RUN:-0}" = 1 ] && return 0
  on_nas "set -eu
    cd '$SRC'
    txn='$REFINERY_WINDOW_TXN_DIR'
    record='$REFINERY_WINDOW_TXN_RECORD'
    backup='$REFINERY_WINDOW_TXN_BACKUP'
    if [ ! -e \"\$record\" ]; then
      # 备份写完、记录尚未落盘时进程可能崩溃；按本协议此时 .env 还没有被替换，
      # 所以仅清掉这个未激活的孤儿目录是安全的。
      if [ -e \"\$backup\" ]; then rm -f \"\$backup\"; fi
      rmdir \"\$txn\" 2>/dev/null || true
      exit 0
    fi
    test \"\$(cat \"\$record\")\" = \"\$backup\"
    test -f \"\$backup\"
    tmp=\$(mktemp '$SRC/.env.XXXXXX')
    cat \"\$backup\" > \"\$tmp\"
    chmod 600 \"\$tmp\"
    mv -f \"\$tmp\" .env
    rm -f \"\$record\" \"\$backup\"
    rmdir \"\$txn\"
  "
}

# 生产 compose 把数据根目录设为必填变量。旧容器在变量成为必填前已创建，因而
# 运行正常也不能证明 .env 具备下一次 `compose up/start` 所需的完整配置。只接受
# 本发布脚本已验证的数据目录；缺失时在任何生产者停机、窗口事务备份之前补齐，
# 这样后续失败回滚的完整 .env 也仍可被 Compose 解析。
ensure_compose_data_root() {
  if [ "${DRY_RUN:-0}" = 1 ]; then
    say "  [dry-run] 会校验 KG_HUB_DATA_ROOT；缺失时补为 $DATA"
    return 0
  fi

  on_nas "set -eu
    cd '$SRC'
    test -f .env
    count=\$(grep -c '^KG_HUB_DATA_ROOT=' .env || true)
    case \"\$count\" in
      0)
        tmp=\$(mktemp '$SRC/.env.XXXXXX')
        cat .env > \"\$tmp\"
        printf 'KG_HUB_DATA_ROOT=%s\\n' '$DATA' >> \"\$tmp\"
        chmod 600 \"\$tmp\"
        mv -f \"\$tmp\" .env
        ;;
      1)
        value=\$(sed -n 's/^KG_HUB_DATA_ROOT=//p' .env)
        test \"\$value\" = '$DATA'
        ;;
      *) exit 1 ;;
    esac
  " || die "KG_HUB_DATA_ROOT 缺失或与已验证的数据目录不一致"
}

# 旧容器创建时携带了独立的网关调用令牌，但历史 .env 未必记录它。Compose 把该
# 令牌设为必填，故在停生产者前从当前健康服务的容器配置中原地恢复；令牌不经本机
# stdout、日志或命令插值传递。已有 .env 值必须与运行中的服务一致，拒绝静默轮换。
ensure_compose_model_gateway_token() {
  if [ "${DRY_RUN:-0}" = 1 ]; then
    say "  [dry-run] 会校验 KG_HUB_MODEL_GATEWAY_TOKEN；缺失时从现有服务容器安全恢复"
    return 0
  fi

  on_nas "set -eu
    cd '$SRC'
    test -f .env
    live_token=\$($DK inspect -f '{{range .Config.Env}}{{println .}}{{end}}' kg-hub-server | sed -n 's/^KG_HUB_MODEL_GATEWAY_TOKEN=//p')
    live_count=\$(printf '%s\\n' \"\$live_token\" | sed '/^\$/d' | wc -l | tr -d ' ')
    test \"\$live_count\" = 1
    count=\$(grep -c '^KG_HUB_MODEL_GATEWAY_TOKEN=' .env || true)
    case \"\$count\" in
      0)
        tmp=\$(mktemp '$SRC/.env.XXXXXX')
        cat .env > \"\$tmp\"
        printf 'KG_HUB_MODEL_GATEWAY_TOKEN=%s\\n' \"\$live_token\" >> \"\$tmp\"
        chmod 600 \"\$tmp\"
        mv -f \"\$tmp\" .env
        ;;
      1)
        value=\$(sed -n 's/^KG_HUB_MODEL_GATEWAY_TOKEN=//p' .env)
        test \"\$value\" = \"\$live_token\"
        ;;
      *) exit 1 ;;
    esac
  " || die "KG_HUB_MODEL_GATEWAY_TOKEN 缺失、重复或与运行中服务不一致"
}

prepare_refinery_window_change() {
  [ "$window_change_requested" = 1 ] || return 0
  if [ "${DRY_RUN:-0}" = 1 ]; then
    say "  [dry-run] 会在锁内校验并把 KG_HUB_REFINERY_WINDOW_END 从 10 原子改为 8"
    return 0
  fi

  on_nas "set -eu
    cd '$SRC'
    test -f .env
    start_count=\$(grep -c '^KG_HUB_REFINERY_WINDOW_START=' .env || true)
    end_count=\$(grep -c '^KG_HUB_REFINERY_WINDOW_END=' .env || true)
    start=\$(sed -n 's/^KG_HUB_REFINERY_WINDOW_START=//p' .env)
    end=\$(sed -n 's/^KG_HUB_REFINERY_WINDOW_END=//p' .env)
    test \"\$start_count\" = 1
    test \"\$end_count\" = 1
    test \"\$start\" = 22
    test \"\$end\" = 10
    txn='$REFINERY_WINDOW_TXN_DIR'
    record='$REFINERY_WINDOW_TXN_RECORD'
    backup='$REFINERY_WINDOW_TXN_BACKUP'
    test ! -e \"\$txn\"
    mkdir \"\$txn\"
    chmod 700 \"\$txn\"
    backup_tmp=\$(mktemp \"\$txn/.env.before.XXXXXX\")
    cat .env > \"\$backup_tmp\"
    chmod 600 \"\$backup_tmp\"
    mv -f \"\$backup_tmp\" \"\$backup\"
    record_tmp=\$(mktemp \"\$txn/.active.XXXXXX\")
    printf '%s\\n' \"\$backup\" > \"\$record_tmp\"
    chmod 600 \"\$record_tmp\"
    mv -f \"\$record_tmp\" \"\$record\"
    tmp=\$(mktemp '$SRC/.env.XXXXXX')
    sed 's/^KG_HUB_REFINERY_WINDOW_END=10\$/KG_HUB_REFINERY_WINDOW_END=8/' .env > \"\$tmp\"
    chmod 600 \"\$tmp\"
    mv -f \"\$tmp\" .env
  " || die "只允许既有 KG_HUB_REFINERY_WINDOW_START=22、KG_HUB_REFINERY_WINDOW_END=10 的窗口切换"
  say "已在发布锁内暂存 refinery 窗口 22:00–08:00；失败会恢复旧 .env"
}

# 必须在任何旧镜像 compose 回滚前调用。成功恢复后才丢弃备份；失败则保留，避免把
# 唯一的旧配置删掉。
restore_refinery_window_env() {
  say "  先恢复窗口变更前的 .env"
  recover_pending_refinery_window_transaction
}

discard_refinery_window_backup() {
  [ "$window_change_requested" = 1 ] || return 0
  on_nas "set -eu
    record='$REFINERY_WINDOW_TXN_RECORD'
    backup='$REFINERY_WINDOW_TXN_BACKUP'
    txn='$REFINERY_WINDOW_TXN_DIR'
    test \"\$(cat \"\$record\")\" = \"\$backup\"
    test -f \"\$backup\"
    rm -f \"\$record\" \"\$backup\"
    rmdir \"\$txn\"
  " || die "发布成功但无法安全清理 .env 备份"
}

rollback_to_previous_image() {
  local reason="$1"
  say "$reason —— 自动回到 $PREV"
  # 这是刻意排在 compose 前面的：窗口变更和镜像切换是同一事务。
  restore_refinery_window_env || die "无法在镜像回滚前恢复旧 .env；停止自动回滚"
  on_nas "set -eu
    cd $SRC
    tmp=\$(mktemp '$SRC/.env.XXXXXX')
    grep -v '^KG_HUB_IMAGE_TAG=' .env > \"\$tmp\" 2>/dev/null || true
    printf 'KG_HUB_IMAGE_TAG=%s\\n' '$PREV' >> \"\$tmp\"
    chmod 600 \"\$tmp\"; mv -f \"\$tmp\" .env
    $DK compose -p $PROJECT up -d --no-deps --no-build $SERVICES" \
    || die "回滚也失败了；线上需要人工介入（旧镜像 kg-hub-server:$PREV 仍在盘上）"
}

release_exit() {
  local rc=$?
  # 清理途中再次收到中断时不能跳过恢复步骤。先忽略这三种信号，再撤销 EXIT
  # 本身，确保完整旧 .env、生产者和锁有机会按顺序收束。
  trap '' HUP INT TERM
  trap - EXIT
  if [ "$rc" -ne 0 ]; then
    restore_refinery_window_env || say "  ⚠ 未能自动恢复 .env 备份"
  fi
  restore_producers 2>/dev/null || true
  if [ "$lock_acquired" = 1 ]; then
    ssh "${SSH_OPTS[@]}" "$NAS" "rm -rf '$LOCK'" >/dev/null 2>&1 || true
  fi
  exit "$rc"
}

# 调用端（终端、SSH、自动化执行器）中断时也必须走同一条失败收束路径。
# 单独依赖 EXIT trap 不能处理被转发的 HUP/INT/TERM：那些信号会在子 ssh 退出后
# 直接结束 shell，留下尚未恢复的 .env 事务和发布锁。
install_release_exit_traps() {
  trap 'release_exit' EXIT
  trap 'die "发布被中断；正在恢复配置和生产者"' HUP INT TERM
}

# ---- 1. 确定要发布的 commit，并要求它在仓库里可追溯 ----------------------
main() {
mode=release
target=HEAD
target_set=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --rollback)
      [ "$mode" = release ] && [ "$target_set" = 0 ] || die "--rollback 不能和 commit 一起使用"
      mode=rollback
      ;;
    --refinery-window-22-08)
      [ "$window_change_requested" = 0 ] || die "--refinery-window-22-08 只能指定一次"
      window_change_requested=1
      ;;
    --*) die "未知参数：$1" ;;
    *)
      [ "$mode" = release ] && [ "$target_set" = 0 ] || die "只能指定一个发布 commit"
      target=$1
      target_set=1
      ;;
  esac
  shift
done
[ "$mode" = release ] || [ "$window_change_requested" = 0 ] \
  || die "窗口变更只能随正常发布执行，不能和 --rollback 一起使用"

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

# 崩溃在完成窗口变更之后、清理备份之前时，下一次拿到锁的连接先收束那一笔旧事务。
# 这一步只读取固定事务记录，不打印 .env 或其中任何秘密。
recover_pending_refinery_window_transaction || die "无法恢复上一次未完成的 refinery 窗口事务"

# Compose 解析的必填运行时配置必须先就绪；否则候选容器和回滚都会在生产者已停后
# 被拒绝，反而破坏发布的恢复保证。
ensure_compose_data_root
ensure_compose_model_gateway_token

# 锁必须已经取得，才允许读、备份、替换 NAS 的 .env。之后的任何失败都会由
# release_exit 复原这份完整旧配置。
prepare_refinery_window_change

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
  die "5 分钟没排空干净，已中止（生产者已恢复）"
fi

# ---- 5. 切标签 + 起容器 ----------------------------------------------------
say "[5/6] 切到 $SHA 并启动 $SERVICES"
if ! on_nas "set -eu
  cd $SRC
  # .env 里同时记住上一次的标签：回滚不需要人去翻历史。
  tmp=\$(mktemp '$SRC/.env.XXXXXX')
  grep -v '^KG_HUB_IMAGE_TAG' .env > \"\$tmp\" 2>/dev/null || true
  printf 'KG_HUB_IMAGE_TAG=%s\nKG_HUB_IMAGE_TAG_PREV=%s\n' '$SHA' '$PREV' >> \"\$tmp\"
  chmod 600 \"\$tmp\"; mv -f \"\$tmp\" .env
  $DK compose -p $PROJECT up -d --no-deps --no-build $SERVICES"; then
  rollback_to_previous_image "候选容器启动失败"
  die "发布失败，已回到 $PREV"
fi

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
  rollback_to_previous_image "健康检查未通过"
  die "发布失败，已回到 $PREV"
fi

producers_stopped=0   # 上一步的 up -d 已经把它们带起来了
discard_refinery_window_backup
say "✅ 发布完成：kg-hub-server:$SHA（上一个 $PREV 仍在盘上，可 --rollback）"
}

# 允许 shell 测试 source 本文件、替换 on_nas 后直接覆盖 .env 事务的失败分支；正常
# 执行时才运行完整发布流程。
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
