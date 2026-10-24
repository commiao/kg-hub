"""skills/kg-model-eval 与代码对账：frontmatter 与 manifest 一致；skill 里写的子命令都存在；写出来的准入门槛与 THRESHOLDS 一致。

skill 会分发到 qoder / codex / cursor / DSH 等工具，别的会话照着它跑——文档与代码一旦对不上，就是跨会话的误导。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import argparse
import os
import json
import re
import unittest
from unittest import mock

from tools import extract_bench as xb
from tools import extract_campaign as ec
from tools import agent_extract as ax
from tools import extraction_repair as er

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


def flat_commands(main) -> set[str]:
    """Read the actual flat parser choices without invoking API/config actions."""
    seen = set()
    real = argparse.ArgumentParser.add_argument

    def spy(self, *args, **kwargs):
        if args and args[0] in ("cmd", "command"):
            seen.update(kwargs.get("choices", []))
        return real(self, *args, **kwargs)

    with mock.patch.object(argparse.ArgumentParser, "add_argument", spy), \
            mock.patch.object(argparse.ArgumentParser, "parse_known_args", side_effect=SystemExit(0)), \
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

    def test_orchestrator_must_open_in_dedicated_workspace(self):
        """2026-10-24 用户定：战役固定在专用目录跑；从别处触发时到专用目录开新会话，不给触发会话的目录加白。"""
        step0 = self.text[self.text.index("0. **专用目录**"):self.text.index("1. 准入检查")]
        self.assertIn("编排会话必须开在这个固定的专用目录", step0)
        self.assertIn("直接开新会话", step0)
        self.assertIn("不给当前会话目录追加 claude-mem 排除", step0)
        env = {k: v for k, v in os.environ.items() if k != "KG_HUB_CAMPAIGN_WORKSPACE"}
        with mock.patch.dict("os.environ", env, clear=True):
            default = "~/" + str(ec.workspace_dir().relative_to(_Path.home()))
        self.assertIn(f"缺省 `{default}`", step0)                 # 写出来的缺省目录与代码一致
        self.assertIn("KG_HUB_CAMPAIGN_WORKSPACE", step0)

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
        have = {"bench": subcommands(xb.main), "campaign": subcommands(ec.main),
                "agent": flat_commands(ax.main), "repair": flat_commands(er.main)}
        self.assertIn("register", have["bench"])
        reference = (SKILL / "references" / "quality.md").read_text()
        mentioned = set(re.findall(r'\$CLI"\s+(bench|campaign|agent|repair)\s+([a-z-]+)',
                                   self.text + "\n" + reference + "\n" + "\n".join(
                                       p.read_text() for p in (SKILL.parent.parent / "docs" / "executors").glob("*.md"))))
        self.assertTrue(mentioned)
        for tool, sub in sorted(mentioned):
            with self.subTest(tool=tool, sub=sub):
                self.assertIn(sub, have[tool])
        # 步骤里只写子命令名的（`report --id`、`review --batch` 这类）：反引号里「单词 --参数」都当子命令核对，外部命令除外
        external = {"qodercli", "git", "dsh", "codex", "cursor-agent"}
        bare = set(re.findall(r"`([a-z][a-z-]*) --[a-z]", self.text)) - external
        self.assertIn("review", bare)
        for sub in sorted(bare):
            with self.subTest(bare=sub):
                self.assertIn(sub, set().union(*have.values()))

    def test_default_prompt_and_distributed_reference_match_runtime(self):
        prompt = ax.PROMPT_PATH.read_text()
        self.assertIn(f"prompt_version = {ax.PROMPT_VERSION}", prompt)
        self.assertIn(ax.PROMPT_VERSION, self.text)
        reference = SKILL / "references" / "quality.md"
        self.assertTrue(reference.is_file())
        self.assertIn("references/quality.md", self.text)
        for tool in ("claude-code", "codex", "qoder", "cursor", "dsh"):
            doc = SKILL.parent.parent / "docs" / "executors" / f"{tool}.md"
            with self.subTest(tool=tool):
                self.assertIn("_common.md", doc.read_text())
                self.assertIn("quality-check", doc.read_text())

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
        self.assertIn('"$CLI" bench revoke', self.text)

    def test_current_reference_recipe_uses_native_baseline_and_verified_selection(self):
        """Next-session defaults must not spend a candidate run on the legacy v4 reference."""
        recipe = self.text[self.text.index("### 1.1"):self.text.index("## 2. 冒烟")]
        self.assertIn('"$CLI" bench baseline', recipe)
        self.assertIn("--executor claude --models claude-opus-5-5", recipe)
        self.assertIn("--limit 50", recipe)
        self.assertIn("取得用户确认后", recipe)
        for contract in ("reference.prompt_version", "reference.prompt_sha256", "system_prompt_sha256"):
            self.assertIn(contract, recipe)
        for section in ("## 2. 冒烟", "## 3. 准入"):
            content = self.text[self.text.index(section):].split("\n## ", 1)[0]
            commands = re.findall(r'"\$CLI" bench (?:run|admit|blind)[^\n]+', content)
            self.assertTrue(commands)
            self.assertTrue(all('--bench "$V5_BENCH"' in command for command in commands))
        self.assertIn('"$CLI" bench parts --bench "$V5_BENCH"', recipe)
        self.assertTrue((SKILL.parent.parent / "docs/executors/bench-reference.md").is_file())


if __name__ == "__main__":
    unittest.main()
