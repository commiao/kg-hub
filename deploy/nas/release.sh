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
COMPOSE_BASE="docker-compose.yml"
COMPOSE_GATEWAY_OVERRIDE="deploy/model-gateway-network.override.yml"
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

# The model-gateway client deliberately accepts only the local gateway origin
# and a kg-hub business key.  A legacy direct-provider value can keep /health
# green while every ingest fails during lazy Graphiti construction, so reject
# it before stopping either producer or building a candidate image.
#
# 判据取**生效值**而不是 .env 里那一行（2026-09-22，T-0084 发布时撞上）。
# 原写法要求这两个键在 .env 里恰好出现一次。但它们在 override 里自带默认值
# （`${ANTHROPIC_BASE_URL:-http://model-gateway:39000}`），所以「不在 .env 里」
# 是完全正常的状态 —— 而同一个仓库的 check_env_drift.py 正是这么建议的：
# 「与 git 默认相同的冗余覆盖，建议从 .env 删掉」。
#
# 09-21 17:05 有人照着做了，于是**这道闸从那一刻起让 kg-hub 完全发不了版**，
# 十七小时没人知道（服务照常，因为 compose 的默认值就是受控值）。
# 两个检查在同一个仓库里要求正好相反：一个说「删掉」，一个说「不在就不许发」。
#
# 根子是这道闸问错了对象：它查文件，而决定生效值的是 compose。改成问
# compose 自己（准则 27：要判断就用会真正解析它的那个解析器）。这样值住在
# .env 还是 override 都不影响判断，下次再搬一次也不会锁死发布。
ensure_compose_gateway_route() {
  if [ "${DRY_RUN:-0}" = 1 ]; then
    say "  [dry-run] 会校验 ANTHROPIC_BASE_URL 与 ANTHROPIC_MODEL 的受控网关路由"
    return 0
  fi

  on_nas "set -eu
    cd '$SRC'
    test -f .env
    # .env 里重复定义是歧义（compose 只会取一个），仍然拦掉；但「零次」是正常的。
    test \$(grep -c '^ANTHROPIC_BASE_URL=' .env || true) -le 1
    test \$(grep -c '^ANTHROPIC_MODEL=' .env || true) -le 1
    # 不落临时文件：compose config 的输出含 .env 里的机密，不该写到盘上。
    base=\$($DK compose -f $COMPOSE_BASE -f $COMPOSE_GATEWAY_OVERRIDE -p $PROJECT config \
      | sed -n 's/^ *ANTHROPIC_BASE_URL: *//p' | sort -u)
    model=\$($DK compose -f $COMPOSE_BASE -f $COMPOSE_GATEWAY_OVERRIDE -p $PROJECT config \
      | sed -n 's/^ *ANTHROPIC_MODEL: *//p' | sort -u)
    # sort -u：override 给三个服务各设一份。值一致时塌成一行；有哪个服务被单独
    # 改成别的值，这里就是两行，下面的等值判断当场挂 —— 比原写法更严。
    test \"\$base\" = 'http://model-gateway:39000'
    test \"\$model\" = 'kg_hub.entity_extract'
  " || die "模型网关路由缺失、重复或不是受控的本地业务路由"
}

# 上面那几条各自校验一个具体条件（数据目录、令牌、路由、私有网络）。它们合起来
# 仍然回答不了最朴素的那一问：**这套 compose 现在整体解析得通吗？**
#
# 为什么必须在停生产者之前问：一旦 refinery/ingester 停了，后面每一步 —— 起候选
# 容器、回滚 —— 都要 compose 能解析。解析不通的话，生产者已经停了，而任何一条
# 恢复路径都走不了。那正是「切了之后回不来」的形态。
#
# 不是假设：`docker-compose.yml` 里用的是 `${KG_HUB_IMAGE_TAG:?...}` 这类硬引用
# （铁律三：少了变量就当场失败，不许隐式默认）。**这个设计本身就意味着「.env 少
# 一行 = compose 整体不可用」**，而 .env 在发布过程中会被改写。上面那几条检查只覆盖
# 它们各自关心的那一个变量；新加一个变量、或者别的 actor 改了 override，它们一个
# 都不会响。
#
# `config -q` 只解析不执行、不碰任何容器，代价是一次远端调用。
ensure_compose_parses() {
  if [ "${DRY_RUN:-0}" = 1 ]; then
    say "  [dry-run] 会跑 compose config -q，确认整套文件在停生产者前解析得通"
    return 0
  fi
  # 同时传两个文件和同一个项目名 —— 必须和真正执行时的参数完全一致，
  # 否则校验的是另一套东西（准则 28：比较的两端要取自同一来源）。
  on_nas "cd '$SRC' && $DK compose -f $COMPOSE_BASE -f $COMPOSE_GATEWAY_OVERRIDE -p $PROJECT config -q" \
    || die "compose 解析失败（少变量或文件有误）；已在停生产者前中止，线上未受影响"
}

