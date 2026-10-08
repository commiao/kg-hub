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


def judge(lag, backlog_age_s, stamp_age_s=None, sync_runs=0):
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
    orig_stamp, orig_log, orig_marker = P.SYNC_STAMP, P.SYNC_LOG, P.DUAL_MARKER
    P.SYNC_STAMP = _Stamp
    # 单源判据：本机若真有双源标记，不能让它混进来。
    P.DUAL_MARKER = Path("/nonexistent/claude-mem-dual-active.json")
    # 同步器在积压窗口内"真正执行了几次"是第四轮判据的核心维度：
    # 墙钟时长在可休眠设备上不代表机会次数（笔记本睡着时任务根本不触发）。
    if sync_runs:
        base = time.time() - (backlog_age_s or 0) + 1
        text = "\n".join(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + i))
            + "  synced (obs MAX id=1000)" for i in range(sync_runs))
        P.SYNC_LOG = type("L", (), {
            "exists": staticmethod(lambda: True),
            "read_text": staticmethod(lambda *a, **k: text)})
    else:
        P.SYNC_LOG = type("L", (), {"exists": staticmethod(lambda: False)})
    try:
        return P.probe_sync(local_max, "dummy-host")["state"]
    finally:
        P.sh, P.sqlite3.connect = orig_sh, orig_connect
        P.SYNC_STAMP, P.SYNC_LOG, P.DUAL_MARKER = orig_stamp, orig_log, orig_marker


CASES = [
    # (落差, 积压最老等了多久, 距上次同步, 期望, 说明)
    (0,   None,  10*3600, P.GREEN, "笔记本睡了一夜：落差 0 就是健康，与多久没同步无关"),
    (20,   4*M,  11*3600, P.GREEN, "★8-24 本次误报实况：整夜零 obs(stamp 11h)，新 obs 只等 4 分钟"),
    (79,   6*M,   14*M,   P.GREEN, "8-23 误报实况：79 条是本周期正常累积"),
    (56,   3*M,    5*M,   P.GREEN, "实测最忙单周期 56 条，刚积压 → 健康"),
    (600,  2*M,    2*M,   P.GREEN, "积压 600 条但刚产生 → 追赶中，不是故障"),
    (3,   30*M,   30*M,   P.AMBER, "积压等了 30 分钟(跳过 1 周期) → 留意"),
    # ↓ 第四轮判据（2026-09-03）：RED 要求同步器**真的跑过 ≥3 次仍未追平**。
    #   此前这两例只按墙钟时长判红，而墙钟在可休眠设备上会把"机器睡着"读成"同步失败"。
    (41,  66*M,   66*M,   P.RED,   "8-21 真故障：积压 66 分钟且同步器跑了 5 次仍未追平", 5),
    (1,   46*M,   46*M,   P.RED,   "只差 1 条但同步器跑了 4 次都没搬动 → 真卡住", 4),
    (41,  66*M,   66*M,   P.AMBER, "★同样积压 66 分钟，但同步器只跑了 1 次(机器在睡) → 不告警", 1),
    (41,  66*M,   66*M,   P.AMBER, "★同样积压 66 分钟，同步器一次都没轮到 → 不告警", 0),
    (10,  None,   50*M,   P.AMBER, "算不出等待时长(库读不到) → 留意但不告警，不猜"),
    # ↓ 2026-10-08：双源切换后探针一直拿冻结的旧库比 NAS，落差 -9205 照样绿。
    (-9205, None, 10*M,   P.RED,   "★负落差：比错了库，判据失效必须出声"),
]
ok = fail = 0
for case in CASES:
    lag, bage, sage, want, why = case[:5]
    runs = case[5] if len(case) > 5 else 0
    got = judge(lag, bage, sage, runs)
    mark = "✅" if got == want else "❌"
    ok, fail = (ok + 1, fail) if got == want else (ok, fail + 1)
    ba = "—" if bage is None else f"{bage//60}分"
    print(f"  {mark} 落差{lag:>4} / 积压{ba:>4} / 同步器跑{runs}次 → {got:<5} (期望 {want:<5}) {why}")


# ── 双源采集：真实 SQLite 临时库，判据走 probe_sync 真实路径 ──────────────
import json, sqlite3, tempfile


