#!/bin/sh
# kg-hub 监控全景:一条命令看清 VPS+NAS 所有探针/容器/进度/健康。
NAS="commiao@100.123.208.32"
DK="sudo -n /var/packages/ContainerManager/target/usr/bin/docker"
echo "================== kg-hub 监控全景 =================="
echo "时间: $(date '+%F %T')"
echo
echo "--- [VPS] 探针目标 + 状态 (check.sh 每分钟; progress.sh 7/27/47) ---"
while IFS='|' read -r name url webhook thr; do
  case "$name" in ''|\#*) continue;; esac
  st=$(cat "/root/uptime/state/$name.status" 2>/dev/null || echo "?")
  fa=$(cat "/root/uptime/state/$name.fails" 2>/dev/null || echo "?")
  code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null)
  printf "  %-10s 状态=%-4s fails=%-2s 实时HTTP=%-3s  %s\n" "$name" "$st" "$fa" "$code" "$url"
done < /root/uptime/targets.conf
echo
echo "--- [VPS] cron ---"
crontab -l 2>/dev/null | grep "uptime/" | sed 's/^/  /'
echo
echo "--- [NAS] 容器(经 VPS->NAS)---"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$NAS" "$DK ps --format '{{.Names}}  {{.Status}}' 2>/dev/null | grep kg-hub | sed 's/^/  /'" 2>/dev/null || echo "  (NAS 暂不可达)"
echo "--- [NAS] watchdog + 重建进度 ---"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$NAS" '
python3 -c "import json;d=json.load(open(\"/volume1/docker/kg-hub-data/watchdog/state/watchdog.json\"));print(\"  watchdog last_run=\"+d[\"last_run\"][11:19]+\"  异常:\"+(str({k for k,v in d[\"anomalies\"].items() if v}) if any(d[\"anomalies\"].values()) else \"无\"))" 2>/dev/null
python3 -c "import json,os;p=\"/volume1/docker/kg-hub-data/ingest-state/.ingested.claude_mem.json\";print(\"  claude-mem ingested=\"+str(len(json.load(open(p))[\"ingested_obs_ids\"]) if os.path.exists(p) else 0))" 2>/dev/null
python3 -c "import json,os;p=\"/volume1/docker/kg-hub-data/ingest-state/.ingested.json\";print(\"  openclaw   ingested=\"+str(len(json.load(open(p))) if os.path.exists(p) else 0))" 2>/dev/null
redis-cli -h 127.0.0.1 -a "$(cat /volume1/docker/kg-hub-data/dbpass.conf 2>/dev/null)" --no-auth-warning GRAPH.QUERY kg_hub "MATCH (n) RETURN count(n)" 2>/dev/null | sed -n 2p | sed "s/^/  图节点=/"
echo "  --- NAS->VPS 反向探针 (容器 kg-hub-nas-probe) ---"
for f in /volume1/docker/nas-probe/state/*.status; do [ -f "$f" ] && printf "  %s=%s fails=%s\n" "$(basename "$f" .status)" "$(cat "$f" 2>/dev/null)" "$(cat "${f%.status}.fails" 2>/dev/null)"; done
' 2>/dev/null || echo "  (NAS 暂不可达)"
echo "--- 告警通道 ---"
echo "  飞书 webhook(kg-hub 群) + feishu-notify skill(~/.claude/skills/)"
echo "===================================================="
