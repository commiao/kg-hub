#!/bin/sh
# kg-hub 管线看门 -> 飞书。每 20 分钟轮询,【只在出问题或问题恢复时】说话。
#
# 2026-09-17 重写。旧版的契约是"计数一变就播报"。问题有两层：
#
#   1) 它数的是退役 ingester 的水位线(.ingested.claude_mem.json),那个文件
#      2026-06-11 就冻住了。所以它安静了 96 天 —— 而"安静"在旧契约下等于
#      "没有新进展",看的人自然读成"没事"。真实情况是积压 7489、当轮入图 0。
#      取数已挪到 lib-nas-read.sh,那里说明了为什么必须只有一份。
#   2) 就算修好数据源,"一变就播报"现在会变成每 20 分钟一条、一天 70 条。
#      吞吐量的日常汇报是 daily-summary.sh 的活;这里改成看门狗：
#      **正常时一个字都不说**。
#
# 边沿触发：同一个问题只在发生时喊一次,恢复时喊一次。不重复刷屏。
# 宕机/连不上由 check.sh 负责,这里读不到就静默退出。
WH=$(cat /root/uptime/webhook.conf 2>/dev/null)
STATEF="/root/uptime/state/progress-last.txt"
STALL=7200          # 入图计数这么久(秒)没涨就算停了
mkdir -p /root/uptime/state
. "$(dirname "$0")/lib-nas-read.sh"

send() { curl -s -m 10 -X POST "$WH" -H 'Content-Type: application/json' \
  -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$1\"}}" >/dev/null 2>&1; }

OUT=$(nas_read 3)
[ -z "$OUT" ] && exit 0            # 连不上 -> 静默,宕机归 check.sh

now=$(date +%s)
prev=$(cat "$STATEF" 2>/dev/null)
# 状态：累计入图 / 上次见到它上涨的时刻 / 当前已喊过的告警名(none 表示正常)
ping=$(echo "$prev" | awk '{print $1+0}')
psince=$(echo "$prev" | awk '{print $2+0}')
palarm=$(echo "$prev" | awk '{print ($3==""?"none":$3)}')
[ "$psince" -eq 0 ] && psince="$now"

case "$OUT" in
  ERR*)
    why=$(printf '%s' "$OUT" | cut -f2)
    if [ "$palarm" != "read" ]; then
      send "⚠️ kg-hub：读不到管线状态 —— $why"
      printf '%s %s read\n' "$ping" "$psince" > "$STATEF"
    fi
    exit 0 ;;
esac

ING=$(printf '%s' "$OUT" | cut -f2);  LAG=$(printf '%s' "$OUT" | cut -f4)
BACK=$(printf '%s' "$OUT" | cut -f5); AGE=$(printf '%s' "$OUT" | cut -f7)
HALT=$(printf '%s' "$OUT" | cut -f8); FLAGS=$(printf '%s' "$OUT" | cut -f9)
QUOTA=$(printf '%s' "$OUT" | cut -f10)
case "$ING" in ''|*[!0-9]*) exit 0 ;; esac   # 计数读不出 -> 交给日报去报,这里不重复喊

# 配额逼近上限:边沿触发喊一次。只在分母确实拿得到时判 —— 上限取不到时不猜,
# 那种情况由日报如实带出"配额上限取不到",不在这里制造一条假警报。
QHOT=$(printf '%s' "$QUOTA" | awk '{
  for (i = 1; i <= NF; i++) {
    if (split($i, a, "=") == 2 && split(a[2], b, "/") == 2 && b[2] ~ /^[0-9]+$/) {
      if (b[2] > 0 && b[1] * 100 / b[2] >= 90) printf "%s ", $i
    }
  }
}')
if [ -n "$QHOT" ] && [ "$palarm" != "quota" ]; then
  send "⚠️ kg-hub：网关配额逼近上限（${QHOT}）。今日用量：$QUOTA"
  printf '%s %s quota\n' "$ING" "$psince" > "$STATEF"
  exit 0
fi

# 计数涨了 = 管线在干活。刷新"上次前进时刻",并在刚从告警里出来时报一次恢复。
if [ "$ING" -gt "$ping" ]; then
  [ "$palarm" = "none" ] || send "✅ kg-hub：管线恢复推进（累计入图 ${ING}、积压 ${BACK}）。"
  printf '%s %s none\n' "$ING" "$now" > "$STATEF"
  exit 0
fi

# 没涨。够久了就喊一次停摆,喊过就闭嘴,等它恢复时再说。
stalled=$((now - psince))
if [ "$stalled" -gt "$STALL" ] && [ "$palarm" != "stall" ]; then
  send "🔴 kg-hub：管线已 $((stalled / 3600)) 小时没有新入图（累计 ${ING}、落后 $LAG 条、积压 ${BACK}、本轮 halted ${HALT}、标志 ${FLAGS}）。"
  printf '%s %s stall\n' "$ING" "$psince" > "$STATEF"
  exit 0
fi
printf '%s %s %s\n' "$ING" "$psince" "$palarm" > "$STATEF"
exit 0