# server/ingester/watchdog 和 model-gateway 属于两个 Compose 项目。默认网络隔离
# 是正确的；只有这三个调用者通过已存在的 private network 相连，refinery 不接入。
# 漏掉 override 会在容器重建时静默断开 DNS，所有抽取都变成 deferred。必须在
# 停生产者前验证 override、网络与 gateway 的实际成员关系。
ensure_model_gateway_private_network() {
  if [ "${DRY_RUN:-0}" = 1 ]; then
    say "  [dry-run] 会校验 model-gateway private network 与现有网关成员关系"
    return 0
  fi

  on_nas "set -eu
    cd '$SRC'
    test -f '$COMPOSE_GATEWAY_OVERRIDE'
    count=\$(grep -c '^MODEL_GATEWAY_PRIVATE_NETWORK=' .env || true)
    case \"\$count\" in
      0) network='model-gateway-private' ;;
      1) network=\$(sed -n 's/^MODEL_GATEWAY_PRIVATE_NETWORK=//p' .env) ;;
      *) exit 1 ;;
    esac
    case \"\$network\" in ''|*[!A-Za-z0-9_.-]*) exit 1 ;; esac
    $DK network inspect \"\$network\" >/dev/null
    $DK inspect -f '{{range \$name, \$net := .NetworkSettings.Networks}}{{println \$name}}{{end}}' model-gateway | grep -qx \"\$network\"
  " || die "model-gateway private network 不存在、网关未接入或配置不合法"
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
  # 没有回滚目标就别假装回滚。PREV 为空只有一种来源：首次发布，.env 里本来就
  # 没有 KG_HUB_IMAGE_TAG（读不到的那种在上面已经 die 掉了）。
  # 这里若照常往下走，会把 .env 写成 `KG_HUB_IMAGE_TAG=` 然后 compose up ——
  # 等于把线上切到一个空标签，比不回滚坏得多。
  if [ -z "${PREV:-}" ]; then
    die "$reason —— 但没有可回滚的上一版（首次发布）；新容器保持现状，需要人工介入"
  fi
  say "$reason —— 自动回到 ${PREV}"
  # 这是刻意排在 compose 前面的：窗口变更和镜像切换是同一事务。
  restore_refinery_window_env || die "无法在镜像回滚前恢复旧 .env；停止自动回滚"
  on_nas "set -eu
    cd $SRC
    tmp=\$(mktemp '$SRC/.env.XXXXXX')
    grep -v '^KG_HUB_IMAGE_TAG=' .env > \"\$tmp\" 2>/dev/null || true
    printf 'KG_HUB_IMAGE_TAG=%s\\n' '$PREV' >> \"\$tmp\"
    chmod 600 \"\$tmp\"; mv -f \"\$tmp\" .env
    $DK compose -f $COMPOSE_BASE -f $COMPOSE_GATEWAY_OVERRIDE -p $PROJECT up -d --no-deps --no-build $SERVICES" \
    || die "回滚也失败了；线上需要人工介入（旧镜像 kg-hub-server:$PREV 仍在盘上）"
}

