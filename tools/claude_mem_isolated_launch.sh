#!/bin/sh
# Opt-in isolated capture runtime. No legacy process is stopped or signalled.
set -eu
DATA="$HOME/.claude-mem-next"
BUNDLE="$HOME/.local/share/claude-mem/current/plugin/scripts/worker-service.cjs"
[ -f "$DATA/settings.json" ] && [ -f "$DATA/claude-mem.db" ] && [ -f "$BUNDLE" ] || {
  echo 'isolated capture has not been prepared' >&2; exit 1;
}
PORT=$(/usr/bin/python3 - "$DATA" <<'PY'
import json, pathlib, sqlite3, sys
p=pathlib.Path(sys.argv[1]);s=json.loads((p/'settings.json').read_text())
if s.get('CLAUDE_MEM_DATA_DIR')!=str(p):raise SystemExit('data directory mismatch')
port=int(s['CLAUDE_MEM_WORKER_PORT'])
if port==37701 or not 1024<=port<=65535:raise SystemExit('isolated port required')
c=sqlite3.connect((p/'claude-mem.db').as_uri()+'?mode=ro',uri=True)
if c.execute('SELECT count(*) FROM capture_generation').fetchone()[0]!=1:raise SystemExit('missing generation')
c.close();print(port)
PY
)
while /usr/bin/nc -z -G 1 127.0.0.1 "$PORT" >/dev/null 2>&1; do sleep 5; done
export CLAUDE_MEM_DATA_DIR="$DATA"
export CLAUDE_MEM_WORKER_PORT="$PORT"
export CLAUDE_MEM_MANAGED=true
export DO_NOT_TRACK=1
exec "$HOME/.bun/bin/bun" "$BUNDLE" --daemon
