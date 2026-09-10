#!/usr/bin/env bash
# 把 kg-hub 在 Mac 上的常驻/定时服务定义装到 launchd。
#
# ## 为什么这些东西要进仓库
#
# 2026-09-10 盘点发现：这 6 个服务的定义**只活在一台机器的
# ~/Library/LaunchAgents/ 里**，仓库里一个字都没有（`capsule-watch` 连名字都没在
# 代码里出现过）。后果是——谁存在、多久跑一次、带什么参数，全靠口口相传；这台 Mac
# 挂了就得凭记忆重建；改一次调度没人 review 也没有历史。
#
# 脚本本身一直在 git 里，缺的是**服务定义**这一半。这个目录补的就是那一半。
#
# ## 为什么是模板而不是直接放 plist
#
# 装好的 plist 里写死了 `/Users/mac/...`，而且 `capsule-watch` 那份里**明文存着飞书
# webhook**。直接提交等于把机密写进仓库。所以路径用 `__HOME__` / `__REPO__` 占位，
# 机密用 `@VAR@` 占位，安装时才渲染。
#
# ## 用法
#
#   deploy/mac/install.sh            # 渲染并安装全部（会重载服务）
#   deploy/mac/install.sh --check    # 只比对，不改任何东西 —— 用它发现手改漂移
#   deploy/mac/install.sh <label>…   # 只装指定的几个
#
# 机密从 `<repo>/.env`（0600、已 gitignore）或同名环境变量取。取不到就**拒绝安装
# 那一个**并说清缺什么——绝不装一个带着 `@VAR@` 字面量的坏 plist 上去。
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
AGENTS="$REPO/deploy/mac/agents"
TARGET="$HOME/Library/LaunchAgents"
ENV_FILE="${KG_HUB_ENV_FILE:-$REPO/.env}"
DOMAIN="gui/$(id -u)"

mode=install
labels=()
for arg in "$@"; do
  case "$arg" in
    --check) mode=check ;;
    --help|-h) sed -n '1,30p' "$0"; exit 0 ;;
    *) labels+=("$arg") ;;
  esac
done

say() { printf '%s\n' "$*" >&2; }

# 机密：环境变量优先，其次 .env。两边都没有就让调用方自己判断怎么办。
secret() {
  local name=$1 value
  value=${!name:-}
  if [ -z "$value" ] && [ -f "$ENV_FILE" ]; then
    value=$(grep "^$name=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')
  fi
  printf '%s' "$value"
}

render() {
  # 占位替换顺序要紧：__REPO__ 是 __HOME__ 的子路径，先 REPO 再 HOME 会把
  # 已经替换出来的路径再替一次。这里两个都是整串占位符，互不包含，安全。
  local file=$1 text
  text=$(sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" "$file")
  # 机密占位
  local missing=()
  while IFS= read -r name; do
    local value
    value=$(secret "$name")
    if [ -z "$value" ]; then missing+=("$name"); continue; fi
    text=${text//@$name@/$value}
  done < <(grep -o '@[A-Z_][A-Z0-9_]*@' "$file" | tr -d '@' | sort -u)
  if [ ${#missing[@]} -gt 0 ]; then
    say "  ✗ $(basename "$file")：缺机密 ${missing[*]}（放进 $ENV_FILE 或导出同名环境变量）"
    return 1
  fi
  printf '%s\n' "$text"
}

rc=0
shopt -s nullglob
for template in "$AGENTS"/com.kg-hub.*.plist; do
  label=$(basename "$template" .plist)
  if [ ${#labels[@]} -gt 0 ]; then
    printf '%s\n' "${labels[@]}" | grep -qx "$label" || continue
  fi
  installed="$TARGET/$label.plist"

  if ! rendered=$(render "$template"); then rc=1; continue; fi

  if [ "$mode" = check ]; then
    if [ ! -f "$installed" ]; then
      say "  ✗ $label：仓库里有，机器上没装"; rc=1
    elif printf '%s\n' "$rendered" | diff -q - "$installed" >/dev/null 2>&1; then
      say "  ✓ $label"
    else
      # 手改过 plist 而没回写仓库 —— 这正是「各种版本互相冲突」的起点。
      say "  ✗ $label：机器上的与仓库里的不一致"
      printf '%s\n' "$rendered" | diff -u "$installed" - | sed -n '3,12p' >&2
      rc=1
    fi
    continue
  fi

  mkdir -p "$TARGET"
  tmp=$(mktemp "$TARGET/.$label.XXXXXX")
  printf '%s\n' "$rendered" > "$tmp"
  chmod 600 "$tmp"
  plutil -lint "$tmp" >/dev/null || { say "  ✗ $label：渲染出的 plist 不合法"; rm -f "$tmp"; rc=1; continue; }
  mv -f "$tmp" "$installed"
  # bootout 再 bootstrap 才会重读文件；单纯 kickstart 用的还是旧定义。
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$installed" 2>/dev/null \
    || { say "  ✗ $label：bootstrap 失败"; rc=1; continue; }
  say "  ✓ $label 已安装并重载"
done

[ "$mode" = check ] && [ $rc -eq 0 ] && say "机器上的服务定义与仓库一致"
exit $rc