# 让 kg-hub 在排空期间拒收**新的**写入，使「排空不掉就别发」这条闸的终点可达。
#
# 停生产者只停得住自己的 refinery / ingester。`/api/ingest` 还有别的写入方：
# NAS 上的 task-hub 容器会把 done 任务结晶入图，任何用 kg_hub MCP 的会话也能直写。
# 2026-09-21 实测：12:16 那次发布等了约 1260s 才归零，期间一直有新条目进来。
# 预算再大也没用 —— 只要到达率不为零，计数就可能一直不为零（T-0117）。
#
# 拒收是安全的，这一点是读了调用方的重试路径才敢做的，不是假设：task-hub 的
# reconciler 只在 POST 返回 2xx 之后才落 reconciler_marks，失败就不落、下一轮
# （300s）重来、按任务 id 幂等。所以 503 不丢数据，只是晚几分钟入图。
#
# 令牌只在 NAS 上取用：不回传到本机 stdout、日志或命令插值（与本脚本处理
# KG_HUB_MODEL_GATEWAY_TOKEN 的做法一致）。
# 把排空窗口写成 NAS 上的持久证据。
#
# 为什么必须持久：排空发生在**旧容器**上，而发布的最后一步就是换掉它 ——
# 2026-09-21 实测两次，想复盘 12:16 那个窗口时日志已随容器重建消失，
# 事后没有第二次机会（T-0117 量不到外部到达率，就是卡在这里）。
#
# 落在点目录 `.release-history/` 下，不是根上的点文件：漂移检测对根下**点目录**
# 整体豁免，而根上的点**文件**会被报成「只在 NAS 上，git 未跟踪」——
# 那是这个检查里最响的一档，用它报一个自己刚造的文件就是在花可信度。
# prune 同样碰不到点目录（它的清单来自同一个豁免规则）。
DRAIN_LOG="$SRC/.release-history/drain.log"
drain_note() {
  [ "${DRY_RUN:-0}" = 1 ] && { say "  [dry-run] 会记一行排空证据：$*"; return 0; }
  on_nas "mkdir -p $(printf %q "$SRC/.release-history") && printf '%s %s\n' \
    \"\$(date -Iseconds)\" $(printf %q "$*") >> $(printf %q "$DRAIN_LOG")" \
    >/dev/null 2>&1 || say "  ⚠ 排空证据没记下（不影响发布，但这次窗口将无法复盘）"
}

