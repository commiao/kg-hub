"""probe_sync 判据的边界测试：什么该报、什么不该报。

这套判据的价值全在边界上 —— 阈值落错位置就会把健康态报成故障，或者把真故障
放过。三轮踩坑史(每轮都是"用代理量替代真量"的同一个错):
  1. 落差条数判红(50 条) → 正常单周期就 49~56 条，阈值落在正常范围里
  2. 距上次成功同步判红 → 空闲一夜 stamp 不动，新 obs 一落地立刻假红(8-24)
  3. 现判据:**最老未同步 obs 等了多久** = 直接测"数据被卡多久"这个量本身

⚠️ 本文件曾经"复刻"判定段来测，于是实现改了测试照样全绿 —— 等于没有保护。
现在直接调用 P.probe_sync 的真实判定路径(注入 metrics 走同一分支)。
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tools.capture_probe as P

M = 60


def judge(lag, backlog_age_s, stamp_age_s=None):
    """跑真实 probe_sync 的判定段：把 nas/local watermark 与积压时长喂进去。

    通过 monkeypatch 让取数返回合成值，判定逻辑本体不复刻。"""
    local_max, nas_max = 1000, 1000 - lag
    orig_sh, orig_connect = P.sh, P.sqlite3.connect

    P.sh = lambda *a, **k: (f"ok@@{nas_max}", None)

    class _Con:
        def execute(self, q, args=()):
            # 只拦 MIN(created_at_epoch)；返回毫秒(与 claude-mem 真实 schema 一致)
            v = None if backlog_age_s is None else (time.time() - backlog_age_s) * 1000.0
            return type("R", (), {"fetchone": lambda s: (v,)})()
        def close(self): pass
    P.sqlite3.connect = lambda *a, **k: _Con()

    class _Stamp:
        exists = staticmethod(lambda: stamp_age_s is not None)
        @staticmethod
        def stat():
            return type("S", (), {"st_mtime": time.time() - (stamp_age_s or 0)})()
    orig_stamp, orig_log = P.SYNC_STAMP, P.SYNC_LOG
    P.SYNC_STAMP = _Stamp
    P.SYNC_LOG = type("L", (), {"exists": staticmethod(lambda: False)})
    try:
        return P.probe_sync(local_max, "dummy-host")["state"]
    finally:
        P.sh, P.sqlite3.connect = orig_sh, orig_connect
        P.SYNC_STAMP, P.SYNC_LOG = orig_stamp, orig_log


CASES = [
    # (落差, 积压最老等了多久, 距上次同步, 期望, 说明)
    (0,   None,  10*3600, P.GREEN, "笔记本睡了一夜：落差 0 就是健康，与多久没同步无关"),
    (20,   4*M,  11*3600, P.GREEN, "★8-24 本次误报实况：整夜零 obs(stamp 11h)，新 obs 只等 4 分钟"),
    (79,   6*M,   14*M,   P.GREEN, "8-23 误报实况：79 条是本周期正常累积"),
    (56,   3*M,    5*M,   P.GREEN, "实测最忙单周期 56 条，刚积压 → 健康"),
    (600,  2*M,    2*M,   P.GREEN, "积压 600 条但刚产生 → 追赶中，不是故障"),
    (3,   30*M,   30*M,   P.AMBER, "积压等了 30 分钟(跳过 1 周期) → 留意"),
    (41,  66*M,   66*M,   P.RED,   "8-21 真故障：积压等了 66 分钟"),
    (1,   46*M,   46*M,   P.RED,   "只差 1 条但已等 46 分钟 → 仍是故障(时间才是判据)"),
    (10,  None,   50*M,   P.AMBER, "算不出等待时长(库读不到) → 留意但不告警，不猜"),
]
ok = fail = 0
for lag, bage, sage, want, why in CASES:
    got = judge(lag, bage, sage)
    mark = "✅" if got == want else "❌"
    ok, fail = (ok + 1, fail) if got == want else (ok, fail + 1)
    ba = "—" if bage is None else f"{bage//60}分"
    print(f"  {mark} 落差{lag:>4} / 积压{ba:>4} / 上次同步{sage//60:>4}分前 → {got:<5} (期望 {want:<5}) {why}")
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
