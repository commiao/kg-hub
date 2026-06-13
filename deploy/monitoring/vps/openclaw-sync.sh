#!/bin/sh
# OpenClaw 胶囊持续同步:VPS 本地 clawd -> NAS openclaw-src。
# NAS 上的 ingester 循环会发现并摄入新胶囊(capsule-/CAPSULE- 命名 ≥1500B,水位线去重)。
# VPS↔NAS 走 tailscale(稳),不经 Mac。仅同步 5 个胶囊根目录,体量小。
SRC="/home/admin/clawd"
NAS="commiao@100.123.208.32"
DST="/volume1/docker/kg-hub-data/openclaw-src"
ROOTS="notes memory plans reports capsules"
[ -d "$SRC" ] || { echo "$(date '+%F %T') no clawd src"; exit 0; }
# 只打包胶囊文件(保留相对路径),tar 经 ssh 注入 NAS,合并进 openclaw-src(不删旧)
exist=""
for r in $ROOTS; do [ -d "$SRC/$r" ] && exist="$exist $r"; done
[ -z "$exist" ] && { echo "$(date '+%F %T') no capsule roots"; exit 0; }
if tar czf - -C "$SRC" $exist 2>/dev/null | \
   ssh -o BatchMode=yes -o ConnectTimeout=15 "$NAS" "mkdir -p $DST && tar xzf - -C $DST 2>/dev/null && echo ok" >/dev/null 2>&1; then
  echo "$(date '+%F %T') openclaw synced to NAS"
else
  echo "$(date '+%F %T') sync failed (NAS unreachable), retry next cron"
fi
exit 0
