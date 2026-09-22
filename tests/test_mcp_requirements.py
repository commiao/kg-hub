"""`mcp_server.py` 要装哪几个、什么版本 —— 必须在仓库里，而且与代码一致。

2026-09-22（T-0152）：muxcp 的 kg_hub 后端跑的是开发工作树里的
`spike-graphiti/.venv`，不是发布产物（准则 20）。要把它切到产物，第一个问题就是
「用哪个解释器」—— 而产物是 `git archive` 出来的、带不了 `.venv`。

卡点不在工具，在**没人说得出可信的依赖清单**：那三个版本当时只以散文形式躺在
T-0050 的 body 里（写给 Windows 的接入步骤），仓库里一个钉子都没有。
散文里的规格，换台机器就没人找得到。

这套测试钉两件事：
1. 清单**存在且逐个钉死版本**（`==`，不是 `>=`，不是裸名字）；
2. 清单与代码**同一个来源** —— 按 ast 推导 `mcp_server.py` 的传递 import 图，
   多一个少一个都报红（准则 28：一个判断的两端必须取自同一个源）。

第 2 条必须走 ast 而不是 grep：`python-dotenv` 是 `kg_hub_env.py` 里的**函数内
延迟 import**，grep 顶格的 import 行看不见它 —— 我第一次核实时就漏了，
差点把它当成一个多余的钉子删掉。
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements-mcp.txt"
ENTRYPOINT = "mcp_server.py"

# 导入名 → 发行包名。只有对不上的才需要列；列不出来的一律报红（失败关闭），
# 免得将来新加一个 import 悄悄溜过这条检查。
DISTRIBUTION = {"dotenv": "python-dotenv"}


def third_party_modules() -> set[str]:
    """按 ast 推导入口的**传递** import 图里的三方模块。

    只跟着仓库根上的本地 .py 走（`kg_hub_env` 这种），不进三方包内部。
    """
    seen: set[str] = set()
    queue = [ENTRYPOINT]
    modules: set[str] = set()
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        tree = ast.parse((ROOT / name).read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found = {node.module.split(".")[0]}
            else:
                continue
            for mod in found:
                modules.add(mod)
                local = f"{mod}.py"
                if (ROOT / local).exists():
                    queue.append(local)
    local_names = {m for m in modules if (ROOT / f"{m}.py").exists()}
    return modules - set(sys.stdlib_module_names) - local_names - {"__future__"}


def pinned() -> dict[str, str]:
    """清单里钉住的 {发行包名: 版本}。注释与空行不算。"""
    out: dict[str, str] = {}
    for line in REQUIREMENTS.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"([A-Za-z0-9._-]+)==([0-9][0-9A-Za-z._-]*)", line)
        assert m, f"这一行没有钉死版本: {line!r}"
        out[m.group(1)] = m.group(2)
    return out


class ExistenceTests(unittest.TestCase):
    def test_the_list_is_in_the_repo(self):
        """散文里的规格换台机器就没人找得到 —— 这是 T-0152 卡住的那一步。"""
        self.assertTrue(REQUIREMENTS.exists(), "requirements-mcp.txt 不在仓库里")

    def test_every_line_pins_an_exact_version(self):
        """`>=` 或裸名字装出来的是「当天 PyPI 上恰好是什么」，不是可复现的环境。"""
        text = REQUIREMENTS.read_text("utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            self.assertRegex(line, r"^[A-Za-z0-9._-]+==[0-9]",
                             f"没钉死版本: {line!r}")
        self.assertTrue(pinned(), "清单里一条有效钉子都没有")


class SameSourceTests(unittest.TestCase):
    """清单与代码必须取自同一个源，否则它迟早报一件不存在的事。"""

    def test_every_third_party_import_is_pinned(self):
        missing = []
        for mod in sorted(third_party_modules()):
            dist = DISTRIBUTION.get(mod, mod)
            if dist not in pinned():
                missing.append(f"{mod}（发行包 {dist}）")
        self.assertEqual(missing, [],
                         f"代码 import 了但清单没钉: {missing}")

    def test_nothing_is_pinned_that_the_code_does_not_import(self):
        """多钉一个也是假话 —— 它声称「这是跑起来必需的」，而其实不是。"""
        needed = {DISTRIBUTION.get(m, m) for m in third_party_modules()}
        extra = sorted(set(pinned()) - needed)
        self.assertEqual(extra, [], f"清单钉了代码用不到的: {extra}")

    def test_a_lazy_import_inside_a_function_is_still_seen(self):
        """python-dotenv 是 kg_hub_env.py 函数内的延迟 import。

        走 grep 会漏掉它（我第一次核实时就漏了，差点把这个钉子当多余的删掉），
        所以推导必须走 ast。这条直接钉住那个结果。
        """
        self.assertIn("dotenv", third_party_modules(),
                      "推导没看见函数内的延迟 import —— 大概是退回 grep 了")

    def test_an_unmapped_third_party_module_fails_closed(self):
        """将来新加一个 import 而忘了钉，必须报红而不是被放过。"""
        needed = {DISTRIBUTION.get(m, m) for m in third_party_modules()}
        self.assertTrue(needed, "推导出空集合 —— 那说明推导本身坏了")


class ProvenanceTests(unittest.TestCase):
    def test_the_file_says_why_these_versions(self):
        """钉一个数而不写它从哪来，下一个人只能拍脑袋改。"""
        text = REQUIREMENTS.read_text("utf-8")
        self.assertIn("T-0152", text)
        self.assertIn("spike-graphiti/.venv", text, "没说清钉的是哪一份实测组合")


if __name__ == "__main__":
    unittest.main()