set_drain() {
  local seconds="$1"
  [ "${DRY_RUN:-0}" = 1 ] && { say "  [dry-run] 会把 kg-hub 排空态设为 ${seconds}s"; return 0; }
  on_nas "set -eu
    cd '$SRC'
    tok=\$(sed -n 's/^KG_HUB_API_TOKEN=//p' .env | head -1)
    test -n \"\$tok\"
    curl -fsS -m 10 -X POST \
      -H \"Authorization: Bearer \$tok\" -H 'Content-Type: application/json' \
      -d '{\"seconds\": ${seconds}}' http://127.0.0.1:17171/api/drain >/dev/null
  "
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
  # 解除排空态。它本身带截止时间兜底，但那是**兜底**不是正常路径：
  # 中止之后让写入方白等几分钟没有任何好处。
  set_drain 0 >/dev/null 2>&1 || true
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
  say "发布 commit ${SHA}（$(git log -1 --format=%s "$SHA")）"
fi

acquire_lock

# 崩溃在完成窗口变更之后、清理备份之前时，下一次拿到锁的连接先收束那一笔旧事务。
# 这一步只读取固定事务记录，不打印 .env 或其中任何秘密。
recover_pending_refinery_window_transaction || die "无法恢复上一次未完成的 refinery 窗口事务"

# Compose 解析的必填运行时配置必须先就绪；否则候选容器和回滚都会在生产者已停后
# 被拒绝，反而破坏发布的恢复保证。
ensure_compose_data_root
ensure_compose_model_gateway_token
ensure_compose_gateway_route
ensure_model_gateway_private_network
ensure_compose_parses

# 锁必须已经取得，才允许读、备份、替换 NAS 的 .env。之后的任何失败都会由
# release_exit 复原这份完整旧配置。
prepare_refinery_window_change

# ---- 2. 记下当前标签，作为回滚点 ------------------------------------------
# 读「线上当前是哪个标签」。这个值是**自动回滚的目标**，所以它必须要么是真的，
# 要么就承认读不到 —— 不能填一个看起来合理的默认值。
#
# 原来是 `|| true` 吃掉 ssh 失败、再 `${PREV:-latest}` 补上，于是把两件完全不同的
# 事压成了一件：
#   .env 里没有这一行  → 首次发布，本来就没有「上一个」，回滚无从谈起
#   这次没读到         → ssh 挂了 / 文件读不了，**线上其实有一个上一版，只是我们不知道**
# 后者填成 `latest` 的后果是：真要自动回滚时，会把线上切到一个可变标签指向的
# 镜像 —— 而 latest 恰恰是本脚本存在的理由（T-0046：可变标签一 build 就把旧镜像
# 覆盖成悬空层，于是回滚时压根不存在「旧版本」这个东西）。
set +e
PREV=$(on_nas "grep '^KG_HUB_IMAGE_TAG=' $SRC/.env 2>/dev/null | cut -d= -f2-")
prev_rc=$?
set -e
PREV=$(printf '%s' "$PREV" | tr -d '[:space:]')
if [ "$prev_rc" != 0 ] && [ "${DRY_RUN:-0}" != 1 ]; then
  die "读不到线上当前标签（ssh 退出 ${prev_rc}）；不发 —— 没有回滚目标的发布不叫可回滚"
fi
if [ -z "$PREV" ]; then
  # 读到了，确实没有这一行：首次发布。说清楚，别假装有个「上一个」。
  say "当前线上标签：（.env 里没有 KG_HUB_IMAGE_TAG —— 首次发布，本次没有回滚目标）"
else
  say "当前线上标签：$PREV"
fi

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
# PREV 为空时这一步无事可做，而且**不能硬做**：这个分支恰恰只在 .env 缺这一行时
# 触发，也正是 PREV 为空的那种情形（首次发布）。写一个空值等于没关窗口 ——
# `${KG_HUB_IMAGE_TAG:?}` 把空值也当未设。原来这里写的是 `latest`，既关不上窗口
# （latest 指向的镜像是悬空的旧层），又把「首次发布」伪装成「有个上一版」。
if [ -z "${PREV:-}" ]; then
  say "      首次发布：.env 里本来就没有当前标签，这个窗口关不掉 ——"
  say "      在第 4 步写入新标签之前，任何人跑 docker compose 都会因变量缺失而失败（这是对的）"
else
  on_nas "set -eu
    cd $SRC
    grep -q '^KG_HUB_IMAGE_TAG=' .env 2>/dev/null || {
      tmp=\$(mktemp '$SRC/.env.XXXXXX')
      cat .env > \"\$tmp\" 2>/dev/null || true
      printf 'KG_HUB_IMAGE_TAG=%s\n' '$PREV' >> \"\$tmp\"
      chmod 600 \"\$tmp\"; mv -f \"\$tmp\" .env
      echo '      .env 已补上当前标签 $PREV'
    }"
fi

# ---- 4. 新卷 + 构建不可变镜像 ---------------------------------------------
on_nas "mkdir -p '$DATA/breakers' && chmod 700 '$DATA/breakers'"

if [ "$mode" = release ]; then
  say "[3/6] 构建 kg-hub-server:${SHA}（不动 latest）"
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
# 回来接着跑，一条不丢。在飞的条数由 INGEST_CONCURRENCY 封顶（现为 2）。
# **别再按「每条约 3 分钟」估**：2026-09-20 实测最长一次成功入图 1091.8s，而光是
# 等写锁最坏就要 1155s。预算改为从 utils/ingest_budget.py 派生（见下）。
say "[4/6] 排空：停生产者，等在飞的抽取归零（等的是在飞，不是积压）"
producers_stopped=0
# 生产者一旦停下，就必须保证它们能起回来：发布在这之后任何一步失败而没人管，
# 整条采集就静悄悄停了，比发布失败本身严重得多。
restore_producers() {
  [ "$producers_stopped" = 1 ] || return 0
  say "  恢复生产者 refinery / ingester"
  on_nas "cd $SRC && $DK compose -f $COMPOSE_BASE -f $COMPOSE_GATEWAY_OVERRIDE -p $PROJECT start refinery ingester" \
    || say "  ⚠ 生产者没起回来，需要人工：docker compose -f $COMPOSE_BASE -f $COMPOSE_GATEWAY_OVERRIDE -p $PROJECT start refinery ingester"
  producers_stopped=0
}
if on_nas "cd $SRC && $DK compose -f $COMPOSE_BASE -f $COMPOSE_GATEWAY_OVERRIDE -p $PROJECT stop -t 30 refinery ingester"; then
  producers_stopped=1
else
  say "  （生产者没停成，继续——最坏是多等一会儿）"
fi
# 排空预算与 refinery 的轮询上限、服务端的写锁上限同源（utils/ingest_budget.py）。
# 原先是写死的 60 轮 × 5s = 300s，依据是上面那句「每条约 3 分钟」—— 2026-09-20 实测
# 一次**成功**入图 elapsed=1091.8s，那个假设不成立，于是窗口期内发布几乎必然先撞
# 一次中止再重来。预算是**上限不是固定等待**：归零就立刻继续，正常情况代价为零。
DRAIN_BUDGET_S=$(cd "$REPO" && python3 -m utils.ingest_budget) || DRAIN_BUDGET_S=""
case "$DRAIN_BUDGET_S" in
  ''|*[!0-9]*) die "排空预算算不出来（utils/ingest_budget.py）——不猜一个数就发" ;;
