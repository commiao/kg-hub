#!/usr/bin/env python3
"""排空态：拒收新写入，让「排空不掉就别发」这条闸的终点可达。

T-0117。`release.sh` 停的是自己的 refinery / ingester，而 `/api/ingest` 还有
别的写入方（NAS 上的 task-hub 容器结晶入图、任何用 kg_hub MCP 的会话直写）。
2026-09-21 实测一次发布等了约 1260s 才归零 —— 预算再大也没用，**只要到达率
不为零，计数就可能一直不为零**，最后一律落到中止。

拒收之所以安全，是读了调用方的重试路径确认的：task-hub 的 reconciler 只在
POST 返回 2xx 之后才落 `reconciler_marks`，失败就不落、下一轮（300s）重来、
按任务 id 幂等。
"""

from __future__ import annotations

import importlib.util
import re
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "deploy/nas/release.sh"


def load_server_symbols() -> types.SimpleNamespace:
    """只取排空那几个函数，不导入整个服务端。

    `kg_hub_server` 在导入时就会要求 KG_HUB_API_TOKEN 等一堆环境，把它整个拉起来
    会让这组用例的成败取决于本机环境 —— 而它们要验的是排空逻辑（准则 28）。
    所以按源码片段单独执行那一段。
    """
    source = (ROOT / "kg_hub_server.py").read_text("utf-8")
    start = source.index("_drain_until = 0.0")
    end = source.index("def _extraction_started()")
    ns: dict = {"threading": __import__("threading"), "time": time}
    exec(compile(source[start:end], "kg_hub_server.py", "exec"), ns)
    return types.SimpleNamespace(**ns)


class DrainStateTests(unittest.TestCase):
    def setUp(self):
        self.m = load_server_symbols()
        self.addCleanup(lambda: self.m.set_drain(0))

    def test_not_draining_by_default(self):
        self.assertEqual(self.m.drain_seconds_left(), 0.0)

    def test_entering_and_leaving(self):
        self.assertGreater(self.m.set_drain(60), 0)
        self.assertGreater(self.m.drain_seconds_left(), 0)
        self.m.set_drain(0)
        self.assertEqual(self.m.drain_seconds_left(), 0.0)

    def test_it_expires_on_its_own(self):
        """**必须带截止时间。**

        发布中途崩掉、ssh 断掉、有人按了 Ctrl-C —— 任何一种都会留下一个再也没人
        来清的排空态。没有截止时间，那就是永久拒写：一次发布事故变成一次数据
        管道事故。
        """
        self.m.set_drain(0.05)
        self.assertGreater(self.m.drain_seconds_left(), 0)
        time.sleep(0.08)
        self.assertEqual(self.m.drain_seconds_left(), 0.0)

    def test_the_window_is_capped(self):
        """上限兜住笔误和恶意值 —— 不然一个多打的零就是十小时拒写。"""
        self.m.set_drain(10 ** 9)
        self.assertLess(self.m.drain_seconds_left(), 10 ** 6)

    def test_a_negative_value_clears_instead_of_wrapping(self):
        self.m.set_drain(60)
        self.m.set_drain(-1)
        self.assertEqual(self.m.drain_seconds_left(), 0.0)


class ServerWiringTests(unittest.TestCase):
    """钉住放行的那一行，而不是「这些字眼在不在源码里」。"""

    def setUp(self):
        self.source = (ROOT / "kg_hub_server.py").read_text("utf-8")
        self.lines = [x for x in self.source.splitlines()
                      if not x.lstrip().startswith("#")]

    def _body(self, defname: str) -> str:
        i = next(k for k, x in enumerate(self.lines)
                 if x.startswith(f"async def {defname}("))
        j = next((k for k in range(i + 1, len(self.lines))
                  if self.lines[k].startswith(("async def ", "def "))), len(self.lines))
        return "\n".join(self.lines[i:j])

    def test_ingest_refuses_while_draining(self):
        body = self._body("ingest")
        self.assertIn("drain_seconds_left()", body, "ingest 没有检查排空态")
        self.assertIn("status_code=503", body)
        self.assertIn("Retry-After", body,
                      "拒收却不告诉对方什么时候回来 —— 重试只能靠猜")

    def test_status_polling_is_never_refused(self):
        """只拦新写入。拦住状态查询，在飞的那些就问不到终态，调用方会以为自己
        超时了而重推 —— 那正是 utils/ingest_budget 记的「假 timeout 造出真 409」。
        """
        self.assertNotIn("drain_seconds_left()", self._body("ingest_status"),
                         "排空态拦住了状态查询")

    def test_health_exposes_it(self):
        """发布方要能分清「计数不降是因为还有人在写」和「真的在排空」。"""
        body = self._body("health")
        self.assertIn("draining", body)
        self.assertIn("drain_seconds_left", body)

    def test_the_endpoint_is_registered(self):
        self.assertIn('Route("/api/drain", drain, methods=["POST"])', self.source)

    def test_the_cap_comes_from_the_single_budget_source(self):
        """别再开一个拍脑袋的数：2026-09-20 同一件事上有三个互不相同的常数。"""
        self.assertIn("from utils.ingest_budget import", self.source)


class ReleaseScriptOrderTests(unittest.TestCase):
    def setUp(self):
        out, buf = [], ""
        for raw in RELEASE.read_text("utf-8").splitlines():
            if raw.lstrip().startswith("#"):
                continue
            buf += raw.rstrip()
            if buf.endswith("\\"):
                buf = buf[:-1] + " "
                continue
            out.append(buf.strip()); buf = ""
        self.lines = out

    def _index(self, pred) -> int:
        return next(i for i, x in enumerate(self.lines) if pred(x))

    def test_drain_is_entered_before_the_wait_loop(self):
        """顺序反了等于没做：等的过程中还会有新条目进来，终点仍然到不了。"""
        enter = self._index(lambda x: x.startswith('if set_drain "$DRAIN_BUDGET_S"'))
        loop = self._index(lambda x: x.startswith('while [ "$waited"'))
        self.assertLess(enter, loop, "进入排空态排在等待循环之后")

    def test_failing_to_enter_drain_does_not_claim_success(self):
        """进不去排空态要说出来 —— 否则操作者以为外部写入已经停了。"""
        enter = self._index(lambda x: x.startswith('if set_drain "$DRAIN_BUDGET_S"'))
        window = "\n".join(self.lines[enter:enter + 6])
        self.assertIn("else", window)
        self.assertIn("⚠", window)

    def test_the_exit_path_clears_it(self):
        """中止之后让写入方白等几分钟没有任何好处。截止时间是兜底，不是正常路径。"""
        i = self._index(lambda x: x.startswith("release_exit() {"))
        body = "\n".join(self.lines[i:i + 20])
        self.assertIn("set_drain 0", body, "退出路径没有解除排空态")

    def test_the_token_never_reaches_this_machine(self):
        """令牌只在 NAS 上取用，不回传本机 stdout/日志/命令插值。"""
        i = self._index(lambda x: x.startswith("set_drain() {"))
        body = "\n".join(self.lines[i:i + 16])
        self.assertIn("sed -n 's/^KG_HUB_API_TOKEN=//p'", body)
        # 取到的值必须留在远端那段脚本里：本机侧不得出现赋值给本地变量的写法。
        self.assertNotRegex(body, r"^\s*tok=\$\(ssh", "令牌被取回了本机")


if __name__ == "__main__":
    unittest.main()
