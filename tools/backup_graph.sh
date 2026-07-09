#!/bin/sh
# kg-hub 图谱全量备份 —— G3 apply 前的兜底恢复点。**在 NAS 上跑**：sh tools/backup_graph.sh
#
# 为什么快照整个 data 目录：FalkorDB 开了 appendonly=yes（有 appendonlydir），重启时加载
# AOF 而非 dump.rdb —— 只备份 dump.rdb 无法恢复。故快照 dump.rdb + appendonlydir 全套。
#
# ⚠️ 主回滚不是它：G3 只给 27 个节点打 archived=true，**首选回滚 = `curate_ops_noise --restore <manifest>`**
#    （REMOVE archived，零停机、精确、无需碰 RDB）。本脚本是灾难兜底（误操作/图状态错乱时）。
# 恢复（兜底，有停机）：停 falkordb → 用备份目录覆盖 data/dump.rdb + data/appendonlydir → 起 falkordb。
set -e
DOCKER=/var/packages/ContainerManager/target/usr/bin/docker
TS=$(date +%Y%m%d-%H%M%S)
cd "$(dirname "$0")/.."
echo "[backup] SAVE + 快照 dump.rdb+appendonlydir → backup-pre-G3-$TS"
sudo -n "$DOCKER" compose -p kg-hub exec -T -e TS="$TS" falkordb sh -c '
  set -e
  D=/var/lib/falkordb/data; B="$D/backup-pre-G3-$TS"
  redis-cli -a "$FALKORDB_PASSWORD" --no-auth-warning SAVE >/dev/null
  mkdir -p "$B"
  cp -a "$D/dump.rdb" "$D/appendonlydir" "$B/"
  echo "[backup] 快照就绪：$B"; du -sh "$B"
'
echo "[backup] done。apply 前请就近重跑本脚本取最新快照。"
