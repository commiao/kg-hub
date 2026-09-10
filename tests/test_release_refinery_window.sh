#!/usr/bin/env bash
# Shell-level safety tests for the optional refinery-window transaction.  They
# source release.sh and replace on_nas with a local command runner, so no SSH,
# Docker, NAS files, or real .env values are used.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/kg-hub-release-window.XXXXXX")
trap 'rm -rf "$FIXTURE"' EXIT

export KG_HUB_NAS_SRC="$FIXTURE"
source "$ROOT/deploy/nas/release.sh"
DRY_RUN=0
say() { :; }

# Every helper under test emits a remote shell program through on_nas.  This
# mock executes it locally against the disposable fixture; it never invokes
# ssh.  A test can replace it again to inspect ordering around compose.
on_nas() { /bin/bash -c "$1"; }

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
assert_eq() { [ "$1" = "$2" ] || fail "$3"; }
assert_file_unchanged() { cmp -s "$1" "$2" || fail "$3"; }
end_hour() { sed -n 's/^KG_HUB_REFINERY_WINDOW_END=//p' "$FIXTURE/.env"; }
start_hour() { sed -n 's/^KG_HUB_REFINERY_WINDOW_START=//p' "$FIXTURE/.env"; }
backup_count() { find "$FIXTURE" -maxdepth 1 -name '.release-window-transaction' -type d | wc -l | tr -d ' '; }
reset_state() {
  window_change_requested=0
  lock_acquired=0
}
write_env() {
  printf '%s\n' \
    'KG_HUB_IMAGE_TAG=old-image' \
    'KG_HUB_REFINERY_WINDOW_START=22' \
    'KG_HUB_REFINERY_WINDOW_END=10' \
    'KG_HUB_REFINERY_MAX_DISK_TEMP=52' \
    'KG_HUB_REFINERY_INGEST_CONCURRENCY=2' \
    'UNRELATED_SECRET=fixture-only' > "$FIXTURE/.env"
  chmod 600 "$FIXTURE/.env"
}

test_rejects_all_other_windows() {
  write_env
  cp "$FIXTURE/.env" "$FIXTURE/before"
  reset_state
  if (main --refinery-window-22-09 >/dev/null 2>&1); then
    fail 'an unrecognised refinery window was accepted'
  fi
  assert_file_unchanged "$FIXTURE/before" "$FIXTURE/.env" 'unrecognised window changed .env'

  # The accepted flag is still exact: a drifted live end hour is rejected.
  sed -i.bak 's/^KG_HUB_REFINERY_WINDOW_END=10$/KG_HUB_REFINERY_WINDOW_END=9/' "$FIXTURE/.env"
  rm -f "$FIXTURE/.env.bak"
  if (window_change_requested=1; prepare_refinery_window_change >/dev/null 2>&1); then
    fail 'a live 22-09 window was accepted'
  fi
  assert_eq "9" "$(end_hour)" 'rejected live window was modified'
  assert_eq "0" "$(backup_count)" 'rejected live window left a backup'
}

test_build_failure_restores_complete_env() {
  write_env
  cp "$FIXTURE/.env" "$FIXTURE/before"
  reset_state
  window_change_requested=1
  prepare_refinery_window_change
  assert_eq "8" "$(end_hour)" 'prepare did not set END=8'

  # A build failure reaches the unified EXIT handler before any image swap.
  # Run it in a subshell because the handler intentionally exits with failure.
  if (set +e; false; release_exit); then
    fail 'the simulated build failure returned success'
  fi
  assert_file_unchanged "$FIXTURE/before" "$FIXTURE/.env" 'build-failure restoration did not restore full .env'
  assert_eq "0" "$(backup_count)" 'restored backup was not cleaned up'
}

