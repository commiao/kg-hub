"""skills/kg-model-eval 与代码对账：frontmatter 与 manifest 一致；skill 里写的子命令都存在；写出来的准入门槛与 THRESHOLDS 一致。

skill 会分发到 qoder / codex / cursor / DSH 等工具，别的会话照着它跑——文档与代码一旦对不上，就是跨会话的误导。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import argparse
import json
import re
import unittest
from unittest import mock

from tools import extract_bench as xb
from tools import extract_campaign as ec

SKILL = _Path(__file__).resolve().parent.parent / "skills" / "kg-model-eval"


def subcommands(main) -> set[str]:
    seen = set()
    real = argparse.ArgumentParser.add_subparsers

    def spy(self, *a, **kw):
        sp = real(self, *a, **kw)
        orig = sp.add_parser

        def add_parser(name, *x, **y):
            seen.add(name)
            return orig(name, *x, **y)
        sp.add_parser = add_parser
        return sp
    with mock.patch.object(argparse.ArgumentParser, "add_subparsers", spy), \
            mock.patch.object(argparse.ArgumentParser, "parse_args", side_effect=SystemExit(0)):
        try:
            main([])
        except SystemExit:
            pass
    return seen


class SkillDocTest(unittest.TestCase):
    def setUp(self):
        self.text = (SKILL / "SKILL.md").read_text()
        self.manifest = json.loads((SKILL / "manifest.json").read_text())

    def test_frontmatter_matches_manifest(self):
        fm = re.match(r"^---\n(.*?)\n---\n", self.text, re.S).group(1)
        meta = dict(line.split(": ", 1) for line in fm.splitlines())
        self.assertEqual(meta["name"], self.manifest["name"])
        self.assertEqual(meta["name"], self.manifest["skill_id"])
        self.assertEqual(meta["description"], self.manifest["description"])
        for tool in ("qoder", "codex", "cursor", "claude-code", "deepseek-harness"):
            self.assertIn(tool, self.manifest["targets"])
        self.assertFalse(self.manifest["security"]["contains_secrets"])

    def test_trigger_is_the_fixed_prefix(self):
        """2026-10-23 用户定：只认「使用kg-hub对比模型」开头；不带前缀的宽泛说法会在日常讨论 kg-hub 时误触发。"""
        prefix = "使用kg-hub对比模型"
        desc = self.manifest["description"]
        self.assertIn(f"只在用户以「{prefix}」开头时使用", desc)
        for loose in ("kg-hub 盲评", "kg-hub 模型对比", "kg-hub 准入", "使用kg-hub盲测", "使用kg-hub对比 A"):
            with self.subTest(loose=loose):
                self.assertNotIn(loose, desc)
        examples = re.findall(rf"「({prefix}\+[^」]+)」", desc)
        self.assertEqual(len(examples), 4)
        for mode, ex in zip(("冒烟", "准入", "盲测", "战役"), examples):
            with self.subTest(mode=mode):
                self.assertIn(f"| 「{ex}」", self.text)                  # 描述里的例子在意图表里都有对应行
                self.assertIn(f"**{mode}**", self.text)

    def test_every_mentioned_subcommand_exists(self):
        have = {"extract_bench": subcommands(xb.main), "extract_campaign": subcommands(ec.main)}
        self.assertIn("register", have["extract_bench"])
        mentioned = set(re.findall(r"tools\.(extract_bench|extract_campaign)\s+([a-z]+)", self.text))
        self.assertTrue(mentioned)
        for tool, sub in sorted(mentioned):
            with self.subTest(tool=tool, sub=sub):
                self.assertIn(sub, have[tool])
        # 步骤里只写子命令名的（`report --id`、`review --batch` 这类）：反引号里「单词 --参数」都当子命令核对，外部命令除外
        external = {"qodercli", "git", "dsh", "codex"}
        bare = set(re.findall(r"`([a-z]+) --[a-z]", self.text)) - external
        self.assertIn("review", bare)
        for sub in sorted(bare):
            with self.subTest(bare=sub):
                self.assertIn(sub, have["extract_campaign"] | have["extract_bench"])

    def test_thresholds_written_in_skill_match_code(self):
        t = xb.THRESHOLDS
        self.assertIn(f"照抄标题、译文边 ≥{t['headline_verbatim_pct']:.0f}%", self.text)
        self.assertEqual(t["headline_verbatim_pct"], t["second_other_lang_pct"])
        self.assertIn(f"中英问句命中各 ≥{t['hit_zh_pct']:.0f}%", self.text)
        self.assertEqual(t["hit_zh_pct"], t["hit_en_pct"])
        self.assertIn(f"不低 {t['subset_drop_pts']:.0f} 点以上", self.text)
        self.assertIn(f"盲评 {xb.BLIND_N} 条错误 ≤{t['blind_max_errors']}", self.text)
        self.assertIn(f"≥{xb.ADMISSION_MIN_ITEMS} 条", self.text)
        self.assertIn(f"空观测率不比基线高出 {t['empty_extra_pts']:.0f} 点以上", self.text)
        self.assertIn("extract_bench revoke", self.text)


if __name__ == "__main__":
    unittest.main()