esac
say "  排空预算 ${DRAIN_BUDGET_S}s（服务端一次 ingest 的最坏用时；归零即继续）"
# 先让服务端拒收新写入，再开始等 —— 顺序反了的话，等的过程中还会有新条目进来，
# 终点就可能永远到不了（T-0117）。
if set_drain "$DRAIN_BUDGET_S"; then
  say "  已让 kg-hub 拒收新写入（外部写入方会在各自的下一轮重试，不丢）"
  drain_note "enter sha=${SHA} budget=${DRAIN_BUDGET_S}s"
else
  say "  ⚠ 没能让 kg-hub 进入排空态；继续等，但外部写入方仍在推，可能等不到归零"
fi
drained=0
waited=0
while [ "$waited" -lt "$DRAIN_BUDGET_S" ]; do
  [ "${DRY_RUN:-0}" = 1 ] && { drained=1; break; }
  # 三种情况必须分清，**不能都看成「没拿到值」**：
  #
  #   够不到线上        ssh 挂了 / curl 超时 / 非 2xx → 什么都不知道，不许当成排空
  #   拿到了但没这个字段 线上还是旧版本            → 盲等一个路由超时 + 余量
  #   拿到了值           这才是真的在回答问题
  #
  # 原来是把这三种压成一个「$n 为空」，然后**打印一句关于线上版本的诊断**、置
  # drained=1 继续切换 —— 于是一次网络抖动就能绕过下面那条「排空不掉就别发」的闸，
  # 而操作者被告知的是一件假事。Mac↔NAS 这条链路有据可查地会抖（T-0077/T-0083）。
  #
  # 讽刺的是上面那条注释本来就写着「字段缺失和值为 0 必须能分清」—— 缺字段和 0
  # 确实分清了，唯独把「压根没够到」并进了「缺字段」。
  #
  # 判据一律写成「只有明确的成功才配走成功路径」：够不到就继续轮询，预算耗尽自然
  # 落到那条闸上中止，而不是替它做一个乐观的决定。
  # DRY_RUN 在循环第一行就 break 了，这里不用再管它 —— 写一个到不了的分支，
  # 比不写更坏：它看起来像在处理一种情况。
  body=$(ssh "${SSH_OPTS[@]}" "$NAS" "curl -fsS -m 5 '$HEALTH'" 2>/dev/null); probe_rc=$?
  if [ "$probe_rc" != 0 ]; then
    say "  够不到线上 /health（ssh/curl 退出 ${probe_rc}）；**不当作已排空**，继续等（已等 ${waited}s / ${DRAIN_BUDGET_S}s）"
    sleep 5
    waited=$((waited + 5))
    continue
  fi
  # 用 python 解 JSON 而不是 sed 抠字符串：字段缺失和值为 0 必须能分清。
  n=$(printf '%s' "$body" | python3 -c '
import json,sys
try: print(json.load(sys.stdin)["active_extractions"])
except Exception: pass' 2>/dev/null || true)
  if [ -z "$n" ]; then
    # 够到了，但没有这个字段 —— 线上确实还是旧版本。只能盲等一个路由超时（150s）+ 余量。
    say "  线上版本还没有 active_extractions，改为盲等 180s（下次发布起就精确了）"
    sleep 180; drained=1; break
  fi
  if [ "$n" = 0 ]; then
    refused=$(printf '%s' "$body" | python3 -c '
import json,sys
try: print(json.load(sys.stdin).get("drain_refused", "?"))
except Exception: print("?")' 2>/dev/null || echo "?")
    say "  在飞抽取已归零（等了 ${waited}s，期间挡下 ${refused} 次外部写入）"
    drain_note "drained waited=${waited}s refused=${refused}"
    drained=1; break
  fi
  say "  还有 $n 条在飞，等…（已等 ${waited}s / ${DRAIN_BUDGET_S}s）"
  sleep 5
  waited=$((waited + 5))
done
if [ "$drained" != 1 ]; then
  # 排空不掉就**别发**。硬换会掐断在飞的流式抽取，在网关留下永不过期的记录 ——
  # 那正是这一步要避免的东西，为了赶一次发布去制造它不划算。此刻源码已同步、
  # 镜像已构建，但容器还没换，中止是干净的：过会儿重跑即可。
  restore_producers
  drain_note "abort waited=${waited}s budget=${DRAIN_BUDGET_S}s"
  die "${DRAIN_BUDGET_S}s 没排空干净，已中止（生产者已恢复）"
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
  $DK compose -f $COMPOSE_BASE -f $COMPOSE_GATEWAY_OVERRIDE -p $PROJECT up -d --no-deps --no-build $SERVICES"; then
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
    say "  ⚠ 容器跑的不是 kg-hub-server:${SHA}（want=$want got=${got}）"
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
# 顺序要紧：prune 会改变「NAS 上有什么」，而漂移判决正是对这件事的结论。
# 刷在前面的话，刚刷的那条判决在几秒后就被自己这一步弄过期了（准则 10）。
prune_orphans
refresh_drift_verdict
say "✅ 发布完成：kg-hub-server:${SHA}（上一个 $PREV 仍在盘上，可 --rollback）"
}

# 发完顺手刷新漂移巡检的判决。
#
# 那个判决是缓存：任一侧变了它就过期，而巡检是**每天**跑一次。一次发布恰好改的就是
# 「线上」那一侧 —— 于是发完之后，SessionStart 会继续拿发布前那条结论当现状显示，
# 最长可达一天。2026-09-18 在网关那边实测踩到过：部署成功后巡检仍在报「deploy/ 有
# 8 个文件漂了」，而那 8 个正是刚被这次发布对齐掉的。
#
# 年龄阈值救不了这一种：文件是一天之内写的，看起来就是新鲜的。**能改变判决的动作
# 自己负责刷新它**，比把巡检调密更准，也不会多打一次 NAS。
#
# 刷新失败不该让发布失败 —— 发布本身已经成功，巡检下一轮自会跟上。
refresh_drift_verdict() {
  [ "${DRY_RUN:-0}" != 1 ] || return 0
  checker="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check_source_drift.py"
  [ -f "$checker" ] && command -v python3 >/dev/null 2>&1 || return 0
  set +e
  python3 "$checker" --status-file "$HOME/.cache/kg-hub/source-drift.status" \
    >/dev/null 2>&1
  local rc=$?
  set -e
  # 三档，不能并成两档。原来写的是「非零一律算刷新成功，因为非零也可能只是确实
  # 有漂移」—— 那句把「有漂移」和「检测压根没跑成」混成了一句，而后者状态文件
  # **根本没写**，此时说「已刷新」是假话。准则 10 的要害正是「能改变判决的动作
  # 要对判决负责」。
  case "$rc" in
    0) say "  巡检判决已刷新：一致" ;;
    1) say "  巡检判决已刷新：有待处理项，见 $HOME/.cache/kg-hub/source-drift.status" ;;
    *) say "  ⚠ 巡检判决**没有**刷新（检测退出 ${rc}）；状态文件可能还是发布前那条"
       say "    以 $HOME/.cache/kg-hub/source-drift.status 里的时间戳为准" ;;
  esac
}

