#!/bin/sh
# 通用轻量探针 -> 飞书。配置驱动,边沿触发(挂了报一次、恢复报一次,不刷屏)。
# targets.conf 每行:  name|health_url|webhook(可空)|fail_threshold
# webhook 留空 -> 回退读同目录 webhook.conf(避免在 targets.conf 硬编码/泄密)。
BASE=/root/uptime; CONF=$BASE/targets.conf; STATE=$BASE/state
mkdir -p "$STATE"
now=$(date '+%Y-%m-%d %H:%M:%S')
DEFAULT_WH=$(cat "$BASE/webhook.conf" 2>/dev/null)
send() { curl -s -m 10 -X POST "$1" -H 'Content-Type: application/json' \
  -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$2\"}}" >/dev/null 2>&1; }
grep -vE '^[[:space:]]*(#|$)' "$CONF" 2>/dev/null | while IFS='|' read -r name url webhook thr; do
  [ -z "$name" ] && continue
  thr=${thr:-3}; [ -z "$webhook" ] && webhook="$DEFAULT_WH"
  sf="$STATE/$name.fails"; ss="$STATE/$name.status"
  fails=$(cat "$sf" 2>/dev/null || echo 0); status=$(cat "$ss" 2>/dev/null || echo UP)
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null)
  if [ "$code" = "200" ]; then
    [ "$status" = "DOWN" ] && send "$webhook" "✅ [$name] 恢复 (HTTP 200) @ $now"
    echo 0 > "$sf"; echo UP > "$ss"
  else
    fails=$((fails+1)); echo "$fails" > "$sf"
    if [ "$fails" -ge "$thr" ] && [ "$status" != "DOWN" ]; then
      send "$webhook" "🔴 [$name] 不可达 (code=$code, 连续失败 ${fails}次) @ $now"
      echo DOWN > "$ss"
    fi
  fi
done
