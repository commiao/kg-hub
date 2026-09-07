#!/bin/sh
# Targeted NAS deployment; preserve the running image and exact source backups.
set -eu
src=/volume1/docker/kg-hub-src
stage=/volume1/docker/kg-hub-src/.dashboard-health.MCdbrv
dk() { sudo -n /var/packages/ContainerManager/target/usr/bin/docker "$@"; }
cd "$src"
test "$(sha256sum topology.py | cut -d ' ' -f 1)" = cd48d88bb114915cc055b18c6c2b193e96b091d77d0ca8c960bfbe953b95cd96
test "$(sha256sum kg_hub_server.py | cut -d ' ' -f 1)" = 2bc94fca4d2a5841e448ff8ed614413182fec85e2398692fc2ac94c03eb44c3a
test ! -e dashboard_status.py
old_image=$(dk inspect kg-hub-server --format '{{.Image}}')
test "$old_image" = sha256:6446eb98373f292cc125ee13cdda32fa0e2b8f536767904a271798dd8fb18f80
dk tag "$old_image" kg-hub-server:dashboard-health-base-20260907
dk build --network none -t kg-hub-server:dashboard-health-20260907 \
  -f "$stage/deploy/dashboard-health.Dockerfile" "$stage"
backup=$(mktemp -d "$src/.dashboard-health-backup.XXXXXX")
cp -p topology.py kg_hub_server.py "$backup/"
printf 'services:\n  kg_hub_server:\n    image: kg-hub-server:dashboard-health-base-20260907\n' > "$backup/rollback.yml"
compose() {
  dk compose --env-file deploy/nas/.env -f docker-compose.yml \
    -f deploy/model-gateway-network.override.yml -f "$1" -p kg-hub \
    up -d --no-deps --no-build --timeout 120 kg_hub_server
}
rollback() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    cp -p "$backup/topology.py" "$backup/kg_hub_server.py" "$src/"
    compose "$backup/rollback.yml" || true
    printf 'DEPLOY_FAILED backup=%s\n' "$backup"
  fi
  exit "$rc"
}
trap rollback EXIT
for name in topology.py kg_hub_server.py dashboard_status.py; do
  cp "$stage/$name" "$src/.$name.dashboard-health.tmp"
  mv "$src/.$name.dashboard-health.tmp" "$src/$name"
done
cp "$stage/deploy/dashboard-health.override.yml" deploy/dashboard-health.override.yml
compose "$src/deploy/dashboard-health.override.yml"
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS --max-time 2 http://127.0.0.1:17171/health >/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
test "$ready" = 1
trap - EXIT
printf 'DEPLOY_OK backup=%s\n' "$backup"
dk inspect kg-hub-server --format '{{.State.Status}} {{.Config.Image}}'
