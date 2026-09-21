#!/usr/bin/env python3
"""「够不到线上」不等于「线上是旧版本」，也不等于「没有上一版」。

T-0103。两处都是同一个形状：**空值 / 非零被当成了一个我预料中的情形**，
于是预料之外的失败顺着成功路径走了下去。

- F1 排空循环：`n` 为空有四种成因（旧版本没这字段 / curl 失败 / ssh 失败 /
  JSON 坏了），原来一律报「线上版本还没有 active_extractions」并置 `drained=1`
  继续切换 —— **绕过了「排空不掉就别发」那道闸**，而且操作者被告知了一件假事。
- F4 `PREV`：`|| true` 吃掉 ssh 失败、再 `${PREV:-latest}` 补上，把「首次发布」
  和「这次没读到」压成一件事；后者的后果是自动回滚指向可变标签 `latest` ——
  而可变标签正是这个脚本存在的理由（T-0046）。

判据统一写成：**只有明确的成功才配走成功路径。**
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "deploy/nas/release.sh"


def logical_lines(path: Path) -> list[str]:
    """把续行拼成逻辑行，并去掉注释行。

    按物理行取会把一条跨行命令劈开，断言于是只钉了半句话
    （2026-09-21 在另一条任务上当场假红过一次）。
    """
    out, buf = [], ""
    for raw in path.read_text("utf-8").splitlines():
        if raw.lstrip().startswith("#"):
            continue
        buf += raw.rstrip()
        if buf.endswith("\\"):
            buf = buf[:-1] + " "
            continue
        out.append(buf.strip())
        buf = ""
    return out


class DrainLoopTests(unittest.TestCase):
    """F1：够不到线上时，绝不能置 drained=1。"""

    def setUp(self):
        self.lines = logical_lines(RELEASE)
        self.text = "\n".join(self.lines)

    def test_the_probe_captures_its_own_exit_status(self):
        """不捕获 rc 就无从分辨「够不到」和「够到了但没这字段」。"""
        self.assertTrue(
            any("probe_rc=$?" in x for x in self.lines),
            "探测没有单独捕获退出码，四种成因仍然压成一个空值")

    def test_unreachable_keeps_waiting_instead_of_declaring_drained(self):
        """够不到就继续等；预算耗尽自然落到那条闸上中止。

        钉的是**放行的那一行**：`drained=1` 只允许出现在两处 —— DRY_RUN、
        以及真的拿到了 `0`。够不到那一支必须是 `continue`。
        """
        # 按行取块：logical_lines 已经 strip 掉缩进，靠缩进定位 fi 会找不到
        # （第一版就是这么写的，当场报错）。
        start = next(i for i, x in enumerate(self.lines)
                     if x.startswith('if [ "$probe_rc" != 0 ]'))
        end = next(i for i in range(start + 1, len(self.lines))
                   if self.lines[i] == "fi")
        block = "\n".join(self.lines[start:end + 1])
        self.assertNotIn("drained=1", block,
                         "够不到线上却宣布已排空 —— 这正好绕过「排空不掉就别发」")
        self.assertIn("continue", block)

    def test_the_old_version_branch_only_fires_after_a_successful_probe(self):
        """「线上版本还没有 active_extractions」这句话必须在探测成功之后才说得出口。

        它是一句**关于线上版本**的诊断。探测失败时说它，就是在告诉操作者一件假事。
        """
        probe = self.text.index("probe_rc=$?")
        claim = self.text.index("线上版本还没有 active_extractions")
        self.assertLess(probe, claim, "还没探测成功就断言线上是旧版本")

    def test_the_drain_gate_itself_is_still_there(self):
        """守卫本身要被钉住 —— 上面几条都以它存在为前提。"""
        self.assertIn('if [ "$drained" != 1 ]; then', self.text)
        gate = self.text[self.text.index('if [ "$drained" != 1 ]; then'):]
        self.assertIn("die", gate[:400], "排空不掉却没有中止")


class PreviousTagTests(unittest.TestCase):
    """F4：读不到 ≠ 没有；都不能变成 `latest`。"""

    def setUp(self):
        self.lines = logical_lines(RELEASE)
        self.text = "\n".join(self.lines)

    def test_latest_is_never_used_as_a_fallback_tag(self):
        """`latest` 是可变标签 —— 一 build 就把旧镜像覆盖成悬空层。

        回滚目标指向它，等于回滚到一个不确定的东西，而这个脚本存在的全部理由
        就是不再用可变标签（T-0046）。
        """
        offenders = [x for x in self.lines if re.search(r"PREV[:=]-latest|PREV:-latest", x)]
        self.assertEqual(offenders, [], f"PREV 仍会静默变成 latest：{offenders}")

    def test_failing_to_read_the_tag_aborts_the_release(self):
        """读不到就别发 —— 没有回滚目标的发布不叫可回滚。"""
        start = next(i for i, x in enumerate(self.lines)
                     if x.startswith('if [ "$prev_rc" != 0 ]'))
        end = next(i for i in range(start + 1, len(self.lines))
                   if self.lines[i] == "fi")
        block = self.lines[start:end + 1]
        # 钉「这一支真的会中止」，不是钉「这段话还在」—— 第一版写成
        # `"读不到线上当前标签" in text`，把 die 改成 say 它照样绿。
        self.assertTrue(any(x.startswith("die ") for x in block),
                        f"读标签失败这一支没有中止发布：{block}")

    def test_rollback_refuses_when_there_is_no_previous_tag(self):
        """PREV 为空时照常回滚，会把 .env 写成空标签再 up —— 比不回滚坏得多。"""
        fn = self.text[self.text.index("rollback_to_previous_image() {"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn('-z "${PREV:-}"', fn, "没有回滚目标时仍然会执行回滚")
        self.assertIn("die", fn)

    def test_the_env_backfill_does_not_write_an_empty_tag(self):
        """那个「关掉变量缺失窗口」的兜底只在有当前标签时才做得到。

        写一个空值等于没关窗口：`${KG_HUB_IMAGE_TAG:?}` 把空值也当未设。
        """
        # 钉「兜底那一步的紧邻下一行就是守卫」，不是「附近找得到这串字符」——
        # 第一版写成后者，而 `-z "${PREV:-}"` 在回滚函数里也有一份，
        # 于是把守卫删掉它照样绿。
        i = next(k for k, x in enumerate(self.lines) if "兜住 .env" in x)
        guard = self.lines[i + 1]
        self.assertTrue(guard.startswith('if [ -z "${PREV:-}" ]'),
                        f"兜底那一步没有先判空，首次发布会把空标签写进 .env：{guard}")
        # 对照：守卫之后确实还有真正写 .env 的那一支，否则这条用例没在验东西。
        self.assertTrue(any("KG_HUB_IMAGE_TAG=%s" in x
                            for x in self.lines[i:i + 20]))


class ProbeBehaviourTests(unittest.TestCase):
    """光看源码看不出 shell 会怎么走 —— 用一个假 ssh 真跑那一段。"""

    def run_probe(self, ssh_exit: int, body: str) -> tuple[int, str]:
        """复刻循环里那三行的判定，用真 bash 执行。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ssh"
            fake.write_text(
                "#!/bin/sh\n"
                f"printf '%s' {body!r}\n"
                f"exit {ssh_exit}\n", encoding="utf-8")
            fake.chmod(0o755)
            script = Path(tmp) / "probe.sh"
            script.write_text(
                'set -euo pipefail\n'
                'drained=0\n'
                'set +e\n'
                'body=$(ssh x "curl" 2>/dev/null); probe_rc=$?\n'
                'set -e\n'
                'if [ "$probe_rc" != 0 ]; then echo UNREACHABLE; exit 0; fi\n'
                'n=$(printf "%s" "$body" | python3 -c \'\n'
                'import json,sys\n'
                'try: print(json.load(sys.stdin)["active_extractions"])\n'
                'except Exception: pass\' 2>/dev/null || true)\n'
                'if [ -z "$n" ]; then echo OLD_VERSION; exit 0; fi\n'
                'if [ "$n" = 0 ]; then echo DRAINED; exit 0; fi\n'
                'echo "INFLIGHT=$n"\n', encoding="utf-8")
            done = subprocess.run(
                ["bash", str(script)], capture_output=True, text=True,
                env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
            return done.returncode, done.stdout.strip()


    def test_the_replicated_decision_lines_are_verbatim_from_the_script(self):
        """这组用例跑的是**复刻**的判定，所以必须钉住复刻与原件一致。

        不钉的话它验的是它自己：release.sh 怎么改，这里照样全绿 ——
        「一个函数正确不等于有人在用它」的同款，只是换成了「一份拷贝正确
        不等于原件也这样」。
        """
        script = RELEASE.read_text("utf-8")
        wanted = [
            'probe_rc=$?',
            'if [ "$probe_rc" != 0 ]; then',
            'if [ -z "$n" ]; then',
            'if [ "$n" = 0 ]; then',
            '["active_extractions"]',
        ]
        for line in wanted:
            self.assertIn(line, script,
                          "复刻的判定在 release.sh 里找不到原件：" + line)
    def test_ssh_failure_is_reported_as_unreachable_not_old_version(self):
        _, out = self.run_probe(255, "")
        self.assertEqual(out, "UNREACHABLE",
                         "ssh 挂了却被读成「线上是旧版本」")

    def test_curl_non_2xx_is_unreachable_too(self):
        _, out = self.run_probe(22, "")
        self.assertEqual(out, "UNREACHABLE")

    def test_reached_but_field_absent_is_old_version(self):
        _, out = self.run_probe(0, '{"status":"ok"}')
        self.assertEqual(out, "OLD_VERSION")

    def test_broken_json_from_a_reachable_host_is_old_version_not_unreachable(self):
        """够到了但 JSON 坏了：仍按旧版本盲等，**不**报成够不到。

        这一档在旧写法里和 ssh 失败混在一起；分开之后各自该走哪条要钉住，
        否则下一个人「顺手」把它并回去也没人拦。
        """
        _, out = self.run_probe(0, "not json at all")
        self.assertEqual(out, "OLD_VERSION")

    def test_zero_means_drained(self):
        _, out = self.run_probe(0, '{"active_extractions":0}')
        self.assertEqual(out, "DRAINED")

    def test_a_positive_count_keeps_waiting(self):
        _, out = self.run_probe(0, '{"active_extractions":3}')
        self.assertEqual(out, "INFLIGHT=3")


if __name__ == "__main__":
    unittest.main()
