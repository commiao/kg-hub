"""最近若干次抽取的耗时拆分：排队等写锁多久、真正抽取多久。

这两个数原先只进 `[ingest:lock_acquired] waited=` 与 `[ingest:done] elapsed=` 日志，
看板和告警都读不到 —— 于是「积压卡在写锁排队」还是「卡在模型调用本身」只能靠翻
容器日志猜。这里在进程内留一个有界窗口，供积压消化看板取分位数。

只存数字，不存来源 ID 或正文。进程重启即清空，`started_at` 让读者知道样本从何时起。
"""
from __future__ import annotations

import threading
import time
from collections import deque

MAX_SAMPLES = 500

_samples: deque[dict] = deque(maxlen=MAX_SAMPLES)
_lock = threading.Lock()
_started_at = time.time()


def record(*, waited_s: float, extract_s: float | None, outcome: str,
           parallel: bool = False, at: float | None = None) -> None:
    sample = {
        "at": time.time() if at is None else float(at),
        "waited_s": max(0.0, float(waited_s)),
        "extract_s": None if extract_s is None else max(0.0, float(extract_s)),
        "outcome": str(outcome),
        "parallel": bool(parallel),
    }
    with _lock:
        _samples.append(sample)


def reset() -> None:
    global _started_at
    with _lock:
        _samples.clear()
        _started_at = time.time()


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 1)


def summary(window_s: float = 86400, now: float | None = None) -> dict:
    now = time.time() if now is None else float(now)
    with _lock:
        rows = [s for s in _samples if 0 <= now - s["at"] <= window_s]
        started_at = _started_at
    waits = [s["waited_s"] for s in rows]
    extracts = [s["extract_s"] for s in rows if s["extract_s"] is not None]
    busy = sum(waits) + sum(extracts)
    outcomes: dict[str, int] = {}
    for s in rows:
        outcomes[s["outcome"]] = outcomes.get(s["outcome"], 0) + 1
    return {
        "samples": len(rows),
        "window_s": int(window_s),
        "process_started_at": started_at,
        # 窗口内最早一条样本：分位数实际从这一刻算起（重启清空、500 条上限都会把它往后推）。
        "earliest_at": min((s["at"] for s in rows), default=None),
        "wait_p50": _percentile(waits, 0.5),
        "wait_p90": _percentile(waits, 0.9),
        "extract_p50": _percentile(extracts, 0.5),
        "extract_p90": _percentile(extracts, 0.9),
        # 排队时间在「排队 + 抽取」总时长里的占比；高说明写锁是瓶颈，低说明是模型本身慢。
        "wait_share": round(sum(waits) / busy, 3) if busy > 0 else None,
        "outcomes": outcomes,
        "parallel": any(s["parallel"] for s in rows),
    }
