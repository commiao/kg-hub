#!/bin/sh
# 持续摄入(切流量后):把 Mac 的 live claude-mem.db 同步到 NAS。
# NAS 上的 ingester 循环会读它、摄入新 obs(LLM 在 NAS 跑,只小文件过 tailscale)。
# 仅在 db 变化时传;Mac↔NAS 抖动时重试,失败则跳过(下个周期再来)。
SRC="/Users/mac/.claude-mem/claude-mem.db"
NAS="commiao@100.123.208.32"
DST="/volume1/docker/kg-hub-data/claude-mem/claude-mem.db"
STATE="/Users/mac/.kg-hub/state"
STAMP="$STATE/claude-mem-synced.sha"
mkdir -p "$STATE"
[ -f "$SRC" ] || { echo "$(date '+%F %T') no source db"; exit 0; }
cur=$(shasum -a 256 "$SRC" 2>/dev/null | awk '{print $1}')
prev=$(cat "$STAMP" 2>/dev/null)
[ "$cur" = "$prev" ] && { echo "$(date '+%F %T') unchanged, skip"; exit 0; }
# 用 cat|ssh 管道而非 scp:群晖 sshd 未启用 SFTP 子系统,新版 scp 默认走
# SFTP 会报 "subsystem request failed on channel 0"。管道经临时文件原子 mv,
# 避免 ingester 读到半截库。(与 openclaw 同步同款 ssh 管道思路)
for i in 1 2 3; do
  if cat "$SRC" | ssh -o BatchMode=yes -o ConnectTimeout=10 "$NAS" \
       "cat > '$DST.tmp' && mv -f '$DST.tmp' '$DST'" >/dev/null 2>&1; then
    echo "$cur" > "$STAMP"; echo "$(date '+%F %T') synced ($cur)"; exit 0
  fi
  sleep 10
done
echo "$(date '+%F %T') sync failed (NAS unreachable via Mac), retry next interval"
exit 0
