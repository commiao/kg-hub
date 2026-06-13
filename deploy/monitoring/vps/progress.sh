#!/bin/sh
# kg-hub 进度 -> 飞书。每 20 分钟轮询,但【只在 claude-mem/openclaw 计数变化时】才通知。
# 无变化 -> 静默(不打扰)。宕机/恢复由 check.sh 负责;每日心跳由 daily-summary.sh 负责。
WH=$(cat /root/uptime/webhook.conf 2>/dev/null)
NAS="commiao@100.123.208.32"
STATEF="/root/uptime/state/progress-last.txt"
mkdir -p /root/uptime/state
READ='CM=$(python3 -c "import json,os;p=\"/volume1/docker/kg-hub-data/ingest-state/.ingested.claude_mem.json\";print(len(json.load(open(p))[\"ingested_obs_ids\"]) if os.path.exists(p) else 0)" 2>/dev/null); OC=$(python3 -c "import json,os;p=\"/volume1/docker/kg-hub-data/ingest-state/.ingested.json\";print(len(json.load(open(p))) if os.path.exists(p) else 0)" 2>/dev/null); NODES=$(redis-cli -h 127.0.0.1 -a "$(cat /volume1/docker/kg-hub-data/dbpass.conf 2>/dev/null)" --no-auth-warning GRAPH.QUERY kg_hub "MATCH (n) RETURN count(n)" 2>/dev/null | sed -n 2p); printf "%s\t%s\t%s" "$CM" "$OC" "$NODES"'
OUT=""
for i in 1 2 3; do
  OUT=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$NAS" "$READ" 2>/dev/null)
  [ -n "$OUT" ] && break
  sleep 5
done
[ -z "$OUT" ] && exit 0   # 读不到 -> 静默(宕机由 check.sh 报)
CM=$(printf "%s" "$OUT" | cut -f1); OC=$(printf "%s" "$OUT" | cut -f2); NODES=$(printf "%s" "$OUT" | cut -f3)
[ -z "$CM" ] && exit 0
cur="$CM $OC"
prev=$(cat "$STATEF" 2>/dev/null)
[ "$cur" = "$prev" ] && exit 0   # 无变化 -> 静默
# 有变化 -> 算增量并通知
pcm=$(echo "$prev" | awk '{print $1+0}'); poc=$(echo "$prev" | awk '{print $2+0}')
dcm=$((CM - pcm)); doc=$((OC - poc))
echo "$cur" > "$STATEF"
TEXT="🔄 kg-hub 新增: claude-mem=$CM(+$dcm) openclaw=$OC(+$doc) 图节点=$NODES"
curl -s -m 10 -X POST "$WH" -H 'Content-Type: application/json' \
  -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$TEXT\"}}" >/dev/null 2>&1
