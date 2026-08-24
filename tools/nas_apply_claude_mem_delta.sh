#!/bin/sh
# 在 NAS 上把一个「增量库」合并进 claude-mem.db 的副本，然后原子换上去。
# 由 Mac 侧 tools/sync_claude_mem_to_nas.sh 经 ssh 调用，增量库走 stdin(gzip)。
#
#   用法:  gzip -c delta.db | ssh nas "sh <本脚本> <期望的合并后 MAX(id)>"
#
# ── 为什么是「复制 + 合并 + 原子换」而不是原地 INSERT ──────────────────
# 消费者 kg-hub-refinery 用 `file:...?mode=ro&immutable=1` 打开这个库。
# immutable=1 等于告诉 SQLite「这文件不会变」——它因此**不加锁**。
# 原地 INSERT 会让它读到写了一半的页。所以必须保留原子 mv 的语义:
# 读者要么看到旧 inode、要么看到新 inode，不存在中间态。
#
# 复制发生在 NAS 本地磁盘(实测 70.7 MB/s，80MB 约 1.2 秒)，不过网络。
# 过网的只有增量(典型 ~25KB)。
#
# ── 为什么不能只保留增量、丢掉存量 ───────────────────────────────────
# refinery 有 fetch_rows_by_ids()，会按 id **随机读历史行**烧积压
# (2026-08-24 时 backlog_remaining=9383)。NAS 这份必须是完整存量。
set -e

D="/volume2/4T/kg-hub-data/claude-mem"
DB="$D/claude-mem.db"
DELTA="$D/.delta.db"
NEXT="$D/.next.db"
EXPECT="$1"

cleanup() { rm -f "$DELTA" "$DELTA-wal" "$DELTA-shm" "$NEXT" "$NEXT-wal" "$NEXT-shm"; }
trap cleanup EXIT INT TERM

[ -n "$EXPECT" ] || { echo "usage: $0 <expected_max_id>" >&2; exit 2; }
[ -f "$DB" ] || { echo "FAIL 目标库不存在: $DB" >&2; exit 3; }

# 1) 收增量。gunzip 遇到截断输入会返回非 0 —— 这是防「流被截断却当成功」的第一道闸。
gunzip -c > "$DELTA" || { echo "FAIL 增量解压失败(流被截断?)" >&2; exit 4; }

# 2) 增量自检
[ "$(sqlite3 -readonly "$DELTA" 'PRAGMA integrity_check;' 2>/dev/null | head -1)" = "ok" ] \
  || { echo "FAIL 增量库损坏" >&2; exit 5; }
NROW=$(sqlite3 -readonly "$DELTA" 'SELECT COUNT(*) FROM observations;' 2>/dev/null || echo 0)

# 3) 复制存量 → 合并 → 自检
cp -f "$DB" "$NEXT"
rm -f "$NEXT-wal" "$NEXT-shm"      # 陈旧 WAL 会被回放到新副本上，必须清掉
sqlite3 "$NEXT" "
ATTACH '$DELTA' AS d;
INSERT OR IGNORE INTO main.observations  SELECT * FROM d.observations;
INSERT OR REPLACE INTO main.sdk_sessions SELECT * FROM d.sdk_sessions;
" || { echo "FAIL 合并失败" >&2; exit 6; }

[ "$(sqlite3 -readonly "$NEXT" 'PRAGMA integrity_check;' 2>/dev/null | head -1)" = "ok" ] \
  || { echo "FAIL 合并后完整性不过" >&2; exit 7; }

GOT=$(sqlite3 -readonly "$NEXT" 'SELECT MAX(id) FROM observations;' 2>/dev/null)
[ "$GOT" = "$EXPECT" ] \
  || { echo "FAIL 合并后 MAX(id)=$GOT 期望=$EXPECT" >&2; exit 8; }

# 4) 原子换上。到这一步才动线上文件 —— 前面任何一步失败，线上库原封不动。
mv -f "$NEXT" "$DB"
rm -f "$DB-wal" "$DB-shm"
echo "OK merged=$NROW max=$GOT"
