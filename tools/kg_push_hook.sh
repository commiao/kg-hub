#!/bin/sh
# kg-hub PUSH hook 的启动包装：各工具的 hook 配置统一调它，不直接调 kg_push_hook.py。
#
# ## 为什么需要它（2026-10-14 实测）
#
# Claude Code 的 SessionStart hook 写的是
#   ~/work-ai/kg-hub/spike-graphiti/.venv/bin/python ~/work-ai/kg-hub/tools/kg_push_hook.py
# 坏了两层：
#   1. 那个 venv 已经不存在，hook 一启动就失败。
#   2. 更早就坏了：hook 进程拿不到 KG_HUB_URL，回落到已退役的本机 127.0.0.1:8080。
#      data/.push_hook.log 里 10-09 起 54 次会话启动全是 Connection refused，一条都没注入。
# hook 失败时会话照常开，所以没人发现。
#
# ## 做法：与 muxcp 的 run-kg-hub.sh 一致
#
#   env    加载 ~/.claude-mem/.env：真正的 KG_HUB_URL（NAS 17171）和 token 在那里。
#   代码   ~/.local/share/kg-hub/current/tools/kg_push_hook.py —— 发布产物，不跑开发工作树。
#   解释器 ~/.local/share/kg-hub/venv/bin/python。
#   日志   仍写 ~/work-ai/kg-hub/data/.push_hook.log：发布目录不可变，不能往里写；
#          hook_inventory / push_status / verify_tool_links 都从这个位置读。
#
# 产物缺了就报错退出（非 0），不回退到工作树，也不静默：静默正是这次没人发现的原因。
set -eu

KG_HUB_ROOT="${KG_HUB_ROOT:-${HOME}/.local/share/kg-hub}"
VENV_PY="${KG_HUB_ROOT}/venv/bin/python"
HOOK="${KG_HUB_ROOT}/current/tools/kg_push_hook.py"
LOCAL_ENV="${KG_HUB_PUSH_ENV_FILE:-${HOME}/.claude-mem/.env}"

if [ -f "${LOCAL_ENV}" ]; then
    set -a
    # shellcheck disable=SC1090
    . "${LOCAL_ENV}"
    set +a
fi

if [ ! -x "${VENV_PY}" ] || [ ! -f "${HOOK}" ]; then
    echo "kg-hub PUSH hook：发布产物不完整，需要 ${VENV_PY} 与 ${HOOK}；不回退到工作树。" >&2
    exit 1
fi

KG_HUB_PUSH_HOOK_LOG="${KG_HUB_PUSH_HOOK_LOG:-${HOME}/work-ai/kg-hub/data/.push_hook.log}"
export KG_HUB_PUSH_HOOK_LOG

exec "${VENV_PY}" "${HOOK}" "$@"
