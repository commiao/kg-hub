#!/bin/sh
# 一次跑完全部测试。存在的理由:2026-08-25 审计发现两类"假通过"——
#   ① pytest 风格文件无 runner → 直接 python3 跑只定义函数、零断言执行、exit 0
#   ② 缺项目根目录 import 路径 → ModuleNotFoundError,但没人跑过所以没人知道
# 两类都是"看起来有测试保护,其实没有"。这个脚本让"跑没跑、过没过"一目了然。
#
# 用项目 venv(graphiti_core 等依赖在里面);没有则退回系统 python3。
cd "$(dirname "$0")/.." || exit 1
# Each file is launched as a script, so Python otherwise puts tests/ rather
# than the repository root on sys.path. Keep the project modules importable.
PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
# 测试只认自己 patch.dict 设的值，不继承宿主的模型客户端与服务配置。2026-10-08 实测：
# 在 Claude Code 里跑，进程自带 ANTHROPIC_BASE_URL，网关客户端改连那个地址，
# 5 个文件红得像付费重试逻辑坏了，而这些提交在干净环境里全绿。
for name in $(env | awk -F= '/^(ANTHROPIC|KG_HUB)_[A-Za-z0-9_]*=/ {print $1}'); do
    unset "$name" 2>/dev/null
done
PY=spike-graphiti/.venv/bin/python
[ -x "$PY" ] || PY=python3
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
