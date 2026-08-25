#!/bin/sh
# 在 NAS 上把一个「增量库」合并进 claude-mem.db 的副本，然后原子换上去。
# 由 Mac 侧 tools/sync_claude_mem_to_nas.sh 经 ssh 调用。
#
# 两种取数方式：
#   同步盘（主）  sh 本脚本 <期望MAX(id)> <inbox文件名> <期望字节数> <期望sha256>
#   stdin（兜底）  gzip -c delta.db | ssh nas "sh 本脚本 <期望MAX(id)>"
#
# ── 为什么主路径走同步盘而不是 ssh 管道 ────────────────────────────────
# Mac↔NAS 的 tailscale 链路会退化到只能承载小包。2026-08-24 实测(公司 Wi-Fi,
# 50% 丢包)：ssh 小包 3/3 通，但 133KB 起的大块**全部 Timeout**，连 6KB 的
# 稳态增量都失败过。而同一时刻 Synology Drive 同步盘 4MB/15s、8KB/7s 正常。
# 所以这不是快慢问题，是**可用性**问题：tailscale 只保得住控制面。
#
# 同步盘是异步的、没有"传完了"的信号，所以这里必须自己等齐：
# 按**字节数 + sha256**双条件确认，绝不"看见文件就用"——传输中途文件是可见的
# 半截，那正是 2026-08-21 把 NAS 库写成 17301504 字节 malformed 的同款陷阱。
#
# ── 为什么是「复制 + 合并 + 原子换」而不是原地 INSERT ──────────────────
# 消费者 kg-hub-refinery 用 `file:...?mode=ro&immutable=1` 打开这个库。
# immutable=1 等于告诉 SQLite「这文件不会变」——它因此**不加锁**。
# 原地 INSERT 会让它读到写了一半的页。所以必须保留原子 mv 的语义：
# 读者要么看到旧 inode、要么看到新 inode，不存在中间态。
# 复制发生在 NAS 本地磁盘(实测 70.7 MB/s)，不过网络。
#
# ── 为什么不能只保留增量、丢掉存量 ───────────────────────────────────
# refinery 有 fetch_rows_by_ids()，会按 id **随机读历史行**烧积压
# (2026-08-24 时 backlog_remaining=9383)。NAS 这份必须是完整存量。
set -e

D="/volume2/4T/kg-hub-data/claude-mem"
DB="$D/claude-mem.db"
DELTA="$D/.delta.db"
NEXT="$D/.next.db"
INBOX="/volume1/public-sync/kg-hub-inbox"

EXPECT="$1"          # 落库后应有的 MAX(observations.id)
FNAME="$2"           # 同步盘里的文件名（空 = 走 stdin 兜底）
FBYTES="$3"          # 期望字节数
FSHA="$4"            # 期望 sha256
WAIT_MAX=180         # 等同步盘送达的上限（秒）

# merge  = 增量并进现有副本（稳态）
# replace= 收到的就是完整副本，整份换掉（兜底重建：现有副本可能正损坏着、
#          或是装不下增量的旧格式，往里 merge 没有意义）
MODE="${MODE:-merge}"

SRCFILE=""
cleanup() {
  rm -f "$DELTA" "$DELTA-wal" "$DELTA-shm" "$NEXT" "$NEXT-wal" "$NEXT-shm"
  [ -n "$SRCFILE" ] && rm -f "$SRCFILE"
  # 顺手清掉超过 1 小时的陈旧投递（失败run 留下的）
  find "$INBOX" -maxdepth 1 -type f -mmin +60 -delete 2>/dev/null
  return 0
}
trap cleanup EXIT INT TERM

[ -n "$EXPECT" ] || { echo "usage: $0 <expected_max_id> [file bytes sha256]" >&2; exit 2; }
# merge 需要现有副本当底;replace 是整份换掉,副本缺失/损坏都不影响。
[ "$MODE" = "replace" ] || [ -f "$DB" ] || { echo "FAIL 目标库不存在: $DB" >&2; exit 3; }

# ── 1) 取增量 ──────────────────────────────────────────────────────────
if [ -n "$FNAME" ]; then
  SRCFILE="$INBOX/$FNAME"
  # 等齐：字节数先到位只说明"可能"传完，sha256 才是确认。两者都对才动手。
  i=0
  while [ "$i" -lt "$WAIT_MAX" ]; do
    if [ -f "$SRCFILE" ]; then
      got=$(wc -c < "$SRCFILE" 2>/dev/null | tr -d ' ')
      if [ "$got" = "$FBYTES" ]; then
        sha=$(sha256sum "$SRCFILE" 2>/dev/null | cut -d' ' -f1)
        [ "$sha" = "$FSHA" ] && break
      fi
    fi
    i=$((i + 2)); sleep 2
  done
  [ "$i" -lt "$WAIT_MAX" ] || {
    echo "FAIL 同步盘 ${WAIT_MAX}s 未送达完整文件 ($FNAME, 期望 ${FBYTES}B)" >&2; exit 10; }
  gunzip -c "$SRCFILE" > "$DELTA" || { echo "FAIL 增量解压失败" >&2; exit 4; }
else
  # 兜底:直接从 stdin 收。gunzip 遇截断输入返回非 0,是防"流被截断却当成功"的闸。
  gunzip -c > "$DELTA" || { echo "FAIL 增量解压失败(流被截断?)" >&2; exit 4; }
fi

# ── 2) 增量自检 ────────────────────────────────────────────────────────
[ "$(sqlite3 -readonly "$DELTA" 'PRAGMA integrity_check;' 2>/dev/null | head -1)" = "ok" ] \
  || { echo "FAIL 增量库损坏" >&2; exit 5; }
NROW=$(sqlite3 -readonly "$DELTA" 'SELECT COUNT(*) FROM observations;' 2>/dev/null || echo 0)

# ── 3) 造出候选新库 → 自检 ─────────────────────────────────────────────
if [ "$MODE" = "replace" ]; then
  mv -f "$DELTA" "$NEXT"           # 收到的本身就是完整副本
else
  cp -f "$DB" "$NEXT"
  rm -f "$NEXT-wal" "$NEXT-shm"    # 陈旧 WAL 会被回放到新副本上，必须清掉
  sqlite3 "$NEXT" "
ATTACH '$DELTA' AS d;
INSERT OR IGNORE INTO main.observations  SELECT * FROM d.observations;
INSERT OR REPLACE INTO main.sdk_sessions SELECT * FROM d.sdk_sessions;
" || { echo "FAIL 合并失败" >&2; exit 6; }
fi
rm -f "$NEXT-wal" "$NEXT-shm"

[ "$(sqlite3 -readonly "$NEXT" 'PRAGMA integrity_check;' 2>/dev/null | head -1)" = "ok" ] \
  || { echo "FAIL 合并后完整性不过" >&2; exit 7; }

GOT=$(sqlite3 -readonly "$NEXT" 'SELECT MAX(id) FROM observations;' 2>/dev/null)
[ "$GOT" = "$EXPECT" ] \
  || { echo "FAIL 合并后 MAX(id)=$GOT 期望=$EXPECT" >&2; exit 8; }

# ── 4) 原子换上。到这一步才动线上文件 —— 前面任何一步失败，线上库原封不动。
mv -f "$NEXT" "$DB"
rm -f "$DB-wal" "$DB-shm"
echo "OK merged=$NROW max=$GOT"
