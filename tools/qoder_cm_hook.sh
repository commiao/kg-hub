#!/bin/sh
# claude-mem hook wrapper for Qoder (desktop + IDEA plugin).
#
# Why: Qoder runs the claude-mem hook with process.cwd() pointing at the plugin
# dir (esp. the IDEA plugin), so claude-mem's project derivation
#   GEMINI_CWD ?? GEMINI_PROJECT_DIR ?? CLAUDE_PROJECT_DIR ?? process.cwd()
# falls through to process.cwd() = ".../claude-mem/<version>" and labels the
# project "13.6.0". We set CLAUDE_PROJECT_DIR from QODER_PROJECT_DIR (Qoder sets
# it to the real project for both desktop and IDEA) — or the payload cwd as a
# fallback — so claude-mem records the correct project. Then forward the payload
# to the real claude-mem worker (resolved to the newest installed version).
#
# Usage (from ~/.qoder/settings.json hooks): qoder_cm_hook.sh <mode>
#   modes: context | session-init | observation | file-context | summarize
MODE="$1"
PAYLOAD=$(cat)

# Pick the real project dir: QODER_PROJECT_DIR (most reliable) -> payload.cwd.
PROJ="${QODER_PROJECT_DIR:-}"
if [ -z "$PROJ" ]; then
  PROJ=$(printf '%s' "$PAYLOAD" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('cwd') or '')
except Exception: print('')" 2>/dev/null)
fi
# Never let the plugin dir masquerade as the project.
case "$PROJ" in
  *claude-mem*|*/plugins/*|"") PROJ="" ;;
esac
[ -n "$PROJ" ] && [ -d "$PROJ" ] && export CLAUDE_PROJECT_DIR="$PROJ"

# Resolve newest claude-mem plugin (matches claude-mem's own hook resolution).
_C="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
_P=$({ ls -dt "$_C/plugins/cache/thedotmack/claude-mem"/[0-9]*/ 2>/dev/null; \
       printf '%s\n' "$_C/plugins/marketplaces/thedotmack/plugin"; } \
  | while IFS= read -r _R; do _R="${_R%/}"; \
      [ -d "$_R/plugin/scripts" ] && _Q="$_R/plugin" || _Q="$_R"; \
      [ -f "$_Q/scripts/worker-service.cjs" ] && { printf '%s\n' "$_Q"; break; }; \
    done)
[ -n "$_P" ] || { echo "claude-mem: plugin not found" >&2; exit 0; }

export PATH="$($SHELL -lc 'echo $PATH' 2>/dev/null):$PATH"
printf '%s' "$PAYLOAD" | node "$_P/scripts/bun-runner.js" "$_P/scripts/worker-service.cjs" hook claude-code "$MODE"
