"""409 退避的行为测试:证明活锁不会重演。

2026-08-25 事故:LLM 供应商 key 到期 → 服务端 error 键 → 每次 POST 回 409。
原实现把 409 当"下轮重试",于是每 90s 重试全部积压,一夜 **6864 条 409**,
CPU/IO 空转、日志淹没,而 backlog_remaining 一动不动、watchdog state=OK。

本测试直接调 process_batch(不复刻判定逻辑——上次 test_probe_sync_thresholds
就因为"复刻"而在实现改动后依然全绿,等于零保护)。
"""
import asyncio, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kg_refinery as R


def run(rows, wm, backoff, cycle, verdict="409"):
    """跑一轮 process_batch,ingest 结果由 verdict 决定。"""
    orig = R.ingest_via_api
    R.ingest_via_api = lambda obs: asyncio.sleep(0, result=verdict)
    calls = []
    _real = R.ingest_via_api
    async def counting(obs):
        calls.append(obs["id"])
        return verdict
    R.ingest_via_api = counting
    try:
        cfg = {"shadow_mode": True, "global": {}, "scoring": {}, "platforms": {"_default": {}}}
        # 用 decided 预置 accept,绕过 ingest_filter(本测试只验退避,不验过滤)
        decided = {r["id"]: True for r in rows}
        stats = asyncio.run(R.process_batch(rows, wm, cfg, R.QuotaTracker(),
                                            decided, backoff, cycle, "test"))
        return stats, calls
    finally:
        R.ingest_via_api = orig


def fresh_wm():
    return {"ingested": set(), "rejected": set(), "failed": set(),
            "boundary_id": 0, "live_cursor": None}


ROWS = [{"id": 101, "content_hash": "h101", "created_at": "2026-08-25T00:00:00Z",
         "project": "p", "type": "discovery", "title": "t", "narrative": "n"}]
ok = fail = 0


def check(name, cond):
    global ok, fail
    print(("  ✅ " if cond else "  ❌ ") + name)
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)


# 场景:连续 409 → 退避轮次应指数增长,冷却期内不再发请求
R.save_watermark = lambda wm: None       # 不写盘
wm, backoff = fresh_wm(), {}

_, calls1 = run(ROWS, wm, backoff, cycle=1)
check("第1轮 409:发了请求", calls1 == [101])
check("记录退避 n=1, 下次 cycle=2", backoff[101] == [1, 2])

_, calls2 = run(ROWS, wm, backoff, cycle=1)   # 同一轮再跑 = 冷却中
check("冷却期内:**不发请求**(活锁根治点)", calls2 == [])

_, calls3 = run(ROWS, wm, backoff, cycle=2)   # 到期可试
check("退避到期:重新发请求", calls3 == [101])
check("第2次409 → n=2, 等 2 轮(cycle=4)", backoff[101] == [2, 4])

for c, want_n, want_next in [(4, 3, 8), (8, 4, 16), (16, 5, 32)]:
    run(ROWS, wm, backoff, cycle=c)
check("指数增长到 n=5 / 等 16 轮", backoff[101] == [5, 32])

run(ROWS, wm, backoff, cycle=32)
check("上限封顶 32 轮(≈48min),不无限增长", backoff[101][1] - 32 <= R.BACKOFF_MAX_CYCLES)

# 恢复后:一次成功即清退避
wm2, backoff2 = fresh_wm(), {101: [5, 999]}
stats, calls = run(ROWS, wm2, backoff2, cycle=1000, verdict="ok")
check("成功入图后清掉退避记录(恢复即全速)", 101 not in backoff2 and 101 in wm2["ingested"])

# 对照:活锁重演会是什么样(证明测试真能抓到回归)
wm3, backoff3 = fresh_wm(), {}
total = 0
for c in range(1, 21):
    _, calls = run(ROWS, wm3, backoff3, cycle=c)
    total += len(calls)
check(f"20 轮内只发 {total} 次请求(旧实现会发 20 次)", total <= 6)

# 队头阻塞:冷却中的 id 不得占用积压名额(2026-09-06 积压 7919 三天零进展的直接机制)
pending = list(range(1, 21))                 # 20 条待处理,按 id 升序
cooling = {i: [3, 99] for i in range(1, 9)}  # 前 8 条正在退避(下次可试 cycle=99)
picked = R.select_backlog_batch(pending, cooling, cycle=10, limit=8)
check("冷却中的 8 条不占名额,轮到身后的 9..16", picked == list(range(9, 17)))
picked = R.select_backlog_batch(pending, cooling, cycle=99, limit=8)
check("退避到期后照常回到队头重试(不丢数据)", picked == list(range(1, 9)))
check("无退避时与旧切片 [:N] 一致", R.select_backlog_batch(pending, {}, cycle=1, limit=8) == pending[:8])
check("名额上限严格", len(R.select_backlog_batch(pending, {}, cycle=1, limit=3)) == 3)

