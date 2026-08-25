"""extraction_failing 判据测试:证明"故障持续期不会假恢复"。

2026-08-25 事故复盘:LLM 供应商 key 到期 → 全部抽取失败 → 零产出 9 小时。
watchdog **报了**(16:01 FIRE / 23:01 FIRE),但每次都很快自己 CLEAR:
  16:01 FIRE(7 errored) → 17:35 CLEAR
  23:01 FIRE(1 errored) → 00:00 CLEAR
根因:recent_errors 判据是 `errored_last_1h > 0`,测的是「**新增**错误」。
故障持续时重试被 409 挡住、不再产生新 error 键 → 新增归零 → 判定恢复。
**故障还在,信号消失了。**

extraction_failing 改测**存量**(errored_total),它只有在数据真正入图或键被
清理后才降 —— 这才是"故障是否结束"的诚实信号。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def judge(err_total: int, err_1h: int, gate: int = 5) -> dict:
    """复刻 watchdog 两条判据的**相对关系**(核心是对比,不是单条实现)。"""
    return {
        "recent_errors": err_1h > 0,
        "extraction_failing": err_total > gate,
    }


ok = fail = 0
def check(name, cond):
    global ok, fail
    print(("  ✅ " if cond else "  ❌ ") + name)
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)


# 事故时间线重放
t0 = judge(err_total=7,   err_1h=7)    # 16:01 首现
t1 = judge(err_total=7,   err_1h=0)    # 17:35 新增归零(重试被409挡)——旧判据在此假恢复
t2 = judge(err_total=209, err_1h=1)    # 23:01 又一批
t3 = judge(err_total=209, err_1h=0)    # 00:00 新增再归零——旧判据又假恢复
t4 = judge(err_total=209, err_1h=0)    # 09:00 故障仍在,零产出 9 小时

check("16:01 首现:两条判据都报", t0["recent_errors"] and t0["extraction_failing"])
check("17:35 新增归零:旧判据**假恢复**(复现事故)", not t1["recent_errors"])
check("17:35 同一时刻:新判据**仍在报**(故障未结束)", t1["extraction_failing"])
check("00:00 旧判据再次假恢复", not t3["recent_errors"])
check("09:00 故障持续 9 小时:新判据全程未松口", t4["extraction_failing"])

# 边界
check("清空 error 键后恢复(真恢复才 clear)", not judge(0, 0)["extraction_failing"])
check("零星失败(3条,如抢锁)不报警,免噪音", not judge(3, 3)["extraction_failing"])
check("阈值可配(gate=2 时 3 条即报)", judge(3, 3, gate=2)["extraction_failing"])

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
