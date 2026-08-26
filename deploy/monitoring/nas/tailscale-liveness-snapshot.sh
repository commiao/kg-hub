#!/bin/sh
# NAS host-side producer：采样真实 Tailscale 在线态，给容器只读消费。
# 推荐由 DSM Task Scheduler 每分钟执行；失败时保留上一份文件，让消费者按 mtime
# 自动把它降级为 unknown，而不是拿静态配置或旧 online 冒充设备仍在线。
set -eu

OUT=${KG_HUB_DEVICE_LIVENESS_PATH:-/volume2/4T/kg-hub-data/device-liveness/tailscale-status.json}

if [ -n "${KG_HUB_TAILSCALE_BIN:-}" ]; then
  TS_BIN=$KG_HUB_TAILSCALE_BIN
else
  TS_BIN=
  for candidate in \
    /var/packages/Tailscale/target/bin/tailscale \
    /usr/local/bin/tailscale \
    /usr/bin/tailscale
  do
    if [ -x "$candidate" ]; then
      TS_BIN=$candidate
      break
    fi
  done
  if [ -z "$TS_BIN" ] && command -v tailscale >/dev/null 2>&1; then
    TS_BIN=$(command -v tailscale)
  fi
fi

if [ -z "$TS_BIN" ] || [ ! -x "$TS_BIN" ]; then
  echo "tailscale CLI not found; set KG_HUB_TAILSCALE_BIN" >&2
  exit 1
fi

OUT_DIR=$(dirname "$OUT")
mkdir -p "$OUT_DIR"
TMP="${OUT}.tmp.$$"
trap 'rm -f "$TMP"' EXIT HUP INT TERM

if [ -n "${KG_HUB_TAILSCALE_SOCKET:-}" ]; then
  set -- "--socket=$KG_HUB_TAILSCALE_SOCKET" status --json
else
  set -- status --json
fi

if ! "$TS_BIN" "$@" >"$TMP"; then
  echo "tailscale status --json failed; last good snapshot retained" >&2
  exit 1
fi

# mv 前做完整 JSON + 最小 Tailscale schema 校验。仅检查首字符会把 `{malformed`
# 原子替换成“新鲜坏文件”，反而让 last-good 保护失效。
JSON_PYTHON=${KG_HUB_JSON_PYTHON:-}
if [ -z "$JSON_PYTHON" ]; then
  for candidate in \
    /var/packages/Python3/target/usr/local/bin/python3 \
    /var/packages/Python3/target/bin/python3 \
    /usr/local/bin/python3 \
    /usr/bin/python3 \
    /bin/python3
  do
    if [ -x "$candidate" ]; then
      JSON_PYTHON=$candidate
      break
    fi
  done
  if [ -z "$JSON_PYTHON" ] && command -v python3 >/dev/null 2>&1; then
    JSON_PYTHON=$(command -v python3)
  fi
fi
if [ -z "$JSON_PYTHON" ] || [ ! -x "$JSON_PYTHON" ]; then
  echo "python3 JSON validator not found; set KG_HUB_JSON_PYTHON" >&2
  exit 1
fi
if ! "$JSON_PYTHON" -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
if not isinstance(data, dict):
    raise SystemExit("root is not object")
if data.get("BackendState") != "Running":
    raise SystemExit("BackendState is not Running")
if not isinstance(data.get("Peer"), dict):
    raise SystemExit("Peer is not object")
' "$TMP"; then
  echo "tailscale status --json failed JSON/schema validation; last good snapshot retained" >&2
  exit 1
fi

chmod 0640 "$TMP"
mv -f "$TMP" "$OUT"
trap - EXIT HUP INT TERM