# 配额耗尽:网关 429 时整窗停发,不逐条撞(2026-09-06 夜 218 篇败/127 篇成)
async def quota_verdict(obs):
    quota_calls.append(obs["id"]); return "quota"
quota_calls = []
R.ingest_via_api = quota_verdict
rows2 = [{**ROWS[0], "id": 201}, {**ROWS[0], "id": 202}, {**ROWS[0], "id": 203}]
wmq, qp = fresh_wm(), {}
stats_q = asyncio.run(R.process_batch(rows2, wmq, {"shadow_mode": True, "global": {}, "scoring": {}, "platforms": {"_default": {}}},
                                       R.QuotaTracker(), {r["id"]: True for r in rows2}, {}, 7, "test",
                                       quota_pause=qp))
check(f"配额耗尽后本批立即停发(3 条最多发 {R.INGEST_CONCURRENCY} 条=并发上限,第 3 条不发)",
      len(quota_calls) <= R.INGEST_CONCURRENCY and 203 not in quota_calls)
check("记录停发到期轮次", qp.get("until_cycle") == 7 + R.QUOTA_PAUSE_CYCLES and qp.get("hits") == 1)
check("不落水印(到期后照常重试)", 201 not in wmq["ingested"] and 201 not in wmq["failed"])
check("stats 暴露 quota_paused", stats_q.get("quota_paused") == 1)

# 轮询节奏:先密后疏。固定 8s 让"1 秒内就失败"的条目白等一整周期(实测中位 8.1s)
slept = []
async def fake_sleep(d): slept.append(d)
_orig_sleep, _orig_http = asyncio.sleep, R._http
polls = {"n": 0}
def fake_http(method, url, body=None, timeout=30):
    polls["n"] += 1
    return (200, {"status": "ok"} if polls["n"] >= 4 else {"status": "in_progress"})
asyncio.sleep, R._http = fake_sleep, fake_http
try:
    st = asyncio.run(R.poll_until_done("sd", "sid"))
finally:
    asyncio.sleep, R._http = _orig_sleep, _orig_http
check("poll 拿到终态就返回", st == "ok")
check(f"首次等待 1s 而非 8s(实测节奏 {slept[:3]})", slept and slept[0] == 1)
check("节奏先密后疏 1→1→2", slept[:3] == [1, 1, 2])
check("步长上限 8s", max(R.POLL_STEPS_S) == 8)

# 并发:批内两条同时在飞 → 一条慢抽取不再堵住身后的快速失败
order = []
async def slow_then_fast(obs):
    order.append(("start", obs["id"]))
    await _orig_sleep(0.2 if obs["id"] == 301 else 0.01)
    order.append(("end", obs["id"]))
    return "ok"
R.ingest_via_api = slow_then_fast
rows3 = [{**ROWS[0], "id": 301}, {**ROWS[0], "id": 302}]
wmc = fresh_wm()
stats_c = asyncio.run(R.process_batch(rows3, wmc, {"shadow_mode": True, "global": {}, "scoring": {}, "platforms": {"_default": {}}},
                                      R.QuotaTracker(), {r["id"]: True for r in rows3}, {}, 1, "test"))
check("两条都入图", stats_c["ingested"] == 2)
check("慢条目未阻塞后一条(302 先完成)",
      order.index(("end", 302)) < order.index(("end", 301)) if R.INGEST_CONCURRENCY > 1 else True)
check("水印两条都落账", wmc["ingested"] == {301, 302})

# 落账必须"每条完成即写",不能等整批 gather 完 —— 第一版就是这么错的:200 条一批
# 要全跑完(~100 分钟)才写一次水印,网关明明在调用而 backlog_remaining 半小时不动。
saves = []
_real_save = R.save_watermark
R.save_watermark = lambda wm: saves.append(len(wm["ingested"]))
async def one_by_one(obs):
    await _orig_sleep(0.01)
    return "ok"
R.ingest_via_api = one_by_one
try:
    wmi = fresh_wm()
    rows4 = [{**ROWS[0], "id": 400 + i} for i in range(4)]
    asyncio.run(R.process_batch(rows4, wmi, {"shadow_mode": True, "global": {}, "scoring": {}, "platforms": {"_default": {}}},
                                R.QuotaTracker(), {r["id"]: True for r in rows4}, {}, 1, "test"))
finally:
    R.save_watermark = _real_save
check(f"4 条落账写了 4 次水印(增量可见,实测 {saves})", saves == [1, 2, 3, 4])

# 主循环:积压必须排在 live 之前,且每段都落一次状态
import inspect as _inspect
_src = _inspect.getsource(R.main)
check("积压线排在 live 线之前(否则 live 一轮吃掉整个窗口,积压整夜拿不到名额)",
      _src.index('kind="backlog"' if 'kind="backlog"' in _src else '"backlog"')
      < _src.index('"live"'))
check("每段结束都落状态(不再整轮才写一次)", _src.count("snapshot(") >= 3)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
