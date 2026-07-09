"""护栏：tools/experimental/* 不得被任何线上代码导入。

背景：tools/experimental/*（贡献度/自进化）已被认定 ROI 存疑、默认不接排序
（见 tools/experimental/README.md、docs/SELF-EVOLVING.md）。当前线上无任何
引用（server / ingesters / utils / tools 根目录）。本测试是「上锁」——防止
未来有人把它悄悄接回线上。要接回，必须先删本测试并书面说明理由。

判定口径：只匹配真实的 import/from 语句（行首、允许缩进），不误伤注释或
文档字符串里出现的 "experimental" 一词。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 线上代码面（不含 tools/experimental/ 自身）
SCAN_DIRS = ["ingesters", "utils"]
SCAN_FILES = ["kg_hub_server.py", "mcp_server.py"]

# 只抓真实 import：行首(可缩进) from/import ... experimental
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+\S*experimental", re.MULTILINE)


def _live_py_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for d in SCAN_DIRS:
        files += (ROOT / d).rglob("*.py")
    for f in SCAN_FILES:
        p = ROOT / f
        if p.exists():
            files.append(p)
    # tools 根目录（排除 experimental/ 子目录）
    for p in (ROOT / "tools").glob("*.py"):
        files.append(p)
    # 剔除任何位于 experimental 目录内的文件
    return [f for f in files if "experimental" not in f.parts]


def test_no_live_import_of_experimental() -> None:
    offenders = []
    for f in _live_py_files():
        txt = f.read_text(encoding="utf-8", errors="ignore")
        if IMPORT_RE.search(txt):
            offenders.append(str(f.relative_to(ROOT)))
    assert not offenders, (
        "tools/experimental 被线上代码 import（冻结被打破）: "
        + ", ".join(offenders)
        + "。要接回线上须先删除本测试并书面说明理由。"
    )


if __name__ == "__main__":
    # 无 pytest 环境下可直接跑：python tests/test_experimental_frozen.py
    test_no_live_import_of_experimental()
    print("PASS: tools/experimental 无线上引用（冻结护栏在位）")