def dual_judge(*, unmerged_age_s, aggregate_ahead=0, log_lines=(), break_aggregate=False):
    """legacy + next 两个源并入聚合库；next 在游标之后还有未合并的行。"""
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        now_ms = time.time() * 1000.0
        dbs = {}
        for name in ("legacy", "next", "aggregate"):
            dbs[name] = root / f"{name}.db"
            con = sqlite3.connect(dbs[name])
            con.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, created_at_epoch INTEGER)")
            con.commit(); con.close()
        def add(name, ids, age_s):
            con = sqlite3.connect(dbs[name])
            con.executemany("INSERT INTO observations VALUES (?,?)",
                            [(i, now_ms - age_s * 1000.0) for i in ids])
            con.commit(); con.close()
        add("legacy", range(1, 11), 86400)
        add("next", range(1, 6), 86400)
        add("aggregate", range(1, 16 + aggregate_ahead), 3600)
        if unmerged_age_s is not None:
            add("next", range(6, 9), unmerged_age_s)
        con = sqlite3.connect(dbs["aggregate"])
        con.execute("CREATE TABLE source_cursor (name TEXT, last_id INTEGER)")
        con.executemany("INSERT INTO source_cursor VALUES (?,?)", [("legacy", 10), ("next", 5)])
        con.commit(); con.close()
        if break_aggregate:
            dbs["aggregate"].write_bytes(b"not a database")
        config = root / "sources.json"
        config.write_text(json.dumps({"sources": [
            {"name": "legacy", "path": str(dbs["legacy"])},
            {"name": "next", "path": str(dbs["next"])}]}))
        marker = root / "dual-active.json"
        marker.write_text(json.dumps({"source_config": str(config)}))
        log = root / "sync.out.log"
        log.write_text("\n".join(log_lines))
        saved = (P.DUAL_MARKER, P.AGGREGATE_DB, P.SYNC_LOG, P.SYNC_STAMP, P.sh)
        P.DUAL_MARKER, P.AGGREGATE_DB, P.SYNC_LOG = marker, dbs["aggregate"], log
        P.SYNC_STAMP = root / "missing.stamp"
        P.sh = lambda *a, **k: ("ok@@15", None)       # NAS 与聚合库同步到 15
        try:
            return P.probe_sync(None, "dummy-host")
        finally:
            P.DUAL_MARKER, P.AGGREGATE_DB, P.SYNC_LOG, P.SYNC_STAMP, P.sh = saved


def lines(n, text, age_s):
    base = time.time() - age_s + 60
    return [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + i * 60)) + " " + text
            for i in range(n)]


FENCED = "skip: legacy-only sync fenced by dual capture activation"
FAILED = "FAIL 双源合并失败（原因见 claude-mem-sync.err.log）"
DUAL_CASES = [
    (dict(unmerged_age_s=None), P.GREEN, "两源都已并入、聚合库与 NAS 一致 → 健康"),
    (dict(unmerged_age_s=3 * 3600, log_lines=lines(4, FAILED, 3 * 3600)), P.RED,
     "★10-03 实况：合并连续失败，聚合库=NAS，但源里有 3 条等了 3 小时"),
    (dict(unmerged_age_s=3 * 3600, log_lines=lines(9, FENCED, 3 * 3600)), P.AMBER,
     "★同样卡 3 小时，日志里只有被隔离的旧作业行 → 不算同步机会，不告警"),
    (dict(unmerged_age_s=4 * 60), P.GREEN, "新观测刚产生、尚未到合并周期 → 健康"),
    (dict(unmerged_age_s=None, aggregate_ahead=2, log_lines=lines(5, "推送失败", 3600)),
     P.RED, "聚合库领先 NAS 且同步器跑了 5 次 → 推送卡住"),
    (dict(unmerged_age_s=None, break_aggregate=True), P.RED, "聚合库读不到 → 判据失去比对对象"),
]
for kwargs, want, why in DUAL_CASES:
    node = dual_judge(**kwargs)
    got = node["state"]
    mark = "✅" if got == want else "❌"
    ok, fail = (ok + 1, fail) if got == want else (ok, fail + 1)
    print(f"  {mark} 双源 → {got:<5} (期望 {want:<5}) {why}｜{node.get('detail', '')[:90]}")
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
