#!/usr/bin/env python3
"""准则 31 的机器检查：shell 脚本里不许有紧跟中文标点的裸展开。

2026-09-20 同一形状在两个仓库的发布脚本上各咬一次。本仓库那次：

    say "  ✗ $label：机器上的与仓库里的不一致"      deploy/mac/install.sh
    → line 116: label\xef: unbound variable

**UTF-8** locale 下 bash 3.2 把全角标点的首字节吃进变量名，`set -u` 当场退出。
三个条件缺一不可：中文提示 + set -u + **UTF-8 的 LC_CTYPE**。

⚠️ 本文件初版把这个条件写反了（写成「非 UTF-8 下才炸，launchd/cron/CI 里才炸」），
2026-09-21 实测纠正（deploy-standard 准则 31，fleet-ops `77247d0`）：

    三个变量皆未设 / LANG=C / LC_ALL=C / LC_CTYPE=C   正常
    LC_CTYPE=UTF-8 / LANG=*.UTF-8 / LC_ALL=*.UTF-8    崩

决定性的是 LC_CTYPE。所以最危险的是**人在终端里亲手发版**（locale 是 UTF-8），
而 launchd / cron（无 LANG）反而不会崩 —— 和原来的说法正好相反。

**最坏的是它藏在失败路径上。** install.sh 那几行只在「机器上的与仓库里的不一致」
时才执行；一致时永远碰不到，等真出现不一致，本该说清哪里不一致的那句话自己先崩了。

无 `set -u` 的脚本今天不崩，但同样要改：它们跑在 cron 里，将来谁加一行 `set -u`
就变成定时炸弹。
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
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

    def test_the_install_failure_branch_survives_a_utf8_locale(self):
        """上一条钉写法，这条钉**行为** —— 光看源码看不出 bash 会怎么解析。

        本用例初版是**双重落空**的，2026-09-21 变异验证当场证明它拦不住原始缺陷：

        1. 它用 `LANG=C`。而 bash 3.2 恰恰只在 **UTF-8** 的 ctype 下才把多字节
           首字节吃进变量名 —— `LANG=C` 下怎么写都不会崩，测了等于没测。
        2. 它传 `--check com.kg-hub.does-not-exist`。install.sh 把多余参数当
           label 过滤器，**一个都匹配不上就一条都不检查**，输出是「机器上的服务
           定义与仓库一致」—— 那条中文提示根本没被执行。

        现在的做法：`HOME` 指向空临时目录 ⇒ `TARGET="$HOME/Library/LaunchAgents"`
        落空 ⇒ 每个 label 都走进「仓库里有，机器上没装」那一行；locale 用会触发的
        `LC_ALL=en_US.UTF-8`。

        变异验证（把 `${label}` 退回 `$label`）：
            LANG=C             仍然正常   ← 旧用例就是死在这
            LC_ALL=UTF-8       line 145: label\xef: unbound variable
        """
        with tempfile.TemporaryDirectory() as home:
            done = subprocess.run(
                ["bash", str(ROOT / "deploy/mac/install.sh"), "--check"],
                capture_output=True, timeout=120,
                env={"PATH": "/usr/bin:/bin", "HOME": home,
                     "LC_ALL": "en_US.UTF-8"})
        combined = (done.stdout + done.stderr).decode("utf-8", "replace")
        # 先证明这一跑真的走到了那条失败路径 —— 否则下面两条断言是空的。
        self.assertIn("仓库里有，机器上没装", combined,
                      "没走到失败路径，这条行为检查又变空了：\n" + combined)
        self.assertNotIn("unbound variable", combined)
        self.assertNotIn("\ufffd", combined, "输出里有解不出来的字节")


if __name__ == "__main__":
    unittest.main()
