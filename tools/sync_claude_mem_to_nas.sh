#!/bin/sh
# 持续摄入(切流量后):把 Mac 的 live claude-mem.db 同步到 NAS。
# NAS 上的 ingester 循环会读它、摄入新 obs(LLM 在 NAS 跑,只小文件过 tailscale)。
# 仅在有新 obs 时传;Mac↔NAS 抖动时重试,失败则跳过(下个周期再来)。
#
# ── 2026-08-21 修 T-0028:WAL 静默失效 ────────────────────────────────────
# 原判据是 `shasum -a 256` 主库文件。但 claude-mem 是 journal_mode=wal,新写入
# 全落在 claude-mem.db-wal,主库 mtime/内容长时间不动 → 哈希不变 → 每轮都
# `unchanged, skip`。日志看着完全正常,实际同步只在 SQLite 自动 checkpoint
# (~4MB)把 WAL 刷回主库时才偶然触发一次,节奏被 checkpoint 周期绑架。
#
# 现在:
#   判据 = sqlite3 查 MAX(observations.id) —— 连接会读 WAL,是语义化 watermark
#   传输 = VACUUM INTO 生成一致热快照 —— 含 WAL 全部数据,且**不动源库**
#
# 为什么不用 `.backup`:它需要可写连接。而 `sqlite3 -readonly ... ".backup"`
# 会**静默产出损坏文件** —— 实测退出码 0、生成 75MB、看着正常,但查 MAX(id)
# 返回空。这正是本 bug 的同款陷阱:命令"看起来成功"。VACUUM INTO 在 readonly
# 连接下正常工作,且只写目标文件,不碰 claude-mem 的任何文件。
SRC="/Users/mac/.claude-mem/claude-mem.db"
NAS="commiao@100.123.208.32"
DST="/volume2/4T/kg-hub-data/claude-mem/claude-mem.db"
STATE="/Users/mac/.kg-hub/state"
STAMP="$STATE/claude-mem-synced.obsid"   # 语义已从 sha256 变成 obs id,故换名
mkdir -p "$STATE"
ts() { date '+%F %T'; }
[ -f "$SRC" ] || { echo "$(ts) no source db"; exit 0; }

# ── 并发锁 ─────────────────────────────────────────────────────────────
# launchd 的 StartInterval **不会**在上一轮还活着时另起一轮。所以一次卡死的
# 传输会让同步无限期冻结 —— 2026-08-21 就是这么停了 40 分钟:一个 cat|ssh
# 在 79MB 传到 21% 时僵住,STAT=S 挂了 23 分钟,期间 900s 的定时一次都没触发。
# 这里自己判定并杀掉卡死的上一轮,不把活性交给 launchd。
LOCK="$STATE/sync.lock"
if [ -f "$LOCK" ]; then
  lpid=$(cat "$LOCK" 2>/dev/null)
  if [ -n "$lpid" ] && kill -0 "$lpid" 2>/dev/null; then
    # 跑了多久 = 现在 - 锁文件 mtime。锁是开工那一刻写的,所以它的 mtime
    # 就是本轮起点。不用 `ps -o etimes=` —— 那是 GNU/procps 的关键字,
    # macOS 的 BSD ps 不认,会把整份关键字清单打印出来当成 $age,于是
    # `[ "$age" -gt 600 ]` 直接报错、永远落到 else 分支 ——
    # **卡死接管逻辑等于从没生效过**。(2026-08-21 实测踩到)
    lmt=$(stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK" 2>/dev/null)
    now=$(date +%s)
    if [ -n "$lmt" ]; then age=$((now - lmt)); else age=0; fi
    if [ "$age" -gt 600 ]; then
      echo "$(ts) 上一轮 pid=$lpid 已卡 ${age}s,杀掉重来"
      pkill -9 -P "$lpid" 2>/dev/null
      kill -9 "$lpid" 2>/dev/null
    else
      echo "$(ts) 上一轮 pid=$lpid 仍在跑 (${age}s),本轮跳过"
      exit 0
    fi
  fi
fi
echo $$ > "$LOCK"

# 被 SIGKILL 打断时 trap 不会跑,79MB 的快照会留在 /tmp。开工先扫。
find /tmp -maxdepth 1 -name 'cm-snap.*' -type f -mmin +30 -delete 2>/dev/null

# ── 判据 ───────────────────────────────────────────────────────────────
cur=$(sqlite3 -readonly "$SRC" 'SELECT MAX(id) FROM observations;' 2>/dev/null)
case "$cur" in
  ''|*[!0-9]*)
    # 取不到 watermark 是**真故障**(库被锁/损坏/schema 变了),绝不能当成
    # "没变化"静静跳过 —— 那就是又造一个静默失效。
    echo "$(ts) ERROR 读不到 watermark(库被锁或 schema 变了?),本轮不同步"
    exit 1 ;;
