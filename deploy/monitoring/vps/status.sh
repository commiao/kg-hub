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
echo "--- [NAS] 管线实况 ---"
# 这里原来打的是 .ingested.claude_mem.json —— 退役 ingester 的水位线,2026-06-11
# 起就是个死数字。人手动跑 status 正是为了判断"现在到底好不好",给一个三个月
# 没动过的常数比不给更坏。改走 lib-nas-read.sh 的唯一真源。
. "$(dirname "$0")/lib-nas-read.sh"
PIPE=$(nas_read 2)
case "$PIPE" in
  '')    echo "  (NAS 暂不可达)" ;;
  ERR*)  printf '  ⚠️ %s\n' "$(printf '%s' "$PIPE" | cut -f2)" ;;
  *)     printf '  累计入图=%s 拒=%s｜游标落后=%s 积压=%s 图节点=%s\n' \
           "$(printf '%s' "$PIPE" | cut -f2)" "$(printf '%s' "$PIPE" | cut -f3)" \
           "$(printf '%s' "$PIPE" | cut -f4)" "$(printf '%s' "$PIPE" | cut -f5)" \
           "$(printf '%s' "$PIPE" | cut -f6)"
         printf '  心跳=%ss前 本轮halted=%s 暂停标志=%s\n' \
           "$(printf '%s' "$PIPE" | cut -f7)" "$(printf '%s' "$PIPE" | cut -f8)" \
           "$(printf '%s' "$PIPE" | cut -f9)" ;;
esac
echo "--- [NAS] watchdog + 反向探针 ---"
ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$NAS" '
ENVF=/volume1/docker/kg-hub-src/.env
ROOT=$(sed -n "s/^KG_HUB_DATA_ROOT=//p" "$ENVF" 2>/dev/null)
python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(\"  watchdog last_run=\"+d[\"last_run\"][11:19]+\"  异常:\"+(str({k for k,v in d[\"anomalies\"].items() if v}) if any(d[\"anomalies\"].values()) else \"无\"))" "$ROOT/watchdog/state/watchdog.json" 2>/dev/null || echo "  watchdog 状态读不到"
echo "  --- NAS->VPS 反向探针 (容器 kg-hub-nas-probe) ---"
for f in /volume1/docker/nas-probe/state/*.status; do [ -f "$f" ] && printf "  %s=%s fails=%s\n" "$(basename "$f" .status)" "$(cat "$f" 2>/dev/null)" "$(cat "${f%.status}.fails" 2>/dev/null)"; done
' 2>/dev/null || echo "  (NAS 暂不可达)"
echo "--- 告警通道 ---"
echo "  飞书 webhook(kg-hub 群) + feishu-notify skill(~/.claude/skills/)"
echo "===================================================="