# 发完清掉「git 已经没有、而 NAS 上还在」的文件。
#
# 本脚本是逐文件 mv -f 覆盖、**从不删除**，所以从 git 删掉的文件会一直留在 NAS 上
# 被执行 —— 于是「线上等于某个 commit」只在「有什么」这一半成立。
# 2026-09-20 抓到的 ingesters/claude_mem_obs.py 就是这么来的：git 早已删除
# （972cfae「退役：没人 import、没有作业跑它」），NAS 上却活着，靠漂移检测报红
# 六小时后人工清掉。
#
# 判据是 `--list-prunable`：**NAS 上那份内容在 git 历史里找得到一模一样的**。
# 不是 `--list-extra`（「git 里现在没有」的补集）—— 那里面装着生产独有却正在跑的
# 文件，实测过的例子是 credvault 的 deploy/hot_config_reconciliation.py：360 行、
# NAS 上在跑、git 里连文件名都没有。删它就是把生产打掉（T-0084 立的约束）。
#
# 准则 5「备份范围 ⊇ 覆盖范围」在这里是**靠判据本身**满足的：kg-hub 的发布没有
# 整树备份（只有 .env 的事务备份），而被删的每一个字节都能用
# `git show <commit>:<path>` 原样取回，且这件事在删之前被机器验证过，不靠人核对。
#
# 曾被 git 跟踪、但 NAS 上内容与历史里任何一版都不同的（多半有人直接改过生产），
# 检测走 stderr 单独报出来，**不删**。
prune_orphans() {
  [ "${DRY_RUN:-0}" != 1 ] || return 0
  [ "${NO_PRUNE:-0}" != 1 ] || { say "  跳过清理（NO_PRUNE=1）"; return 0; }
  checker="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check_source_drift.py"
  [ -f "$checker" ] && command -v python3 >/dev/null 2>&1 || {
    say "  ⚠ 找不到检测脚本或 python3，本次不清理任何文件"; return 0; }

  set +e
  extra=$(python3 "$checker" --list-prunable --ref "$SHA" 2>/tmp/kg-prune-skipped.$$)
  local rc=$?
  set -e
  skipped=$(cat /tmp/kg-prune-skipped.$$ 2>/dev/null || true); rm -f /tmp/kg-prune-skipped.$$

  # 「没有多余文件」和「这次没查成」必须分开报。判据写成「等于某个我预料到的
  # 失败码」会漏掉预料之外的那些 —— 未捕获异常给的是 rc=1，正好顺着 happy path
  # 走成「清理干净」。所以这里写「不是明确的成功就按失败处理」。
  if [ "$rc" != "0" ]; then
    say "  ⚠ 拿不到可删清单（检测退出 ${rc}），本次不删任何文件"
    say "    宁可留着让漂移检测继续报，也不在没查清时删生产上的文件"
    return 0
  fi

  [ -z "$skipped" ] || printf '%s\n' "$skipped" | sed 's/^/        /'

  if [ -z "$extra" ]; then
    say "  NAS 无可清理的残留（git 已删但仍在线上的：0 个）"
    return 0
  fi

  pruned=0
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    # 清单是外部命令的输出：绝对路径和 .. 一律拒绝，只在 $SRC 里删。
    case "$path" in
      /*|*..*) say "        拒绝删除可疑路径：$path"; continue ;;
    esac
    say "        删除：$path"
    on_nas "rm -f -- '$SRC/$path'" >/dev/null
    pruned=$((pruned + 1))
  done <<EOF
$extra
EOF
  # 删空的目录顺手清掉，但绝不碰 $SRC 本身，**也绝不碰任何点目录**。
  #
  # 点目录必须排除，有两个独立理由：
  #
  # 一、`$SRC/.release.lock` 就是一个点目录，而这一步跑的时候**我们正握着它**。
  #    正常情况它含 owner 文件、非空、删不到；但 `mkdir $LOCK` 与写 owner 之间
  #    有一个窗口（取锁和抢占陈旧锁两条路径都有），写失败就留下一个空锁目录 ——
  #    然后这行把自己正握着的锁悄悄删掉，没有任何报错，并发闸当场失效。
  #    概率低，可后果正是这套东西要防的那一类，而排除它的成本是一个 `-name`。
  #
  # 二、漂移检测对根下点目录是**整体豁免**的（is_allowed_untracked：那里全是历次
  #    部署留下的备份与暂存）。既然它们从来不被报，就不该被这一步删 ——
  #    「删什么」不能超出「报什么」，超出的那部分没有任何东西看着。
  on_nas "find '$SRC' -mindepth 1 -name '.*' -prune -o \
          -type d -empty -exec rmdir {} + 2>/dev/null || true" >/dev/null
  say "  已清理 $pruned 个 git 已删除的残留文件（内容均可由 git 历史原样取回）"
}

# 允许 shell 测试 source 本文件、替换 on_nas 后直接覆盖 .env 事务的失败分支；正常
# 执行时才运行完整发布流程。
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
