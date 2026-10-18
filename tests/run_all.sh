#!/bin/sh
# 一次跑完全部测试。存在的理由:2026-08-25 审计发现两类"假通过"——
#   ① pytest 风格文件无 runner → 直接 python3 跑只定义函数、零断言执行、exit 0
#   ② 缺项目根目录 import 路径 → ModuleNotFoundError,但没人跑过所以没人知道
# 两类都是"看起来有测试保护,其实没有"。这个脚本让"跑没跑、过没过"一目了然。
#
# 解释器按下面的顺序取第一个存在的，然后**先验证它能导入 graphiti_core**，
# 导入不了就停（退出 125），一条用例都不跑：
#   KG_HUB_TEST_PYTHON                      显式指定
#   spike-graphiti/.venv/bin/python         仓库内 venv（发布执行器按 runtime_links 链进检出的就是它）
#   $KG_HUB_VENV/bin/python                 Mac 作业实际用的 venv；默认值与 deploy/mac/install.sh 同源
#   python3                                 都没有时才用系统的
# 选中的那个存在却导入不了，不往下一个找：坏掉的 venv 要被看见，不能被下一个候选盖住。
#
# 原来的写法是「venv 不在就静默退回 python3」。2026-10-18 实测：本机仓库内 venv 已经
# 不存在，退回的系统 python3 没有 graphiti_core，125 个文件红 18 个；换成有依赖的
# 解释器后只剩 2 个，也就是 16 个是环境造成的，真正的 2 条红埋在里面。退出码就是
# 失败文件数，发布门读的正是它，而它分不出哪些是环境、哪些是代码。
cd "$(dirname "$0")/.." || exit 1
# Each file is launched as a script, so Python otherwise puts tests/ rather
# than the repository root on sys.path. Keep the project modules importable.
PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
# 这两个值必须在下面清掉 KG_HUB_* 之前读，否则它们会被一起清掉、永远不生效。
TEST_PY="${KG_HUB_TEST_PYTHON:-}"
MAC_VENV="${KG_HUB_VENV:-$HOME/.local/share/kg-hub/venv}"
# 测试只认自己 patch.dict 设的值，不继承宿主的模型客户端与服务配置。2026-10-08 实测：
# 在 Claude Code 里跑，进程自带 ANTHROPIC_BASE_URL，网关客户端改连那个地址，
# 5 个文件红得像付费重试逻辑坏了，而这些提交在干净环境里全绿。
for name in $(env | awk -F= '/^(ANTHROPIC|KG_HUB)_[A-Za-z0-9_]*=/ {print $1}'); do
    unset "$name" 2>/dev/null
done
if [ -n "$TEST_PY" ]; then
    PY=$TEST_PY
elif [ -x spike-graphiti/.venv/bin/python ]; then
    PY=spike-graphiti/.venv/bin/python
elif [ -x "$MAC_VENV/bin/python" ]; then
    PY=$MAC_VENV/bin/python
else
    PY=python3
fi
if ! probe=$("$PY" -c 'import graphiti_core' 2>&1); then
    echo "❌ 解释器 $PY 导入不了 graphiti_core，未运行任何用例"
    printf '%s\n' "$probe" | tail -1 | sed 's/^/   /'
    echo "   查过：KG_HUB_TEST_PYTHON、spike-graphiti/.venv、${MAC_VENV}、python3"
    echo "   指定解释器：KG_HUB_TEST_PYTHON=/path/to/python sh tests/run_all.sh"
    exit 125
fi
echo "解释器: $PY"
fail=0
for f in tests/test_*.py; do
    out=$("$PY" "$f" 2>&1)
    rc=$?
    last=$(printf '%s' "$out" | tail -1)
    if [ $rc -eq 0 ]; then
        printf '  ✅ %-38s %s\n' "$(basename "$f")" "$last"
    else
        printf '  ❌ %-38s %s\n' "$(basename "$f")" "$last"
        fail=$((fail + 1))
    fi
done
echo ""
[ $fail -eq 0 ] && echo "全部通过" || echo "$fail 个文件失败"
exit $fail
