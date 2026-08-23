"""probe_sync 判据的边界测试：什么该报、什么不该报。

这套判据的价值全在边界上 —— 阈值落错位置就会把健康态报成故障(2026-08-23
落差 69 的误报)，或者把真故障放过(8-21 那次 66 分钟冻结)。
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tools.capture_probe as P

def judge(lag, age_s):
    """复刻 probe_sync 的判定段，喂合成输入。"""
    if lag <= 0: return P.GREEN
    if age_s > P.SYNC_STALL_RED_S: return P.RED
    if age_s > P.SYNC_STALL_AMBER_S: return P.AMBER
    return P.GREEN

M = 60
CASES = [
    # (落差, 距上次成功同步, 期望, 说明)
    (79,  14*M, P.GREEN, "本次误报的实况：14 分钟前刚同步成功，79 条是本周期正常累积"),
    (56,   5*M, P.GREEN, "实测最忙的单周期新增 56 条，刚同步过 → 健康"),
    (52,   9*M, P.GREEN, "52 条曾经会触发旧的 50 条红线 → 现在不报"),
    (0,   10*3600, P.GREEN, "笔记本睡了一夜：落差 0 就是健康，与多久没同步无关"),
    (3,   30*M, P.AMBER, "跳过 1 个周期且仍有落差 → 留意"),
    (41,  66*M, P.RED,   "8-21 那次真故障：66 分钟没成功同步且有落差"),
    (106, 90*M, P.RED,   "用户 8-21 报的落差 106 + 长时间没同步 → 故障"),
    (1,   46*M, P.RED,   "只差 1 条但 46 分钟没同步成功 → 仍是故障(时间才是判据)"),
    (600,  2*M, P.GREEN, "刚同步成功，积压 600 条是追赶中，不是故障"),
]
ok = fail = 0
for lag, age, want, why in CASES:
    got = judge(lag, age)
    mark = "✅" if got == want else "❌"
    if got == want: ok += 1
    else: fail += 1
    print(f"  {mark} 落差{lag:>4} / {age//60:>3}分钟未同步 → {got:<5} (期望 {want:<5}) {why}")
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
