"""一次 /api/ingest 最坏还要花多久 —— 三处等待预算的唯一来源。

## 为什么要有这个文件

2026-09-20 夜实测：同一件事上有三个互不相同的数，谁也不知道另外两个存在。

    服务端写锁重试上限   kg_hub_server.py 的三个常数推出来      1155s
    refinery 轮询上限    kg_refinery.poll_until_done(max_wait)    600s  ← 写死
    发布排空上限         deploy/nas/release.sh 的 seq 1 60×5      300s  ← 写死

而实测一次**成功**入图 `[ingest:done] obs 5728 elapsed=1091.8s`（10 nodes / 13
edges）。两个后果当晚都撞上了：

- refinery 在 600s 放弃，记一次 `timeout` 并在下一轮重推 —— 而服务端还在合法地
  等锁/抽取，稍后真的成功了。于是同一条观测被推第二次，撞上自己刚建的键，
  换来一个 409 和一串指数退避。**那个 timeout 是假的，409 是它自己造的。**
- release.sh 的排空在 300s 到点，按设计中止发布。它的注释写着「每条约 3 分钟，
  所以正常情况下最坏等 3 分钟左右」—— 这个假设被 1091.8s 证伪，于是发布在窗口
  期内几乎必然要撞一次中止再重来。

准则 28 的形状：一个判断的两端取自不同来源，它迟早会报一件不存在的事。这里把
三个数收到一处，让等待方都从服务端自己的参数派生。

## 派生口径

写锁那一段是**可以算的**，因为它完全由三个参数决定（注意 attempt 从 0 起、
`attempt > RETRIES` 才放弃，所以真正尝试取锁的次数是 RETRIES + 1 —— 服务端
那句报错文案里的 `~RETRIES × TIMEOUT` 其实少算了一次）。

抽取那一段**算不出来**：它取决于文档长度、实体数和上游延迟。所以它是一个显式的
实测值，而不是假装派生出来的数 —— 谁改它都得同时改掉这里的出处说明。
"""
from __future__ import annotations

import os

# —— 服务端写锁参数。kg_hub_server 直接用这三个，不再各自 os.environ ——
# （2026-06-13 锁竞争事故后加的线性退避：抢不到锁不是失败，是排队。）
INGEST_LOCK_TIMEOUT_SEC = float(os.environ.get("KG_HUB_INGEST_LOCK_TIMEOUT_SEC", "180.0"))
INGEST_LOCK_RETRIES = int(os.environ.get("KG_HUB_INGEST_LOCK_RETRIES", "5"))
INGEST_LOCK_BACKOFF_SEC = float(os.environ.get("KG_HUB_INGEST_LOCK_BACKOFF_SEC", "5.0"))

# 拿到锁之后那一次 add_episode 最坏多久。**实测值，不是派生值**：
# 2026-09-20 夜线上最长一次成功入图 1091.8s（obs 5728，10 nodes / 13 edges）。
# 取 1200s 留约 10% 余量。改它请一并更新这行出处。
INGEST_EXTRACT_BUDGET_SEC = float(
    os.environ.get("KG_HUB_INGEST_EXTRACT_BUDGET_SEC", "1200.0"))


def lock_wait_ceiling_sec() -> float:
    """等写锁最坏要等多久。

    循环语义（kg_hub_server.do_extract）：attempt 从 0 开始，每次都等满
    INGEST_LOCK_TIMEOUT_SEC；WriterLockBusy 后 attempt += 1，`attempt > RETRIES`
    才放弃，两次尝试之间线性退避 BACKOFF × attempt。
    """
    attempts = INGEST_LOCK_RETRIES + 1
    backoffs = INGEST_LOCK_BACKOFF_SEC * (
        INGEST_LOCK_RETRIES * (INGEST_LOCK_RETRIES + 1) / 2)
    return attempts * INGEST_LOCK_TIMEOUT_SEC + backoffs


def ingest_ceiling_sec() -> float:
    """一次 /api/ingest 从收到请求到落终态，最坏多久。

    等待方（refinery 的轮询、发布脚本的排空）都不该比这个数先放弃：先放弃换不来
    任何信息，只会把一条还在正常进行的抽取记成失败，然后用重推给自己造一个 409。

    发布排空用同一个数：INGEST_CONCURRENCY=2 时，第二条的等锁与第一条的抽取是
    **重叠**的（它在等，不是排在后面），所以这个上限覆盖现实中的最坏情况。真到点
    还没排空干净，中止发布仍然是对的 —— 那是干净可重试的，硬切不是。
    """
    return lock_wait_ceiling_sec() + INGEST_EXTRACT_BUDGET_SEC


if __name__ == "__main__":  # 给 shell 用：release.sh 的排空预算从这里取
    print(int(ingest_ceiling_sec()))
