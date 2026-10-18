"""tests/run_all.sh 选解释器：选不到能导入项目依赖的就停，不许静默退回。

2026-10-18 实测：本机仓库内 venv 已不存在，原写法静默退回系统 python3（没有
graphiti_core），125 个文件红 18 个；换成有依赖的解释器后只剩 2 个 —— 16 个是环境
造成的，真正的 2 条红埋在里面。退出码就是失败文件数，发布门读的正是它。

每条用例在临时目录里搭一个最小仓库：只有 run_all.sh 的拷贝和一条什么都不做的用例，
解释器全是假的，HOME、PATH 都指进沙箱 —— 用例的安全不能寄托在被测代码上
（deploy-standard 准则 17）。

假解释器有两种：「好的」导入探针成功；「坏的」导入探针失败，**但照样能把用例跑绿**。
后一点是故意的：探针一旦被删，坏解释器会把用例跑成「全部通过」，这几条当场转红。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_ALL = ROOT / "tests" / "run_all.sh"
INSTALL = ROOT / "deploy" / "mac" / "install.sh"

GOOD = """#!/bin/sh
echo "$0 $*" >> "{log}"
[ "$1" = "-c" ] && exit 0
echo OK
"""
BAD = """#!/bin/sh
echo "$0 $*" >> "{log}"
if [ "$1" = "-c" ]; then
    echo "ModuleNotFoundError: No module named 'graphiti_core'" >&2
    exit 1
fi
echo OK
"""


class Sandbox:
    def __init__(self, testcase: unittest.TestCase):
        self.tmp = Path(tempfile.mkdtemp(prefix="run-all-"))
        testcase.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "tests").mkdir(parents=True)
        shutil.copy(RUN_ALL, self.repo / "tests" / "run_all.sh")
        (self.repo / "tests" / "test_dummy.py").write_text("print('OK')\n")
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        # 系统 python3 默认是坏的：这正是 2026-10-18 这台机器的样子。
        self.stub(self.bin / "python3", BAD)

    def stub(self, path: Path, body: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.format(log=self.log))
        path.chmod(0o755)
        return path

    @property
    def mac_venv_python(self) -> Path:
        return self.home / ".local" / "share" / "kg-hub" / "venv" / "bin" / "python"

    @property
    def repo_venv_python(self) -> Path:
        return self.repo / "spike-graphiti" / ".venv" / "bin" / "python"

    def run(self, **env) -> subprocess.CompletedProcess:
        # UTF-8 是会让「裸展开紧跟中文」出事的那个 locale（准则 31）；用 C 跑等于没测。
        base = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
                "LC_ALL": "en_US.UTF-8"}
        return subprocess.run(["sh", "tests/run_all.sh"], cwd=self.repo,
                              env={**base, **env}, capture_output=True, text=True,
                              timeout=60)

    def interpreters_that_ran_tests(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line.split()[0] for line in self.log.read_text().splitlines()
                if "test_dummy.py" in line]


class InterpreterSelectionTests(unittest.TestCase):

    def test_no_usable_interpreter_stops_before_running_any_test(self):
        box = Sandbox(self)
        done = box.run()
        self.assertEqual(done.returncode, 125, done.stdout)
        self.assertIn("导入不了 graphiti_core", done.stdout)
        self.assertIn("未运行任何用例", done.stdout)
        self.assertNotIn("全部通过", done.stdout)
        # 这句提示里的路径紧挨着全角顿号。UTF-8 下写成裸展开，bash 会把顿号首字节吞进
        # 变量名，路径静默消失 —— 本条首版就这么写过，被 test_shell_cjk_expansion 拦下。
        self.assertIn(f"{box.home}/.local/share/kg-hub/venv、python3", done.stdout)
        self.assertEqual(box.interpreters_that_ran_tests(), [],
                         "导入探针失败后还跑了用例 —— 那就是原来那个混着环境错的数")

    def test_explicit_interpreter_is_read_before_kg_hub_vars_are_scrubbed(self):
        box = Sandbox(self)
        good = box.stub(box.tmp / "custom" / "python", GOOD)
        done = box.run(KG_HUB_TEST_PYTHON=str(good))
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(done.stdout.splitlines()[0], f"解释器: {good}")
        self.assertEqual(box.interpreters_that_ran_tests(), [str(good)])

    def test_repo_venv_is_preferred_over_the_mac_job_venv(self):
        box = Sandbox(self)
        box.stub(box.repo_venv_python, GOOD)
        box.stub(box.mac_venv_python, GOOD)
        done = box.run()
        self.assertEqual(done.returncode, 0, done.stdout)
        ran = box.interpreters_that_ran_tests()
        self.assertEqual(len(ran), 1, ran)
        self.assertTrue(ran[0].endswith("spike-graphiti/.venv/bin/python"), ran)

    def test_mac_job_venv_is_used_when_the_repo_has_none(self):
        """2026-10-18 这台机器的情形：仓库内 venv 没了，作业用的那个还在。"""
        box = Sandbox(self)
        box.stub(box.mac_venv_python, GOOD)
        done = box.run()
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(box.interpreters_that_ran_tests(), [str(box.mac_venv_python)])

    def test_kg_hub_venv_override_is_read_before_scrubbing(self):
        box = Sandbox(self)
        alt = box.tmp / "alt-venv"
        box.stub(alt / "bin" / "python", GOOD)
        done = box.run(KG_HUB_VENV=str(alt))
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(box.interpreters_that_ran_tests(), [str(alt / "bin" / "python")])

    def test_a_broken_repo_venv_is_not_papered_over_by_the_next_candidate(self):
        box = Sandbox(self)
        box.stub(box.repo_venv_python, BAD)
        box.stub(box.mac_venv_python, GOOD)
        done = box.run()
        self.assertEqual(done.returncode, 125, done.stdout)
        self.assertIn("spike-graphiti/.venv/bin/python", done.stdout.splitlines()[0])
        self.assertEqual(box.interpreters_that_ran_tests(), [],
                         "坏掉的 venv 被下一个候选盖住了，没人会知道它坏了")

    def test_mac_venv_default_matches_the_install_script(self):
        """同一个默认值写在两处，必须同源（准则 28）：一边改了另一边不跟，测试就跑错解释器。"""
        ours = re.search(r'KG_HUB_VENV:-([^}]+)\}', RUN_ALL.read_text())
        theirs = re.search(r'VENV="\$\{KG_HUB_VENV:-([^}]+)\}"', INSTALL.read_text())
        self.assertIsNotNone(ours, "run_all.sh 里找不到 KG_HUB_VENV 的默认值")
        self.assertIsNotNone(theirs, "install.sh 里找不到 KG_HUB_VENV 的默认值")
        self.assertEqual(ours.group(1), theirs.group(1))


if __name__ == "__main__":
    unittest.main()