esac
prev=$(cat "$STAMP" 2>/dev/null)
[ "$cur" = "$prev" ] && { echo "$(ts) 无新 obs (MAX id=$cur), skip"; exit 0; }

# ── 快照 ───────────────────────────────────────────────────────────────
TMP=$(mktemp /tmp/cm-snap.XXXXXX) && rm -f "$TMP" && TMP="$TMP.db"
trap 'rm -f "$TMP" "$TMP-wal" "$TMP-shm" "$LOCK"' EXIT INT TERM
if ! sqlite3 -readonly "$SRC" "VACUUM INTO '$TMP'" 2>/dev/null; then
  echo "$(ts) ERROR VACUUM INTO 失败,本轮不同步"; exit 1
fi
# 传之前先自检。不验就传等于把"命令退出 0"当成"数据是对的" —— 上面注释里
# 那个 .backup 陷阱就是这么骗过人的。
snap_max=$(sqlite3 -readonly "$TMP" 'SELECT MAX(id) FROM observations;' 2>/dev/null)
integ=$(sqlite3 -readonly "$TMP" 'PRAGMA integrity_check;' 2>/dev/null | head -1)
if [ "$integ" != "ok" ] || [ "$snap_max" != "$cur" ]; then
  echo "$(ts) ERROR 快照自检不过 (integrity=$integ snap_max=$snap_max 期望=$cur),不传"
  exit 1
fi

# ── 传输 ───────────────────────────────────────────────────────────────
# 用 cat|ssh 管道而非 scp:群晖 sshd 未启用 SFTP 子系统,新版 scp 默认走
# SFTP 会报 "subsystem request failed on channel 0"。管道经临时文件原子 mv,
# 避免 ingester 读到半截库。(与 openclaw 同步同款 ssh 备份同款思路)
#
# 换完主库要顺手删掉 NAS 上的 -wal/-shm:VACUUM INTO 的产物是自包含的(已
# checkpoint),所以那边任何 -wal 都必然是**上一个版本主库**留下的陈旧文件。
# 留着的话 SQLite 会拿旧 WAL 去回放新主库 —— 轻则读到错数据,重则判定损坏。
SIZE=$(wc -c < "$TMP" | tr -d ' ')

# 远端在 mv 之前必须自己验一遍:字节数 + integrity_check + watermark 三样都对。
#
# 为什么非得在**远端**验:原来的 `cat > tmp && mv` 挡不住流被截断。ssh 连接
# 一断,远端 cat 收到的是干净的 EOF,于是 **exit 0**,`&&` 成立,mv 把半截库
# 装上去。2026-08-21 实测踩到:79MB 传了 17MB 链路僵住,最后 NAS 上的
# claude-mem.db 变成 17301504 字节、integrity 报 "database disk image is
# malformed",而日志和退出码都看不出任何异常。
# 原子 mv 防的是"读到写一半的文件",防不住"写完的是个短文件"。
REMOTE="gunzip -c > '$DST.tmp' \
 && [ \"\$(wc -c < '$DST.tmp' | tr -d ' ')\" = '$SIZE' ] \
 && [ \"\$(sqlite3 -readonly '$DST.tmp' 'PRAGMA integrity_check;' 2>/dev/null | head -1)\" = 'ok' ] \
 && [ \"\$(sqlite3 -readonly '$DST.tmp' 'SELECT MAX(id) FROM observations;' 2>/dev/null)\" = '$cur' ] \
 && mv -f '$DST.tmp' '$DST' && rm -f '$DST-wal' '$DST-shm' \
 || { rm -f '$DST.tmp'; exit 9; }"

for i in 1 2 3; do
  # ServerAliveInterval/CountMax:ConnectTimeout 只管建连,管不了**传输中途**
  # 卡住。Mac↔NAS 走 tailscale 本来就会抖(见 kg-hub-mac-nas-tailscale-flaky),
  # 没有 keepalive 的 ssh 会无限期挂着。15s×4 → 约 60s 判死。
  # 压着传:实测链路只有 ~327 KB/s(direct,67ms,但吞吐就这样),79MB 裸传要 ~246s,
  # 占掉 900s 周期的 1/4,中途一抖就僵。gzip 后 79MB→26.9MB(2.9x),降到 ~85s,
  # 卡死暴露面小得多。顺带:gunzip 遇到截断输入会返回非 0,多一层防截断。
  if gzip -c "$TMP" | ssh -o BatchMode=yes -o ConnectTimeout=10 \
       -o ServerAliveInterval=15 -o ServerAliveCountMax=4 "$NAS" "$REMOTE" \
       >/dev/null 2>&1; then
    echo "$cur" > "$STAMP"
    echo "$(ts) synced (obs MAX id=$cur, 上轮=${prev:-无})"
    exit 0
  fi
  sleep 10
done
echo "$(ts) sync failed (NAS 不可达或远端校验未过), retry next interval"
exit 0
