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

    def test_the_refusal_path_actually_increments_the_counter(self):
        """钉调用点，不只钉函数。

        变异验证当场发现：把 ingest 里的 `note_drain_refusal()` 换成 `0`，
        上面那些「计数器自己是对的」的用例照样全绿 ——
        **一个函数正确，不等于有人在用它。**
        """
        body = self._body("ingest")
        self.assertIn("note_drain_refusal()", body,
                      "拒收时没有计数，那个数永远是 0，等于没量")

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




class RefusalCountingTests(unittest.TestCase):
    """排空期间挡下了多少次写入 —— T-0117 原本量不到的那个数。"""

    def setUp(self):
        self.m = load_server_symbols()
        self.addCleanup(lambda: self.m.set_drain(0))

    def test_it_counts(self):
        self.m.set_drain(60)
        self.assertEqual(self.m.note_drain_refusal(), 1)
        self.assertEqual(self.m.note_drain_refusal(), 2)
        self.assertEqual(self.m.drain_refused(), 2)

    def test_each_window_starts_from_zero(self):
        """跨窗口累加的话，发布方读到的是「开机以来挡了多少」——
        那回答不了「这次窗口进来多少外部写入」，而后者才是要量的。"""
        self.m.set_drain(60)
        self.m.note_drain_refusal(); self.m.note_drain_refusal()
        self.m.set_drain(0)          # 解除不清零：那一次窗口的结论还要被读走
        self.assertEqual(self.m.drain_refused(), 2)
        self.m.set_drain(60)         # 再次进入才清零
        self.assertEqual(self.m.drain_refused(), 0)


class DrainEvidenceTests(unittest.TestCase):
    """排空窗口要留下能活过容器重建的证据。"""

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
        self.text = "\n".join(out)

    def test_the_log_lives_under_a_dot_directory(self):
        """落错地方会反过来制造漂移。

        `is_allowed_untracked` 只豁免根下的点**目录**（`rest` 非空才返回 True）；
        根上的点**文件**照样被报成「只在 NAS 上，git 未跟踪」——那是这个检查里
        最响的一档，拿它报一个自己刚造的文件，就是在花掉警报的可信度（准则 28）。
        """
        line = next(x for x in self.lines if x.startswith("DRAIN_LOG="))
        path = line.split("=", 1)[1].strip('"')
        rel = path.replace("$SRC/", "")
        self.assertIn("/", rel, f"证据落在根上而不是点目录里：{rel}")
        self.assertTrue(rel.split("/")[0].startswith("."),
                        f"证据不在点目录下，会被报成漂移：{rel}")

    def test_both_outcomes_are_recorded(self):
        """失败的那次窗口比成功的更值得复盘 —— 只记成功等于只留下好消息。"""
        self.assertIn('drain_note "drained waited=', self.text)
        self.assertIn('drain_note "abort waited=', self.text)

    def test_the_drained_line_carries_the_refusal_count(self):
        """记「等了多久」还不够：等得久是因为自己的抽取慢，还是因为外部一直在写，
        是两个不同的病，而它们在「等了 1260s」这一个数字上长得一模一样。"""
        line = next(x for x in self.lines if 'drain_note "drained' in x)
        self.assertIn("refused=", line)

    def test_failing_to_record_does_not_fail_the_release(self):
        """证据没记下是遗憾，不是事故 —— 但必须出声，否则下次复盘时
        「没有记录」会被读成「没有发生」。"""
        i = next(k for k, x in enumerate(self.lines) if x.startswith("drain_note() {"))
        body = "\n".join(self.lines[i:i + 10])
        self.assertIn("⚠", body)
        self.assertNotIn("die", body)

if __name__ == "__main__":
    unittest.main()
