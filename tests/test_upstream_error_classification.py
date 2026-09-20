"""网关回 5xx 时，失败不许算在观测头上。

2026-09-20 夜实测的那条链（T-0018）：

    网关 503「幂等终态写入尚未恢复，已拒绝付费请求」
      → classify_extract_error 认不出这句话 → error_kind = NULL
      → 不满足「网关侧错误 1h 即清」 → 落进 24h 通用清理
      → 之后 24h 内每次重推都撞 409 → refinery 指数退避
      → 一次网关抖动停用 74 条观测整整一天

这是同一个病的第三次发作，前两次的修法都是**再加一条字符串匹配**，所以换一句
文案就又漏了。这里钉住的是判据本身：按状态码判，不按文案判（准则 22）。

配套钉住另外两件事，缺任何一件这个修复都会变成新的坑：
1. refinery 必须**整窗停发**。1h 快清 + 不停发 = 网关坏着的时候每小时把积压重推
   一遍，而 503 打在 resolve_extracted_edges，那时约 20 次调用已经花出去了。
2. 停发必须**可见**。09-16 那次「盘温 58 卡在告警盲区、积压六天零告警」的教训是
   门控加了而报警对象没跟上；再加一道静默的停发闸就是重蹈覆辙。
"""
from __future__ import annotations

import ast
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kg_refinery as refinery  # noqa: E402
import topology  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = (ROOT / "kg_hub_server.py").read_text("utf-8")
REFINERY_SRC = (ROOT / "kg_refinery.py").read_text("utf-8")

# 真实文案，取自线上 IngestedKey.error_message（82 条 error 键里的 74 条）。
GATEWAY_503_TEXT = (
    "Error code: 503 - {'type': 'error', 'error': {'type': 'api_error', "
    "'message': '幂等终态写入尚未恢复，已拒绝付费请求'}, 'request_id': 'req_x'}"
)


def _load_classifier():
    """只把 classify_extract_error 摘出来执行。

    kg_hub_server 整个导不进来（本机没有 graphiti_core），而**这个函数的行为**正是
    要验的东西——只比对源码字符串的话，加一条 return 就能骗过去。
    """
    tree = ast.parse(SERVER_SRC)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "classify_extract_error")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<classifier>", "exec"), ns)
    return ns["classify_extract_error"]


class _Err(Exception):
    """带 status_code 的假异常；类名可改，用来模拟不同 SDK 异常。"""

    def __init__(self, text: str = "", status_code: int | None = None, name: str | None = None):
        super().__init__(text)
        if status_code is not None:
            self.status_code = status_code
        if name:
            self.__class__ = type(name, (_Err,), {})


class ClassifyByStatusCodeTests(unittest.TestCase):
    def setUp(self):
        self.classify = _load_classifier()

    def test_the_real_503_is_no_longer_unclassified(self):
        """线上那 74 条的原文。修之前它返回 None，于是被锁 24h。"""
        exc = _Err(GATEWAY_503_TEXT, status_code=503, name="InternalServerError")
        self.assertEqual(self.classify(exc), "upstream_error")

    def test_any_5xx_counts_even_with_an_unseen_message(self):
        """判据是状态码，所以下一句没见过的文案不必再改代码。"""
        for code in (500, 502, 503, 504, 599):
            exc = _Err("完全没见过的一句话", status_code=code, name="InternalServerError")
            self.assertEqual(self.classify(exc), "upstream_error", f"HTTP {code}")

    def test_4xx_and_plain_failures_stay_out_of_it(self):
        """4xx 与本地异常可能真是这条观测的问题，不该蹭 1h 快清。"""
        self.assertIsNone(self.classify(_Err("bad request", status_code=400,
                                             name="BadRequestError")))
        self.assertIsNone(self.classify(ValueError("Could not extract JSON")))

    def test_existing_classes_are_unchanged(self):
        """这次只补兜底，前面几条更具体的判据必须原样保留。"""
        self.assertEqual(self.classify(_Err("", status_code=429)), "quota_exhausted")
        self.assertEqual(self.classify(_Err("", name="RateLimitError")), "rate_limited")
        self.assertEqual(self.classify(_Err("", name="BreakerOpen")), "breaker_open")
        self.assertEqual(self.classify(_Err("", name="APIConnectionError")),
                         "gateway_unavailable")

    def test_unreachable_gateway_is_not_relabelled_as_a_5xx(self):
        """「够不着网关」和「网关回了 5xx」的运维动作不同。

        把后者报成前者，值班的第一反应是去重启网关——而那正是准则 29 说的、会制造
        孤儿付费记录的动作。所以哪怕它也是 503，更具体的那条判据必须先赢。
        """
        exc = _Err("网关本地配置不可用", status_code=503, name="InternalServerError")
        self.assertEqual(self.classify(exc), "gateway_unavailable")


