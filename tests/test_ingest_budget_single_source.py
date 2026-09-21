"""三处等待预算必须来自同一个源。

2026-09-20 夜实测，同一件事上有三个互不相同的数：

    服务端写锁上限   由 kg_hub_server 的三个常数决定      1155s
    refinery 轮询    poll_until_done(max_wait=600)         600s   ← 写死
    发布排空         release.sh 的 seq 1 60 × sleep 5      300s   ← 写死

而一次**成功**入图实测 elapsed=1091.8s。两个后果当晚都撞上了：refinery 在服务端
还在正常干活时记假 timeout、重推给自己造 409；release.sh 在 300s 到点中止发布，
窗口期内几乎必然要重来一次。

这里钉的不是那几个数，是**它们只有一个出处**（准则 28）。所以主力用例是「改一处，
三处一起动」——把常数抄成第二份，这种用例才会响，而只比对字面值的用例不会。
"""
from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kg_refinery as refinery  # noqa: E402
from utils import ingest_budget  # noqa: E402

SERVER_SRC = (ROOT / "kg_hub_server.py").read_text("utf-8")
RELEASE_SRC = (ROOT / "deploy" / "nas" / "release.sh").read_text("utf-8")


def budget_under(**env) -> int:
    """在子进程里按给定环境算一次预算——和 release.sh 取数的方式完全一致。"""
    e = dict(os.environ)
    e.update({k: str(v) for k, v in env.items()})
    out = subprocess.run([sys.executable, "-m", "utils.ingest_budget"],
                         cwd=ROOT, env=e, capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


class DerivationTests(unittest.TestCase):
    def test_ceiling_counts_the_first_attempt_too(self):
        """attempt 从 0 起、`attempt > RETRIES` 才放弃 ⇒ 取锁尝试是 RETRIES + 1 次。

        服务端那句报错文案原先自己算 `RETRIES × TIMEOUT`，正是漏了这一次，把
        1155s 报成 900s。
        """
        self.assertEqual(
            budget_under(KG_HUB_INGEST_LOCK_RETRIES=0,
                         KG_HUB_INGEST_LOCK_TIMEOUT_SEC=10,
                         KG_HUB_INGEST_EXTRACT_BUDGET_SEC=0),
            10, "RETRIES=0 时仍有一次取锁尝试")
        # RETRIES=2：3 次 × 10s + 退避 (1+2)×5s = 45s
        self.assertEqual(
            budget_under(KG_HUB_INGEST_LOCK_RETRIES=2,
                         KG_HUB_INGEST_LOCK_TIMEOUT_SEC=10,
                         KG_HUB_INGEST_LOCK_BACKOFF_SEC=5,
                         KG_HUB_INGEST_EXTRACT_BUDGET_SEC=0),
            45)

    def test_extraction_budget_is_declared_as_measured_not_derived(self):
        """抽取时长算不出来，只能实测。那就必须写成显式常量并留出处，
        而不是假装派生 —— 否则下一个人会以为它也是推出来的，改了不查实测。"""
        src = (ROOT / "utils" / "ingest_budget.py").read_text("utf-8")
        self.assertIn("1091.8s", src, "实测出处不在文件里，这个数就没人能复核")
        self.assertIn("INGEST_EXTRACT_BUDGET_SEC", src)


class OneSourceTests(unittest.TestCase):
    """改一处，三处一起动。抄成第二份的话，下面任一条就会响。"""

    def test_server_does_not_keep_a_second_copy(self):
        for var in ("KG_HUB_INGEST_LOCK_TIMEOUT_SEC", "KG_HUB_INGEST_LOCK_RETRIES",
                    "KG_HUB_INGEST_LOCK_BACKOFF_SEC"):
            self.assertNotIn(f'os.environ.get("{var}"', SERVER_SRC,
                             f"{var} 在 kg_hub_server 里又读了一遍——那就是第二个源")
        for name in ("INGEST_LOCK_TIMEOUT_SEC", "INGEST_LOCK_RETRIES",
                     "INGEST_LOCK_BACKOFF_SEC"):
            self.assertIn(f"{name} = ingest_budget.{name}", SERVER_SRC)

    def test_refinery_poll_never_gives_up_before_the_server_can(self):
        default = inspect.signature(refinery.poll_until_done).parameters["max_wait"].default
        self.assertEqual(default, refinery.POLL_MAX_WAIT_S)
        self.assertGreaterEqual(
            default, int(ingest_budget.ingest_ceiling_sec()),
            "轮询上限短于服务端上限——会把还在正常进行的抽取记成 timeout")
        self.assertNotIn("max_wait: int = 600", (ROOT / "kg_refinery.py").read_text("utf-8"))

    def test_release_drain_is_derived_not_hardcoded(self):
        self.assertIn("python3 -m utils.ingest_budget", RELEASE_SRC,
                      "release.sh 没从唯一来源取排空预算")
        self.assertNotIn("seq 1 60", RELEASE_SRC, "写死的 60 轮还在")
        self.assertIsNone(
            re.search(r'die "5 分钟没排空', RELEASE_SRC),
            "报错文案还在说「5 分钟」——它必须跟着预算一起变，否则又是一个说谎的数")

    def test_release_refuses_to_guess_when_the_budget_is_unavailable(self):
        """算不出来就别发。默认回 300s 才是最坏的失败方式：它看起来正常。"""
        block = RELEASE_SRC.split("DRAIN_BUDGET_S=$(", 1)[1][:400]
        self.assertIn("die", block, "预算取不到时没有 die，会静默用一个猜的数")

    def test_moving_the_knob_moves_every_consumer(self):
        """把写锁超时翻倍，三处必须一起变。

        这条是这套测试的主力：它验的是耦合，而不是某个具体数字。
        """
        base = budget_under()
        doubled = budget_under(KG_HUB_INGEST_LOCK_TIMEOUT_SEC=360)
        self.assertGreater(doubled, base)
        # refinery 与 release.sh 都从这个函数取数，所以同一个进程里换环境即可验证
        # 它们的取值随之变化（release.sh 调的就是上面那条命令）。
        self.assertEqual(doubled, int(_ceiling_with(KG_HUB_INGEST_LOCK_TIMEOUT_SEC="360")))


def _ceiling_with(**env) -> float:
    """在改过环境后重新导入预算模块，模拟一个新起的进程。"""
    import importlib
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return importlib.reload(ingest_budget).ingest_ceiling_sec()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(ingest_budget)


if __name__ == "__main__":
    unittest.main()
