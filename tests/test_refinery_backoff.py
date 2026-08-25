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

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