class OneHourCleanupTests(unittest.TestCase):
    def test_upstream_error_joins_the_one_hour_sweep(self):
        """标对了类却不进快清名单，等于没修——键照样躺 24h。"""
        sweep = SERVER_SRC.split("quota_threshold = ", 1)[1][:1200]
        self.assertIn("'upstream_error'", sweep,
                      "1h 快清的 error_kind 名单里没有 upstream_error")


class RefineryReactionTests(unittest.TestCase):
    """1h 快清必须配上停发，否则网关坏着时它变成每小时烧一遍积压。"""

    def test_poll_maps_the_kind_to_its_own_status(self):
        calls = []

        def fake_http(method, url, body=None, timeout=30):
            calls.append(url)
            return 200, {"status": "error", "error_kind": "upstream_error"}

        original = refinery._http
        refinery._http = fake_http
        try:
            st = asyncio.run(refinery.poll_until_done("claude-mem", "42"))
        finally:
            refinery._http = original
        self.assertEqual(st, "upstream_error")
        self.assertEqual(len(calls), 1, "认出来就该立刻返回，不该继续轮询到 600s")

    def test_the_rest_of_the_batch_is_not_sent(self):
        """halt 名单漏了它，整批剩余条目会继续逐条撞同一个 5xx。"""
        body = REFINERY_SRC.split("halt[\"stop\"] = True", 1)[0][-400:]
        self.assertIn('"upstream_error"', body,
                      "upstream_error 不在 halt 名单里——同批剩余会继续发")

    def test_the_pause_covers_the_server_side_sweep(self):
        """服务端 1h 清键；暂停短于它，下一次探测撞到的还是同一把 error 键的 409。"""
        self.assertGreaterEqual(
            refinery.RATE_LIMIT_PAUSE_CYCLES * refinery.INTERVAL, 3600,
            "暂停时长没盖住服务端的 1 小时清理窗口")


class HaltIsVisibleTests(unittest.TestCase):
    def verdict(self, **status):
        base = {"backlog_window_open": True, "backlog_remaining": 7421,
                "disk_temp": 49, "ts": "2026-09-20T16:16:21+00:00"}
        base.update(status)
        return topology.refinery_halt(base)

    def test_upstream_pause_is_reported_as_a_stall(self):
        v = self.verdict(upstream_error_paused=True)
        self.assertTrue(v["halted"], "上游 5xx 停发必须算停工，否则又是一次静默停工")
        self.assertIn("上游 5xx 停发", v["gates"])
        self.assertFalse(v["deliberate"], "这不是运维主动断开")

    def test_outside_the_window_it_is_still_not_a_stall(self):
        v = self.verdict(upstream_error_paused=True, backlog_window_open=False)
        self.assertFalse(v["halted"], "窗口外不干活是设计，误报会让告警被无视")

    def test_refinery_writes_the_field_the_alarm_reads(self):
        """判决方读 upstream_error_paused，产出方就必须写它，两端同名才算接上线。"""
        self.assertIn("upstream_error_paused=pause_reason == \"upstream_error\"",
                      REFINERY_SRC, "暂停时没写 upstream_error_paused")
        self.assertIn("upstream_error_paused=False", REFINERY_SRC,
                      "恢复后没把 upstream_error_paused 清回 False——告警会一直亮")


if __name__ == "__main__":
    unittest.main()