test_interruption_restores_complete_env() {
  write_env
  cp "$FIXTURE/.env" "$FIXTURE/before"
  reset_state
  window_change_requested=1

  # The real release installs this pair after it owns the NAS lock.  A TERM
  # from its caller must take the same EXIT path as an ordinary build failure.
  if (
    export KG_HUB_NAS_SRC="$FIXTURE"
    export RELEASE_WINDOW_SCRIPT="$ROOT/deploy/nas/release.sh"
    /bin/bash -c '
      set -euo pipefail
      source "$RELEASE_WINDOW_SCRIPT"
      DRY_RUN=0
      say() { :; }
      on_nas() { /bin/bash -c "$1"; }
      window_change_requested=1
      lock_acquired=0
      prepare_refinery_window_change
      install_release_exit_traps
      kill -TERM "$$"
      sleep 1
    '
  ); then
    fail 'an interrupted release returned success'
  fi
  assert_file_unchanged "$FIXTURE/before" "$FIXTURE/.env" 'interrupt restoration did not restore full .env'
  assert_eq "0" "$(backup_count)" 'interrupt restoration retained transaction state'
}

test_repeated_interrupt_does_not_skip_cleanup() {
  write_env
  cp "$FIXTURE/.env" "$FIXTURE/before"
  reset_state

  if (
    export KG_HUB_NAS_SRC="$FIXTURE"
    export RELEASE_WINDOW_SCRIPT="$ROOT/deploy/nas/release.sh"
    /bin/bash -c '
      set -euo pipefail
      source "$RELEASE_WINDOW_SCRIPT"
      DRY_RUN=0
      say() { :; }
      on_nas() { /bin/bash -c "$1"; }
      window_change_requested=1
      lock_acquired=0
      prepare_refinery_window_change
      # Stretch the cleanup command so a second TERM lands while release_exit
      # is restoring the transaction, rather than after it has finished.
      on_nas() { sleep 2; /bin/bash -c "$1"; }
      install_release_exit_traps
      (sleep 1; kill -TERM "$$") &
      kill -TERM "$$"
      sleep 3
    '
  ); then
    fail 'a repeatedly interrupted release returned success'
  fi
  assert_file_unchanged "$FIXTURE/before" "$FIXTURE/.env" 'repeated interrupt skipped full .env restoration'
  assert_eq "0" "$(backup_count)" 'repeated interrupt retained transaction state'
}

test_lost_prepare_reply_recovers_from_remote_transaction_record() {
  write_env
  cp "$FIXTURE/.env" "$FIXTURE/before"
  reset_state
  window_change_requested=1

  # 模拟远端已备份、写 active 记录并原子替换 .env，但 SSH 在回传结果前断开。
  # prepare 因返回失败不会有任何可用的本地备份变量；失败出口必须重新连接、只靠
  # NAS 固定记录找到备份并恢复。
  lost_reply=1
  on_nas() {
    if [ "$lost_reply" = 1 ] && [[ "$1" == *"record_tmp="* ]]; then
      lost_reply=0
      /bin/bash -c "$1"
      return 255
    fi
    /bin/bash -c "$1"
  }
  if (prepare_refinery_window_change >/dev/null 2>&1); then
    fail 'a lost SSH reply was treated as success'
  fi
  assert_eq "8" "$(end_hour)" 'the simulated remote commit did not happen'
  assert_eq "1" "$(backup_count)" 'the remote transaction record was not kept'

  on_nas() { /bin/bash -c "$1"; }
  if (set +e; false; release_exit); then
    fail 'the simulated failure after a lost SSH reply returned success'
  fi
  assert_file_unchanged "$FIXTURE/before" "$FIXTURE/.env" 'lost-reply recovery did not restore complete .env'
  assert_eq "0" "$(backup_count)" 'lost-reply recovery did not clear the transaction record'
}

test_health_failure_restores_env_before_old_image_compose() {
  write_env
  reset_state
  window_change_requested=1
  prepare_refinery_window_change
  PREV=old-image

  on_nas() {
    if [[ "$1" == *'compose -p '* ]]; then
      assert_eq "10" "$(end_hour)" 'old-image compose was reached before .env restoration'
      return 0
    fi
    /bin/bash -c "$1"
  }
  rollback_to_previous_image 'simulated health failure'
  assert_eq "10" "$(end_hour)" 'health-failure rollback did not leave the old window'
  assert_eq "0" "$(backup_count)" 'health-failure rollback retained an unnecessary backup'
  on_nas() { /bin/bash -c "$1"; }
}

