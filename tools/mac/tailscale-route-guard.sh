#!/bin/bash
# tailscale-route-guard — 守住 tailnet 关键主机的 /32 路由,不被企业 VPN 劫走。
#
# 背景(2026-08-14 实发):深信服 aTrust 连上后往路由表注入 100.64/11、100.96/15、
# 100.99/21 等比 tailscale 的 100.64/10 更长的前缀,按最长前缀优先把整个 CGNAT
# 段(含本机 tailscale IP 段)劫走 → NAS/VPS 全部黑洞,且 `tailscale ping` 仍通
# (它走用户态 socket 不经路由表),极具迷惑性。IT 不支持改 aTrust 策略,故在本机
# 用 /32 主机路由(最长前缀,必胜)把关键 peer 抢回来。
#
# 幂等:路由已正确则完全不动;tailscale 没跑则直接退出。只碰 PEERS 里那几个 /32,
# 不影响 aTrust 自身业务网段。
set -uo pipefail

TS_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
[ -x "$TS_BIN" ] || TS_BIN="$(command -v tailscale || true)"
[ -x "$TS_BIN" ] || exit 0

# 要保护的 tailnet 目标(NAS 知识库 / OpenClaw VPS)
PEERS="${TS_GUARD_PEERS:-100.123.208.32 100.79.177.102}"
LOG="/var/log/tailscale-route-guard.log"

myip="$("$TS_BIN" ip -4 2>/dev/null | head -1)"
[ -n "$myip" ] || exit 0    # tailscale 未运行/未登录

# 动态解析 tailscale 的 utun 接口——接口号每次重启都可能变,绝不能写死
iface="$(ifconfig 2>/dev/null | awk -v ip="$myip" '
  /^utun/ {i=$1}
  $1=="inet" && $2==ip {gsub(":","",i); print i; exit}')"
[ -n "$iface" ] || exit 0

for p in $PEERS; do
    cur="$(route -n get -host "$p" 2>/dev/null | awk '/interface:/{print $2}')"
    [ "$cur" = "$iface" ] && continue          # 已正确,不动
    route -n delete -host "$p" >/dev/null 2>&1
    if route -n add -host "$p" -interface "$iface" >/dev/null 2>&1; then
        echo "$(date '+%F %T') repaired $p: ${cur:-none} -> $iface" >> "$LOG"
    else
        echo "$(date '+%F %T') FAILED to pin $p -> $iface" >> "$LOG"
    fi
done
exit 0
