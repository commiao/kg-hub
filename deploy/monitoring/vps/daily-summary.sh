#!/bin/sh
# kg-hub 每日汇总 -> 飞书。每天 22:00 一条,既是心跳也是体检。
#
# 2026-09-17 重写。旧版三个月里每天都报"今日无新增…系统正常在线",而实际上
# refinery 落后 4450 条、积压 7490、当轮入图 0。两个独立的错让它永远说这句话:
#
#   1) 它数的是 ingesters/claude_mem_obs.py 的水位线 .ingested.claude_mem.json。
#      那个 ingester 早被 kg_refinery.py 取代,文件停在 2026-06-11,再也不会动。
#   2) 它把路径写死成 /volume1/docker/kg-hub-data,而真实数据根是
#      /volume2/4T/kg-hub-data(见 compose .env 的 KG_HUB_DATA_ROOT)。
#
# 所以本版的三条原则:
#
#   * **路径不写死**,从 compose .env 读 KG_HUB_DATA_ROOT —— 跟 release.sh
#     校验的是同一个来源,数据根再迁一次也不会再漂。
#   * **读不到就报警,绝不静默当 0**。旧版 `if os.path.exists(p) else 0` 正是
#     它能冻三个月没人发现的原因:文件没了跟"今天没新增"长得一模一样。
#   * **量干活,不量活着**。进程活着不等于在处理。今日入图 0 是**告警**,
#     不是"正常在线"。
WH=$(cat /root/uptime/webhook.conf 2>/dev/null)
NAS="commiao@100.123.208.32"
BASEF="/root/uptime/state/daily-baseline.txt"
STALE=1800          # 心跳超过这么久(秒)就当 refinery 停了
mkdir -p /root/uptime/state

# 取数逻辑是三个监控脚本共用的,见 lib-nas-read.sh 里为什么必须共用。
. "$(dirname "$0")/lib-nas-read.sh"
OUT=$(nas_read 3)

send() { curl -s -m 10 -X POST "$WH" -H 'Content-Type: application/json' \
  -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$1\"}}" >/dev/null 2>&1; }

# 连不上 NAS：报警。探针自己活着不算数。
if [ -z "$OUT" ]; then
  send "⚠️ kg-hub 日报：连不上 NAS（VPS 探针仍在运行）。请检查 NAS/容器是否在线。"
  exit 0
fi
# 远端明确说读不到：把它说的原因原样带出来,不要吞成 0。
case "$OUT" in
  ERR*) send "⚠️ kg-hub 日报：$(printf '%s' "$OUT" | cut -f2)"; exit 0 ;;
esac

ING=$(printf '%s' "$OUT" | cut -f2);  REJ=$(printf '%s' "$OUT" | cut -f3)
LAG=$(printf '%s' "$OUT" | cut -f4);  BACK=$(printf '%s' "$OUT" | cut -f5)
NODES=$(printf '%s' "$OUT" | cut -f6); AGE=$(printf '%s' "$OUT" | cut -f7)
HALT=$(printf '%s' "$OUT" | cut -f8); FLAGS=$(printf '%s' "$OUT" | cut -f9)

# 计数字段缺失也要报警,别拿空值去算差 —— 那正是旧版把"文件没了"算成
# "今天没新增"的同一个坑,只不过换了个位置。
for v in "$ING" "$REJ"; do
  case "$v" in
    ''|*[!0-9]*) send "⚠️ kg-hub 日报：refinery 状态里的计数读不出（入图=\"$ING\" 拒=\"$REJ\"）。"; exit 0 ;;
  esac
done

# 心跳停了 = refinery 没在跑。这一条比任何计数都优先。
if [ -n "$AGE" ] && [ "$AGE" -gt "$STALE" ]; then
  send "🔴 kg-hub 日报：refinery 心跳已停 $((AGE / 60)) 分钟（阈值 $((STALE / 60)) 分）。积压 $BACK、落后 $LAG 条。"
  exit 0
fi

prev=$(cat "$BASEF" 2>/dev/null)
printf '%s %s\n' "$ING" "$REJ" > "$BASEF"
tail="落后 $LAG 条、积压 $BACK、图节点 $NODES"
[ "$FLAGS" = "-" ] || tail="$tail｜$FLAGS"

if [ -z "$prev" ]; then
  send "📊 kg-hub 日报（首次基线）：累计入图 $ING、拒 $REJ。$tail"
  exit 0
fi
ping=$(echo "$prev" | awk '{print $1+0}'); prej=$(echo "$prev" | awk '{print $2+0}')
ding=$((ING - ping)); drej=$((REJ - prej))

# 今日入图 0 是告警,不是"正常"。旧版把这个当正常,于是三个月没人发现管线停了。
if [ "$ding" -le 0 ]; then
  send "🔴 kg-hub 日报：今日入图 0（拒 +$drej、本轮 halted $HALT）。$tail"
else
  send "📊 kg-hub 日报：今日入图 +$ding、拒 +$drej。$tail"
fi
exit 0
