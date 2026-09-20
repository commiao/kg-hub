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
# webhook**。直接提交等于把机密写进仓库。所以路径用占位符、机密用 `@VAR@` 占位，
# 安装时才渲染：
#
#   __CODE__     发布产物目录（`~/.local/share/kg-hub/current`）—— 生产的代码来源
#   __VENV__     解释器环境，在产物之外（它是环境不是代码，不进 git archive）
#   __GITREPO__  开发工作树。**只有漂移巡检用得上**，而且是作为输入数据：
#                它的职责就是比对 git 仓库，而发布产物里没有 .git
#   __HOME__     $HOME
#
# 2026-09-20 之前这里是 `__REPO__` 一路渲染成开发工作树 —— 那等于「装好了」就是
# 「把生产指回开发目录」，分支因此不可用（准则 20）。
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
# 生产跑的是**发布产物**，不是这棵开发工作树（准则 20）。
# 模板里 __CODE__ 渲染成产物目录、__VENV__ 渲染成产物外的解释器环境；
# 只有 __GITREPO__ 仍指工作树 —— 那是漂移巡检的**输入数据**（它要比对 git，
# 而产物里没有 .git），不是任何人的代码来源。
CODE="${KG_HUB_CODE_ROOT:-$HOME/.local/share/kg-hub/current}"
VENV="${KG_HUB_VENV:-$HOME/.local/share/kg-hub/venv}"
# **不能用 `$REPO`（install.sh 自己在哪个检出里）当默认值。**
# 2026-09-20：`$REPO` 在主工作树里恰好等于机器上该装的那个值，所以一直没人发现。
# 换个 worktree 就不等了 —— 而现在的纪律恰恰是「在独立 worktree 上检出 origin/main、
# 在那里跑全量再发布」。后果有两层：
#   1. `--check` 在任何非主工作树里必红，而那个红与机器状态无关，正好淹掉它本该抓的真漂移
#   2. **更要紧**：谁要是从一个临时发布 worktree 跑了一次 install.sh，漂移巡检就被
#      永久指向那个临时目录 —— 而它随后会被删掉。失效方式是安静的：巡检照跑、照报绿。
#
# 漂移巡检要比对的是**这台机器约定的那棵 git 工作树**，不是「谁碰巧执行了安装」。
# `--git-common-dir` 在 linked worktree 里返回的是主工作树的 .git，正好是这个语义。
# 不是 git 检出时（比如从 tar 解出来跑）回落到 ${REPO} ，保持原行为。
GITREPO="${KG_HUB_GIT_REPO:-}"
if [ -z "$GITREPO" ]; then
  common=$(cd "$REPO" && git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [ -n "$common" ]; then GITREPO=$(dirname "$common"); else GITREPO="$REPO"; fi
fi
TARGET="$HOME/Library/LaunchAgents"
# 同上：`.env` 是**这台机器的配置数据**，住在约定的那棵工作树里。
# 用 $REPO 的话，从任何 worktree 跑 --check 都会报「缺机密」—— 而机器上其实配好了。
# 2026-09-20 实测：那正是 capsule-watch 那一格红的原因。
ENV_FILE="${KG_HUB_ENV_FILE:-$GITREPO/.env}"
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
  # 占位替换顺序要紧：替换出来的路径本身含有 ${HOME}，先替 __HOME__ 会把
  # 后续替换出来的路径再替一次。这几个都是整串占位符、互不包含，所以安全；
  # __HOME__ 放最后只是为了万一。
  local file=$1 text
  text=$(sed -e "s|__CODE__|$CODE|g" -e "s|__VENV__|$VENV|g" \
             -e "s|__GITREPO__|$GITREPO|g" -e "s|__HOME__|$HOME|g" "$file")
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
# 不限 com.kg-hub.* 前缀：这台 Mac 上要管的服务不都姓 kg-hub。
# com.claude-mem.worker 就是一例 —— 它的 label 必须保持 claude-mem 自己的
# 那个名字，否则插件将来重装一份同名 job，两个 job 会各起一份 worker。
for template in "$AGENTS"/com.*.plist; do
  label=$(basename "$template" .plist)
  if [ ${#labels[@]} -gt 0 ]; then
    printf '%s\n' "${labels[@]}" | grep -qx "$label" || continue
  fi
  installed="$TARGET/$label.plist"

  if ! rendered=$(render "$template"); then rc=1; continue; fi

  if [ "$mode" = check ]; then
    if [ ! -f "$installed" ]; then
      say "  ✗ ${label}：仓库里有，机器上没装"; rc=1
    elif printf '%s\n' "$rendered" | diff -q - "$installed" >/dev/null 2>&1; then
      say "  ✓ $label"
    else
      # 手改过 plist 而没回写仓库 —— 这正是「各种版本互相冲突」的起点。
      say "  ✗ ${label}：机器上的与仓库里的不一致"
      printf '%s\n' "$rendered" | diff -u "$installed" - | sed -n '3,12p' >&2
      rc=1
    fi
    continue
  fi

  mkdir -p "$TARGET"
  tmp=$(mktemp "$TARGET/.$label.XXXXXX")
  printf '%s\n' "$rendered" > "$tmp"
  chmod 600 "$tmp"
  plutil -lint "$tmp" >/dev/null || { say "  ✗ ${label}：渲染出的 plist 不合法"; rm -f "$tmp"; rc=1; continue; }
  mv -f "$tmp" "$installed"
  # bootout 再 bootstrap 才会重读文件；单纯 kickstart 用的还是旧定义。
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$installed" 2>/dev/null \
    || { say "  ✗ ${label}：bootstrap 失败"; rc=1; continue; }
  say "  ✓ $label 已安装并重载"
done

[ "$mode" = check ] && [ $rc -eq 0 ] && say "机器上的服务定义与仓库一致"
exit $rc
