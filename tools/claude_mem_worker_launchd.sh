#!/bin/sh
# claude-mem worker 的 launchd 启动器 —— 让 launchd 真正成为兜底看门人。
#
# 为什么需要这一层（两个独立的坑）：
#
# 1) plist 里写死版本号路径。2026-09-10 写的 plist 指向 13.24.5，9-12 升到
#    13.24.23，9-17 回收旧 cache，于是 launchd 的 program 路径彻底不存在，
#    监管名存实亡。这里每次启动现查版本，升级不再需要改 plist。
#
# 2) 不能用 worker-wrapper.cjs。它在 inner 崩溃时打印 "hooks will restart if
#    needed" 然后 process.exit(0)；被 SIGKILL 时 code 是 null，退出码就成了 0，
#    正好落进 KeepAlive.SuccessfulExit=false 的"不重启"那一档 —— 崩溃反而是
#    唯一不会被拉起的情形。9-11 15:54 那次 SIGKILL 之后 worker 再没被 launchd
#    拉起来，就是这么来的。让 launchd 直接看住 worker-service 本身。
#
# 待机语义：hook 也会拉起 worker，而 worker 用一个固定端口做单例守卫。如果那份
# 还活着，这里**不抢**、也不空转重启（KeepAlive 会每 10s 拉一次，纯浪费还刷屏），
# 而是原地等端口空出来再接管。于是 launchd 始终是那个"谁死了我顶上"的角色。
set -eu

log() { echo "[launcher] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

# 单例端口以 settings.json 为准，别在这里再抄一份常量。
PORT=$(/usr/bin/python3 -c "
import json
try:
    print(json.load(open('$HOME/.claude-mem/settings.json')).get('CLAUDE_MEM_WORKER_PORT', 37701))
except Exception:
    print(37701)
" 2>/dev/null || echo 37701)

find_service() {
  # 输出 "<可排序版本键> <根优先级> <版本> <路径>"，最后按 版本降序、优先级升序
  # 取第一条。两个 key 都要有：只按版本排序时，两份 cache 同版本就成了平局，
  # sort 挑谁全看运气 —— 今天两份内容一样看不出问题，但将来两边升级不同步时，
  # "跑哪个版本"就变成了掷骰子。
  priority=0
  for root in \
    "$HOME/.claude/plugins/cache/thedotmack/claude-mem" \
    "$HOME/.codex/plugins/cache/claude-mem-local/claude-mem"
  do
    priority=$((priority + 1))
    [ -d "$root" ] || continue
    for dir in "$root"/*/; do
      [ -d "$dir" ] || continue
      # 插件把废弃版本打上 .orphaned_at；hook 的版本发现会跳过它，这里必须一致，
      # 否则 launchd 会把一个插件认为已死的版本拉起来。
      [ -e "${dir}.orphaned_at" ] && continue
      base=${dir%/}; base=${base##*/}
      case "$base" in [0-9]*.[0-9]*.[0-9]*) ;; *) continue ;; esac
      [ -f "${dir}scripts/worker-service.cjs" ] || continue
      # 零填充成定宽数字键，字典序即数值序（13.9.1 的字典序本来大于 13.24.23）。
      maj=${base%%.*}; rest=${base#*.}; min=${rest%%.*}; patch=${rest#*.}
      patch=${patch%%[!0-9]*}
      printf '%08d%08d%08d %d %s %sscripts/worker-service.cjs\n' \
        "${maj:-0}" "${min:-0}" "${patch:-0}" "$priority" "$base" "$dir"
    done
  done
  # 第三个位置：marketplace 目录。它没有版本号子目录，是插件的「当前」那一份，
  # hook 的版本发现把它当最后兜底 —— 这里必须一致，否则两个 cache 都缺席时
  # hook 还能起而 launchd 起不来。给一个最低的版本键，只有前面都没有才轮到它。
  fallback="$HOME/.claude/plugins/marketplaces/thedotmack/plugin/scripts/worker-service.cjs"
  [ -f "$fallback" ] && printf '%s %d %s %s\n' "000000000000000000000000" 9 "marketplace" "$fallback"
}

pick() { sort -k1,1r -k2,2n | head -1; }

selected=$(find_service | pick)
[ -n "$selected" ] || { log "找不到任何已安装的 claude-mem worker-service.cjs" >&2; exit 1; }
version=$(echo "$selected" | cut -d" " -f3)
service=$(echo "$selected" | cut -d" " -f4)

# 已有 worker 占着单例端口 → 待机，别抢也别空转。
# 轮询要短。hook 在有会话时 6 秒内就能把 worker 拉起来，轮询慢了就永远抢不到，
# launchd 等于只是个摆设。5s 让 launchd 在绝大多数情况下拿到所有权 —— 这才是
# 目的：hook 起的那份是 ppid=1 的孤儿，没人管；launchd 起的才有 KeepAlive。
# 抢输了也没有坏处：worker 是靠端口做单例的，输的一方原地继续待机。
first_wait=1
while /usr/bin/nc -z -G 1 127.0.0.1 "$PORT" >/dev/null 2>&1; do
  # 只在第一次打日志，否则每 5 秒一行会把日志刷爆。
  [ "$first_wait" = 1 ] && { log "端口 $PORT 已被现有 worker 占用，转入待机（每 5s 重查）"; first_wait=0; }
  sleep 5
done

log "接管：版本 $version，$service"
cd "$(dirname "$service")"
# CLAUDE_MEM_MANAGED 沿用 worker-wrapper 给 inner 设的同一个值，让 worker 知道
# 自己有人管。不加 start/--daemon：那条路径会自行 daemonize 脱离，launchd 会
# 误判为"进程退出"从而反复重拉。这里要的是前台进程。
CLAUDE_MEM_MANAGED=true exec "$HOME/.bun/bin/bun" "$service"
