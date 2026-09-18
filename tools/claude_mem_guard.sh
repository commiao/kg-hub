#!/bin/sh
# claude-mem 空转守护(防 issue #2188 复发)。
#
# 背景:claude-mem 的 bun-runner 偶发收到空 stdin 负载(issue #2188)后会进入
# CPU 死循环;这类 "hook" 调用本应几秒内结束,卡死后会以孤儿进程(PPID=1)
# 形态长期空转,曾连烧 9 天 ~270% CPU 未被发现。
#
# 判定:一个命令含 claude-mem 且含 "hook" 的进程,若累计 CPU 时间 > 阈值,
# 必是空转(正常 hook 累计 CPU < 10s)。用「累计 CPU 时间」而非瞬时 %CPU,
# 既躲开 macOS %cpu 是生命周期均值的坑,也不会误杀正在跑 LLM 的合法 hook。
# 常驻 daemon(命令含 --daemon、不含 hook)永不触碰。
#
# 由 launchd com.kg-hub.claude-mem-guard 每 300s 调用;仅在真的清理了进程时
# 才发飞书,平时静默。

CPU_TIME_THRESHOLD=120   # 累计 CPU 秒数;超过即判定空转
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE=${KG_HUB_ENV_FILE:-"$SCRIPT_DIR/../.env"}
LOG="$HOME/.kg-hub/logs/claude-mem-guard.log"
mkdir -p "$(dirname "$LOG")"
ts() { date '+%F %T'; }

WEBHOOK=$(grep '^KG_HUB_FEISHU_WEBHOOK=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"')

# ---- 人工断路器同步 -------------------------------------------------------
# 拓扑图上 claude-mem 那个开关的执行链条:kg-hub(权威) → 这里(每 300s 拉一次)
# → Mac 本地文件 → session_forwarder 出门前读它。
#
# 为什么不是「停 worker」:要求是断开时 claude-mem 不受影响、不丢数据。worker 是
# 队列消费者,pending_messages 只有 pending/processing 两态、没有 failed,成功消费
# 才删行。拦在它出门那一步,hook 照常收、队列照常积、一条不丢,开关一开下一轮自己
# 接着处理——队列本身就是断点。停掉 worker 则会把采集一起停,那才叫受影响。
#
# 拉不到就**保持上次已知值**:绝不因为读不到 kg-hub 就自己合上闸。
#
# 但「保持上次已知值」有个前提:得真有过上次。2026-09-18 查出这条链路从 09-10
# 接线那天起就没通过一次 —— 回落地址写的是 127.0.0.1:17171,而 kg-hub 只跑在
# NAS 上,Mac 这边压根没有这个端口。于是每轮 curl 都失败、STATE 恒为空、文件
# 从未被创建;中继读不到文件按「从没配过=通行」放行(三态设计里对的那一档)。
# 结果:拓扑页显示「已接线」、ENFORCED=True、guard 跑了 2784 轮退出码全 0,而
# 那个开关按下去什么都不会发生。整整八天没人看得出来。
#
# 所以两处改:
#   1) 回落地址对齐全仓库 Mac 侧工具的写法(weekly_report.py / feedback_digest.py
#      都是 100.123.208.32:17171)。kg-hub 在 NAS,loopback 从来就不对。
#   2) **区分「抖了一下」和「从没通过」**。前者保持原样是对的;后者没有任何
#      已知值可保持,沉默就是在撒谎。文件不存在时必须出声 —— 每轮进日志,并按天
#      去重发一次飞书,免得再烂八天。
BREAKER_FILE="$HOME/.claude-mem/.model-gateway/activation-$(
  ls "$HOME/.claude-mem/.model-gateway" 2>/dev/null \
    | sed -n 's/^activation-\([0-9a-f]\{32\}\)\.json$/\1/p' | head -1
).breaker.json"
KG_URL=$(grep '^KG_HUB_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"')
case "$BREAKER_FILE" in
  *"activation-.breaker.json") ;;                 # 没找到激活记录:不动
  *)
    STATE=$(curl -s -m 8 "${KG_URL:-http://100.123.208.32:17171}/dashboard/breakers" 2>/dev/null \
      | python3 -c '
import json,sys
try:
    d = json.load(sys.stdin)["breakers"]["claude_mem.observation"]
except Exception:
    sys.exit(1)                      # 拉不到:退出码非零,下面保持原样
print(json.dumps({"version": 1, "tripped": bool(d["tripped"]),
                  "reason": d.get("reason") or ""}, ensure_ascii=False))' 2>/dev/null)
    if [ -n "$STATE" ]; then
      TMP="$BREAKER_FILE.tmp.$$"
      printf '%s' "$STATE" > "$TMP" 2>/dev/null \
        && chmod 600 "$TMP" 2>/dev/null \
        && mv -f "$TMP" "$BREAKER_FILE" 2>/dev/null
      rm -f "$TMP" 2>/dev/null
    elif [ ! -f "$BREAKER_FILE" ]; then
      # 没拿到新值、也没有旧值 = 这个开关根本没接上。这是故障,不是静默档。
      echo "$(ts) [断路器] 同步失败且本地无任何已知状态 —— claude-mem 那路开关当前不生效(KG_URL=${KG_URL:-默认})" >> "$LOG"
      MARK="$HOME/.kg-hub/state/breaker-unwired-$(date '+%Y%m%d')"
      if [ -n "$WEBHOOK" ] && [ ! -f "$MARK" ]; then
        mkdir -p "$(dirname "$MARK")" 2>/dev/null && : > "$MARK"
        curl -s -m 10 -X POST "$WEBHOOK" -H 'Content-Type: application/json' \
          -d '{"msg_type":"text","content":{"text":"🔴 kg-hub:claude-mem 断路器未接通 —— guard 拉不到 /dashboard/breakers 且本地无已知状态,拓扑页上那个开关按下去不会生效。"}}' \
          >/dev/null 2>&1
      fi
    fi
    ;;
esac

# 找出累计 CPU 时间超阈值的 claude-mem hook 进程(tosec 解析 [hh:]mm:ss.ss)
CANDIDATES=$(ps -axo pid=,cputime=,command= 2>/dev/null | awk -v lim="$CPU_TIME_THRESHOLD" '
  function tosec(t,  a,n,s,i){ n=split(t,a,":"); s=0; for(i=1;i<=n;i++) s=s*60+a[i]; return s }
  /claude-mem/ && /hook/ && !/awk/ { if (tosec($2) > lim) print $1 }')

[ -z "$CANDIDATES" ] && exit 0

n=0; killed=""
for p in $CANDIDATES; do
  if kill "$p" 2>/dev/null; then n=$((n+1)); killed="$killed $p"; fi
done
[ "$n" -eq 0 ] && exit 0

# 给顽固的补一刀
sleep 2
for p in $killed; do kill -9 "$p" 2>/dev/null; done

echo "$(ts) killed $n runaway claude-mem hook proc(s):$killed (cputime>${CPU_TIME_THRESHOLD}s, issue#2188)" >> "$LOG"

if [ -n "$WEBHOOK" ]; then
  TEXT="⚠️ claude-mem 守护: 清理了 $n 个空转 hook 进程(issue#2188,PID:$killed),已释放 CPU。若频繁复发,建议升级/排查 claude-mem 插件。"
  curl -s -m 10 -X POST "$WEBHOOK" -H 'Content-Type: application/json' \
    -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$TEXT\"}}" >/dev/null 2>&1
fi
exit 0
