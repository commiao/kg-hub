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
trap 'rm -f "$TMP" "$TMP-wal" "$TMP-shm"' EXIT INT TERM
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
for i in 1 2 3; do
  if cat "$TMP" | ssh -o BatchMode=yes -o ConnectTimeout=10 "$NAS" \
       "cat > '$DST.tmp' && mv -f '$DST.tmp' '$DST' && rm -f '$DST-wal' '$DST-shm'" \
       >/dev/null 2>&1; then
    echo "$cur" > "$STAMP"
    echo "$(ts) synced (obs MAX id=$cur, 上轮=${prev:-无})"
    exit 0
  fi
  sleep 10
done
echo "$(ts) sync failed (NAS unreachable via Mac), retry next interval"
exit 0
