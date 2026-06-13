#!/bin/sh
# kg-hub 每日汇总 -> 飞书。每天 22:00 发一条(无论是否有新增),作为"还活着"的心跳。
# 与上次汇总(24h前)对比,报今日新增;读不到 NAS 也发一条(提示异常)。
WH=$(cat /root/uptime/webhook.conf 2>/dev/null)
NAS="commiao@100.123.208.32"
BASEF="/root/uptime/state/daily-baseline.txt"
mkdir -p /root/uptime/state
READ='CM=$(python3 -c "import json,os;p=\"/volume1/docker/kg-hub-data/ingest-state/.ingested.claude_mem.json\";print(len(json.load(open(p))[\"ingested_obs_ids\"]) if os.path.exists(p) else 0)" 2>/dev/null); OC=$(python3 -c "import json,os;p=\"/volume1/docker/kg-hub-data/ingest-state/.ingested.json\";print(len(json.load(open(p))) if os.path.exists(p) else 0)" 2>/dev/null); NODES=$(redis-cli -h 127.0.0.1 -a "$(cat /volume1/docker/kg-hub-data/dbpass.conf 2>/dev/null)" --no-auth-warning GRAPH.QUERY kg_hub "MATCH (n) RETURN count(n)" 2>/dev/null | sed -n 2p); printf "%s\t%s\t%s" "$CM" "$OC" "$NODES"'
OUT=""
for i in 1 2 3; do
  OUT=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$NAS" "$READ" 2>/dev/null)
  [ -n "$OUT" ] && break
  sleep 8
done
send() { curl -s -m 10 -X POST "$WH" -H 'Content-Type: application/json' \
  -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$1\"}}" >/dev/null 2>&1; }
if [ -z "$OUT" ]; then
  send "⚠️ kg-hub 日报: 无法读取 NAS(VPS 探针仍在运行)。请检查 NAS/容器是否在线。"
  exit 0
fi
CM=$(printf "%s" "$OUT" | cut -f1); OC=$(printf "%s" "$OUT" | cut -f2); NODES=$(printf "%s" "$OUT" | cut -f3)
prev=$(cat "$BASEF" 2>/dev/null)
echo "$CM $OC" > "$BASEF"
if [ -z "$prev" ]; then
  send "📊 kg-hub 日报(首次基线): claude-mem=$CM openclaw=$OC 图节点=$NODES。系统在线。"
  exit 0
fi
pcm=$(echo "$prev" | awk '{print $1+0}'); poc=$(echo "$prev" | awk '{print $2+0}')
dcm=$((CM - pcm)); doc=$((OC - poc))
if [ "$dcm" -eq 0 ] && [ "$doc" -eq 0 ]; then
  send "📊 kg-hub 日报: 今日无新增(claude-mem=$CM openclaw=$OC 图节点=$NODES)。系统正常在线。"
else
  send "📊 kg-hub 日报: 今日新增 claude-mem +$dcm, openclaw +$doc(现 cm=$CM oc=$OC 节点=$NODES)。"
fi
exit 0
