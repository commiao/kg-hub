#!/usr/bin/env python3
"""准则 31 的机器检查：shell 脚本里不许有紧跟中文标点的裸展开。

2026-09-20 同一形状在两个仓库的发布脚本上各咬一次。本仓库那次：

    say "  ✗ $label：机器上的与仓库里的不一致"      deploy/mac/install.sh
    → line 116: label\xef: unbound variable

非 UTF-8 locale 下 bash 把全角标点的首字节吃进变量名，`set -u` 当场退出。
三个条件缺一不可：中文提示 + set -u + 非 UTF-8 locale —— 人在终端里跑不会暴露
（locale 是 UTF-8），launchd / cron / CI 里才炸。

**最坏的是它藏在失败路径上。** install.sh 那几行只在「机器上的与仓库里的不一致」
时才执行；一致时永远碰不到，等真出现不一致，本该说清哪里不一致的那句话自己先崩了。

无 `set -u` 的脚本今天不崩，但同样要改：它们跑在 cron 里，将来谁加一行 `set -u`
就变成定时炸弹。
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# `$(cmd)` 与 `${x[@]}` 本身有界，不会被命中；只找裸的 $name 紧跟非 ASCII。
BARE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]")


def shell_scripts() -> list[Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.sh"],
                         capture_output=True, text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line.strip()]


class CJKAdjacentExpansionTests(unittest.TestCase):
    def test_there_are_scripts_to_check(self):
        """别让这条检查在文件被挪走之后变成永远通过的空断言。"""
        self.assertTrue(shell_scripts())

    def test_no_bare_expansion_touches_a_non_ascii_character(self):
        offenders = []
        for path in shell_scripts():
            for number, line in enumerate(
                    path.read_text("utf-8").splitlines(), start=1):
                # 注释里允许出现反面示例，改掉它注释本身就没意义了。
                if line.lstrip().startswith("#"):
                    continue
                if BARE.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "紧跟非 ASCII 的裸展开：\n" + "\n".join(offenders))

    def test_the_install_failure_branch_survives_a_stripped_locale(self):
        """上一条钉写法，这条钉**行为** —— 光看源码看不出 bash 会怎么解析。

        `--check <不存在的 label>` 会走到「仓库里有、机器上没装」那句中文提示，
        正是出事的那条路径。
        """
        done = subprocess.run(
            ["bash", str(ROOT / "deploy/mac/install.sh"), "--check",
             "com.kg-hub.does-not-exist"],
            capture_output=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C"})
        combined = (done.stdout + done.stderr).decode("utf-8", "replace")
        self.assertNotIn("unbound variable", combined)
        self.assertNotIn("�", combined, "输出里有解不出来的字节")


if __name__ == "__main__":
    unittest.main()
