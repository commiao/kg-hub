#!/bin/sh
# kg-hub 工具链路快照:一跑出「4 工具 × 3 链路(MCP / PUSH / capture)」现状。
#
# 用法:用完某个工具后跑一下,看哪条链路有"刚刚的活动"。
#   sh tools/verify_tool_links.sh
#
# 判定靠"信号",不用进每个 IDE:
#   - 配置存在性  -> 接没接(grep 各工具配置)
#   - push_hook.log 时间戳 -> PUSH 注入最近一次触发
#   - sdk_sessions.platform_source -> capture 最近一次会话
#   - /api/* -> 中央服务可达 + usage_count 最近 bump

ENVF="$HOME/.claude-mem/.env"
URL=$(grep '^KG_HUB_URL=' "$ENVF" 2>/dev/null | cut -d= -f2- | tr -d '"')
TOK=$(grep '^KG_HUB_API_TOKEN=' "$ENVF" 2>/dev/null | cut -d= -f2- | tr -d '"')
DB="$HOME/.claude-mem/claude-mem.db"
PLOG="$HOME/workspace_claudeCode/kg-hub/data/.push_hook.log"
CMHOOK='/Users/mac/.claude/plugins/marketplaces/thedotmack/plugin/scripts'

ok() { printf "✅"; }; no() { printf "❌"; }; warn() { printf "🔶"; }
has() { grep -q "$1" "$2" 2>/dev/null && ok || no; }
api() { curl -s -m 8 -H "Authorization: Bearer $TOK" "$URL$1" 2>/dev/null; }

echo "==================== kg-hub 工具链路快照 ===================="
echo "时间: $(date '+%F %T')   中央: $URL"

# ---- 中央服务 ----
HEALTH=$(curl -s -m 6 "$URL/health" 2>/dev/null)
STATS=$(api /api/stats)
printf "中央服务: /health=%s  " "$(echo "$HEALTH" | grep -q ok && echo ok || echo 不可达)"
echo "$STATS" | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"图: {d.get('entities')} 实体 / {d.get('edges')} 边 / {d.get('episodes')} episode\")" 2>/dev/null || echo "(stats 取不到)"

# ---- 源 obs + usage 最近 bump ----
if [ -f "$DB" ]; then
  OBS=$(sqlite3 "$DB" "SELECT count(*)||' (最新 '||substr(max(created_at),1,16)||')' FROM observations" 2>/dev/null)
  echo "源 obs: $OBS"
fi
LASTBUMP=$(api "/api/usage_ranking?top_n=1" | python3 -c "import sys,json;d=json.load(sys.stdin);t=d.get('top_canonical') or [{}];print('usage_count 最近 bump:',t[0].get('last_used_at','—'),'| 总事件',d.get('stats',{}).get('total_usage_events'))" 2>/dev/null)
echo "$LASTBUMP"

echo "------------------------------------------------------------"
printf "%-12s %-14s %-26s %s\n" "工具" "MCP(muxcp)" "PUSH(配置/最近触发)" "capture(最近会话)"

# 最近一次某 fmt 的 push 触发时间
pushts() { grep "fmt=$1" "$PLOG" 2>/dev/null | tail -1 | awk '{print $1}' | cut -c1-16; }
# 某 platform_source 的最近会话
capts() { sqlite3 "$DB" "SELECT substr(max(started_at),1,16) FROM sdk_sessions WHERE platform_source='$1'" 2>/dev/null; }

# Claude Code
printf "%-12s " "ClaudeCode"
printf "%s启动参数      " "$(ok)"
printf "%s settings/%-10s " "$(has kg_push_hook /Users/mac/.claude/settings.json)" "$(pushts claude)"
printf "%s claude/%s\n" "$( [ -n "$(capts claude)" ] && ok || warn )" "$(capts claude)"

# Cursor
printf "%-12s " "Cursor"
printf "%s mcp.json     " "$(has muxcp /Users/mac/.cursor/mcp.json)"
printf "%s hooks/%-12s " "$(has kg_push_hook /Users/mac/workspace_cursor/.cursor/hooks.json)" "$(pushts cursor)"
printf "%s cursor/%s\n" "$( [ -n "$(capts cursor)" ] && ok || warn )" "$(capts cursor)"

# Codex
printf "%-12s " "Codex"
printf "%s config.toml  " "$(has muxcp /Users/mac/.codex/config.toml)"
printf "%s AGENTS(pull)/%-3s " "$(has kg-hub /Users/mac/.codex/AGENTS.md)" "$(pushts codex)"
printf "%s codex/%s\n" "$( [ -n "$(capts codex)" ] && ok || warn )" "$(capts codex)"

# Qoder
printf "%-12s " "Qoder"
printf "%s mcp.json     " "$(has muxcp /Users/mac/.qoder/mcp.json)"
printf "%s settings(待验)    " "$(has kg_push_hook /Users/mac/.qoder/settings.json)"
printf "%s %s\n" "$( [ -n "$(capts qoder)" ] && ok || warn )" "$(capts qoder | sed 's/^/qoder\//')"

echo "------------------------------------------------------------"
echo "图例: ✅通/已配  🔶待验证或无近期活动  ❌未接"
echo "提示: 用完某工具后再跑一次,对应行的'最近触发/最近会话'时间应刷新成刚才。"
