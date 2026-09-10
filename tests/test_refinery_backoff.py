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
REAL_INGEST_VIA_API = R.ingest_via_api


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
check("stats 区分 quota 与同批停发", stats_q["result_counts"].get("quota") == 1
      and stats_q["result_counts"].get("halted", 0) >= 1)

# SDK 的 RateLimitError 由服务端按一小时释放；恢复探测不得早于这个阈值。
async def rate_limit_verdict(obs):
    rate_limit_calls.append(obs["id"]); return "rate_limited"
rate_limit_calls = []
R.ingest_via_api = rate_limit_verdict
wmrl, rlp = fresh_wm(), {}
stats_rl = asyncio.run(R.process_batch(rows2, wmrl, {"shadow_mode": True, "global": {}, "scoring": {}, "platforms": {"_default": {}}},
                                        R.QuotaTracker(), {r["id"]: True for r in rows2}, {}, 7, "test",
                                        quota_pause=rlp))
check("上游限流后本批立即停发", len(rate_limit_calls) <= R.INGEST_CONCURRENCY and 203 not in rate_limit_calls)
check("上游限流等待超过一小时", R.RATE_LIMIT_PAUSE_CYCLES * R.INTERVAL > 3600)
check("记录上游限流到期轮次", rlp.get("until_cycle") == 7 + R.RATE_LIMIT_PAUSE_CYCLES and rlp.get("reason") == "rate_limited")
check("stats 暴露 rate_limited", stats_rl.get("rate_limited") == 1)
check("stats 暴露 rate_limited 类别", stats_rl["result_counts"].get("rate_limited") == 1)

# 已在飞的第二条可在限流结果之后才完成；较短的 quota 暂停绝不能覆盖 1 小时暂停。
async def run_mixed_limits():
    mixed_ready = asyncio.Event()
    async def mixed_limit_verdict(obs):
        if obs["id"] == 201:
            await mixed_ready.wait()
            return "rate_limited"
        mixed_ready.set()
        await asyncio.sleep(0)
        return "quota"
    R.ingest_via_api = mixed_limit_verdict
    wmm, mixed_pause = fresh_wm(), {}
    await R.process_batch(rows2[:2], wmm, {"shadow_mode": True, "global": {}, "scoring": {}, "platforms": {"_default": {}}},
                          R.QuotaTracker(), {r["id"]: True for r in rows2[:2]}, {}, 7, "test",
                          quota_pause=mixed_pause)
    return mixed_pause
mixed_pause = asyncio.run(run_mixed_limits())
check("并发短暂停不停覆盖长暂停", mixed_pause.get("until_cycle") == 7 + R.RATE_LIMIT_PAUSE_CYCLES
      and mixed_pause.get("reason") == "rate_limited")

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

# poll 必须先看 HTTP code:服务端「键不存在」是 404 + {"status":"error"},
# 只读正文会把它当成这条观测抽取失败(T-0033 记录的隐患;首次轮询缩到 1s 后更易撞上)
def poll_with(responses):
    """responses: [(code, body), ...] 按顺序返回;记录轮询次数。"""
    seq = list(responses)
    calls = {"n": 0}
    def http(method, url, body=None, timeout=30):
        calls["n"] += 1
        return seq[min(calls["n"] - 1, len(seq) - 1)]
    _sleep, _http_orig = asyncio.sleep, R._http
    asyncio.sleep, R._http = fake_sleep, http
    try:
        return asyncio.run(R.poll_until_done("sd", "sid")), calls["n"]
    finally:
        asyncio.sleep, R._http = _sleep, _http_orig

st_gone, _ = poll_with([(404, {"status": "error", "code": "not_found"})])
check("404 → gone(不是 error;正文同样是 status=error)", st_gone == "gone")
st_bad, _ = poll_with([(400, {"status": "error", "code": "bad_request"})])
check("400 → error(参数问题,重试无用)", st_bad == "error")
st_ok, n_ok = poll_with([(200, {"status": "in_progress"}), (200, {"status": "ok"})])
check("200 in_progress → 继续轮询 → ok", st_ok == "ok" and n_ok == 2)
st_q, _ = poll_with([(200, {"status": "error", "error_kind": "quota_exhausted"})])
check("200 + quota_exhausted → quota", st_q == "quota")
st_rl, _ = poll_with([(200, {"status": "error", "error_kind": "rate_limited"})])
check("200 + rate_limited → 独立限流暂停", st_rl == "rate_limited")
st_net, n_net = poll_with([(0, {"error": "net"}), (200, {"status": "ok"})])
check("网络层失败不当终态,继续轮询", st_net == "ok" and n_net == 2)
st_5xx, _ = poll_with([(503, {"status": "error"})])
check("5xx 视为瞬时:轮询到上限而非误判失败", st_5xx == "timeout")

# POST 的非成功响应只外露状态码类别，绝不把服务端 message 写入 refinery 状态。
_orig_http = R._http
R._http = lambda method, url, body=None, timeout=30: (500, {"status": "error", "message": "private"})
try:
    st_post_500 = asyncio.run(REAL_INGEST_VIA_API(ROWS[0]))
finally:
    R._http = _orig_http
check("POST 500 → 无内容 http_500 类别", st_post_500 == "http_500")

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

# 批内进度必须对外可见:每条落账都回调一次,而不是等整批 gather 完
progress = []
R.save_watermark = lambda wm: None
R.ingest_via_api = one_by_one
wmp = fresh_wm()
rows5 = [{**ROWS[0], "id": 500 + i} for i in range(4)]
asyncio.run(R.process_batch(rows5, wmp, {"shadow_mode": True, "global": {}, "scoring": {}, "platforms": {"_default": {}}},
                            R.QuotaTracker(), {r["id"]: True for r in rows5}, {}, 1, "test",
                            on_progress=lambda st: progress.append(st["ingested"])))
check(f"4 条各回调一次且累计递增(实测 {progress})", progress == [1, 2, 3, 4])

# 过滤阶段的拒绝也要回调(rejected 计数同样要即时可见)
progress_r = []
wmr = fresh_wm()
rows6 = [{**ROWS[0], "id": 600 + i} for i in range(3)]
asyncio.run(R.process_batch(rows6, wmr, {"shadow_mode": True, "global": {}, "scoring": {}, "platforms": {"_default": {}}},
                            R.QuotaTracker(), {r["id"]: False for r in rows6}, {}, 1, "test",
                            on_progress=lambda st: progress_r.append(st["rejected"])))
check(f"3 条拒绝各回调一次(实测 {progress_r})", progress_r == [1, 2, 3])

# 回调抛错不得影响入图(状态刷新失败是次要的)
wmb = fresh_wm()
rows7 = [{**ROWS[0], "id": 700}]
def boom(_st): raise RuntimeError("status write failed")
stats_b = asyncio.run(R.process_batch(rows7, wmb, {"shadow_mode": True, "global": {}, "scoring": {}, "platforms": {"_default": {}}},
                                      R.QuotaTracker(), {700: True}, {}, 1, "test", on_progress=boom))
check("on_progress 抛错不影响入图", stats_b["ingested"] == 1 and 700 in wmb["ingested"])
R.save_watermark = _real_save

# 主循环:积压必须排在 live 之前,且每段都落一次状态
import inspect as _inspect
_src = _inspect.getsource(R.main)
check("积压线排在 live 线之前(否则 live 一轮吃掉整个窗口,积压整夜拿不到名额)",
      _src.index('kind="backlog"' if 'kind="backlog"' in _src else '"backlog"')
      < _src.index('"live"'))
check("每段结束都落状态(不再整轮才写一次)", _src.count("snapshot(") >= 3)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
