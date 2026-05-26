#!/bin/bash
# kg-query.sh — query the central knowledge graph (kg-hub) over Tailscale.
#
# Usage:
#   kg-query.sh <question>              # 10 results default
#   kg-query.sh <question> <num>        # custom num_results (1-30)
#
# Reads:
#   KG_HUB_URL       e.g. http://mac-office:8080 (from ~/.openclaw/env.sh)
#   KG_HUB_TOKEN     Bearer token (same as Mac's KG_HUB_API_TOKEN)
#
# Exit codes:
#   0  success (returns JSON on stdout)
#   2  bad args
#   3  env vars missing
#   4  curl failed (kg-hub unreachable, etc.)

set -euo pipefail

# Source env if not already loaded
if [ -z "${KG_HUB_URL:-}" ] || [ -z "${KG_HUB_TOKEN:-}" ]; then
    if [ -f "$HOME/.openclaw/env.sh" ]; then
        # shellcheck disable=SC1090,SC1091
        source "$HOME/.openclaw/env.sh"
    fi
fi

if [ -z "${KG_HUB_URL:-}" ]; then
    echo "error: KG_HUB_URL not set (add to ~/.openclaw/env.sh)" >&2
    exit 3
fi
if [ -z "${KG_HUB_TOKEN:-}" ]; then
    echo "error: KG_HUB_TOKEN not set (add to ~/.openclaw/env.sh)" >&2
    exit 3
fi

if [ $# -lt 1 ]; then
    echo "usage: $0 <question> [num_results]" >&2
    exit 2
fi

QUERY="$1"
NUM_RESULTS="${2:-10}"

# 15s timeout: Mac usually ms; if longer, kg-hub or network is degraded.
if ! curl -fsS --max-time 15 \
        -G "$KG_HUB_URL/api/search" \
        --data-urlencode "q=$QUERY" \
        --data-urlencode "num_results=$NUM_RESULTS" \
        -H "Authorization: Bearer $KG_HUB_TOKEN"; then
    echo "" >&2
    echo "error: kg-hub query failed. Mac may be offline or Tailscale degraded." >&2
    exit 4
fi
