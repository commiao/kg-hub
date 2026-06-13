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
ENV_FILE="$HOME/.claude-mem/.env"
LOG="$HOME/.kg-hub/logs/claude-mem-guard.log"
mkdir -p "$(dirname "$LOG")"
ts() { date '+%F %T'; }

WEBHOOK=$(grep '^KG_HUB_FEISHU_WEBHOOK=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"')

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
