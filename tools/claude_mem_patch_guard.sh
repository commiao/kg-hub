#!/bin/sh
# claude-mem worker 补丁守护 —— 插件升级会把补丁还原，这里把它按回去。
#
# ## 为什么需要
#
# T-0077 B 项的补丁装在第三方插件的构建产物里（worker-service.cjs）。
# `claude plugin update` 会覆盖它，而覆盖之后**什么都不会报错**：worker 照常
# 跑、采集照常走，只是每次链路抖动又开始重复付费。这类故障没有声音。
#
# ## 绝不盲目还原
#
# 最危险的不是补丁掉了，是**把旧版本盖回新版本**。所以还原的前提是目标安装的
# package.json 版本仍然等于清单里的版本。版本变了 = 插件真升级了，这时：
#   - 一个字节都不写
#   - 出声：补丁已失效，要在新版本上重做
#
# 沉默地降级比丢补丁糟得多。
#
# ## 三态，和断路器同一套思路
#
#   目标缺失/版本对不上   → 不动，出声（按天去重）
#   已是补丁版           → 不动，静默（绝大多数轮次）
#   版本对上但内容不对    → 还原，出声
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
MANIFEST="$SCRIPT_DIR/claude_mem_patch.manifest"
LOG=${CLAUDE_MEM_PATCH_LOG:-"$HOME/.kg-hub/logs/claude-mem-guard.log"}
STATE_DIR=${CLAUDE_MEM_PATCH_STATE:-"$HOME/.kg-hub/state"}
ts() { date '+%F %T'; }
note() { mkdir -p "$(dirname "$LOG")" 2>/dev/null; echo "$(ts) [补丁守护] $*" >> "$LOG"; }

# 按天去重的告警：同一个问题一天只喊一次，免得刷屏，也免得烂八天没人看见。
alert_once() {
  key=$1; text=$2
  mkdir -p "$STATE_DIR" 2>/dev/null
  mark="$STATE_DIR/patch-$key-$(date '+%Y%m%d')"
  [ -f "$mark" ] && return 0
  : > "$mark" 2>/dev/null
  [ -n "${WEBHOOK:-}" ] || return 0
  curl -s -m 10 -X POST "$WEBHOOK" -H 'Content-Type: application/json' \
    -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$text\"}}" >/dev/null 2>&1
}

[ -f "$MANIFEST" ] || { note "清单不存在：$MANIFEST"; exit 0; }
want_version=$(sed -n 's/^version=//p' "$MANIFEST" | head -1)
want_sha=$(sed -n 's/^sha256=//p' "$MANIFEST" | head -1)
source_rel=$(sed -n 's/^source=//p' "$MANIFEST" | head -1)
[ -n "$want_version" ] && [ -n "$want_sha" ] && [ -n "$source_rel" ] || {
  note "清单缺字段，什么都不做"; exit 0; }

SOURCE="$REPO/$source_rel"
if [ ! -f "$SOURCE" ]; then
  # 有清单、没源 —— 还原能力不存在。这是故障，不是静默档：不出声的话，
  # 下一次插件升级会把补丁抹掉而没有任何人知道。
  note "补丁源不存在：$SOURCE（升级一旦发生就无法还原）"
  alert_once missing-source "🔴 kg-hub：claude-mem 补丁源不在（$SOURCE）。插件一升级补丁就没了，且不会有任何报错。"
  exit 0
fi
have_sha=$(shasum -a 256 "$SOURCE" 2>/dev/null | cut -d' ' -f1)
if [ "$have_sha" != "$want_sha" ]; then
  note "补丁源与清单指纹不符（源 ${have_sha:-空} / 清单 $want_sha），拒绝用它还原"
  alert_once source-drift "🔴 kg-hub：claude-mem 补丁源与清单指纹不符，守护已停手。重新构建后请同步更新 tools/claude_mem_patch.manifest。"
  exit 0
fi

restored=0
sed -n 's/^target=//p' "$MANIFEST" | while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  root="$HOME/$rel"
  bundle="$root/scripts/worker-service.cjs"
  pkg="$root/package.json"
  [ -f "$bundle" ] || { note "目标不存在，跳过：$bundle"; continue; }

  got_version=$(/usr/bin/python3 -c "
import json,sys
try:
    print(json.load(open('$pkg'))['version'])
except Exception:
    sys.exit(1)" 2>/dev/null)
  if [ -z "$got_version" ]; then
    note "读不出版本，跳过（绝不在不知道版本的情况下写）：$pkg"
    continue
  fi
  if [ "$got_version" != "$want_version" ]; then
    # 这才是最该出声的一档：插件升级了，补丁已经不适用于新版本。
    # 把旧 bundle 盖回去会静默降级 —— 比丢补丁糟得多。
    note "版本已变（$got_version ≠ $want_version），不还原：$bundle"
    alert_once version-moved "⚠️ kg-hub：claude-mem 已升级到 $got_version，T-0077 的幂等补丁（基于 $want_version）不再适用，**未**还原。重复付费的防护当前失效，需要在新版本上重做补丁。"
    continue
  fi

  now_sha=$(shasum -a 256 "$bundle" 2>/dev/null | cut -d' ' -f1)
  [ "$now_sha" = "$want_sha" ] && continue      # 绝大多数轮次走到这里，静默

  if cp "$SOURCE" "$bundle" 2>/dev/null; then
    note "补丁被覆盖，已还原：$bundle（原 ${now_sha:-空}）"
    alert_once restored "🔧 kg-hub：claude-mem worker 补丁曾被覆盖（多半是插件升级），已自动还原。worker 需重启才生效。"
  else
    note "还原失败（写不进去）：$bundle"
    alert_once restore-failed "🔴 kg-hub：claude-mem worker 补丁被覆盖且还原失败（$bundle 写不进去）。"
  fi
done
exit 0