test_candidate_compose_failure_main_path_restores_before_old_image() {
  write_env
  cp "$FIXTURE/.env" "$FIXTURE/before"
  reset_state
  DK=:
  SERVICES='kg_hub_server device_liveness watchdog ingester refinery'
  sleep() { :; }
  on_nas() { ssh "$@"; }
  ssh() {
    local command="${!#}"
    if [[ "$command" == *"curl -fsS -m 5"* ]]; then
      printf '%s\n' '{"active_extractions": 0}'
      return 0
    fi
    if [[ "$command" == *"KG_HUB_IMAGE_TAG_PREV="* && "$command" == *"compose -p"* ]]; then
      : > "$FIXTURE/candidate-compose"
      assert_eq "8" "$(end_hour)" 'candidate compose did not receive the requested window'
      return 1
    fi
    if [[ "$command" == *"grep -v '^KG_HUB_IMAGE_TAG='"* && "$command" == *"compose -p"* ]]; then
      : > "$FIXTURE/rollback-compose"
      assert_eq "10" "$(end_hour)" 'old-image compose started before .env restoration'
      assert_file_unchanged "$FIXTURE/before" "$FIXTURE/.env" 'old-image compose did not receive the complete original .env'
      /bin/bash -c "$command"
      return 0
    fi
    /bin/bash -c "$command"
  }

  # main 包含真实发布分支，但 git archive 和所有“远端”命令都只落在本地 fixture。
  # 候选 compose 被精确注入失败，验证实际失败分支（而不是直接调用回滚辅助函数）。
  # `HEAD` may be this unpushed test commit.  The real release path rightly
  # rejects that before any NAS operation, so use the tracked origin/main
  # ancestor and reach the candidate-compose failure injection below.
  if (main --refinery-window-22-08 origin/main >/dev/null 2>&1); then
    fail 'candidate compose failure main path returned success'
  fi
  [ -f "$FIXTURE/candidate-compose" ] || fail 'candidate compose failure was not exercised'
  [ -f "$FIXTURE/rollback-compose" ] || fail 'main path did not start the old image'
  assert_eq "10" "$(end_hour)" 'candidate failure main path did not leave the old window'
  assert_eq "0" "$(backup_count)" 'candidate failure main path retained transaction state'
  unset -f ssh
  unset -f sleep
  on_nas() { /bin/bash -c "$1"; }
  DK='sudo -n /var/packages/ContainerManager/target/usr/bin/docker'
}

test_success_keeps_only_end_8_and_discards_backup() {
  write_env
  reset_state
  window_change_requested=1
  prepare_refinery_window_change
  assert_eq "22" "$(start_hour)" 'start hour changed'
  assert_eq "8" "$(end_hour)" 'end hour was not changed to 8'
  grep -qx 'KG_HUB_REFINERY_MAX_DISK_TEMP=52' "$FIXTURE/.env" || fail 'temperature breaker changed'
  grep -qx 'KG_HUB_REFINERY_INGEST_CONCURRENCY=2' "$FIXTURE/.env" || fail 'concurrency changed'
  grep -qx 'UNRELATED_SECRET=fixture-only' "$FIXTURE/.env" || fail 'an unrelated key changed'
  assert_eq "600" "$(stat -f '%Lp' "$FIXTURE/.env")" '.env mode is not 0600'
  discard_refinery_window_backup
  assert_eq "0" "$(backup_count)" 'successful transaction retained backup'
}

test_without_flag_does_not_touch_window_env() {
  write_env
  cp "$FIXTURE/.env" "$FIXTURE/before"
  reset_state
  prepare_refinery_window_change
  assert_file_unchanged "$FIXTURE/before" "$FIXTURE/.env" 'no-flag path changed .env'
  assert_eq "0" "$(backup_count)" 'no-flag path created backup'
}

test_rejects_all_other_windows
test_build_failure_restores_complete_env
test_interruption_restores_complete_env
test_repeated_interrupt_does_not_skip_cleanup
test_lost_prepare_reply_recovers_from_remote_transaction_record
test_health_failure_restores_env_before_old_image_compose
test_candidate_compose_failure_main_path_restores_before_old_image
test_success_keeps_only_end_8_and_discards_backup
test_without_flag_does_not_touch_window_env
printf 'release refinery window safety tests: ok\n'
