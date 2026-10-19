"""release.sh 发布前列出「线上标签..目标」之间会一起上线的提交（deploy-standard 准则 43）。

从脚本里取出函数本体真跑（不抄一份副本），在 UTF-8 locale 下跑 —— 准则 31：
中文紧跟裸展开只在 UTF-8 下崩，而人手敲发版正是这个环境。
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "deploy" / "nas" / "release.sh"


def function_source() -> str:
    text = RELEASE.read_text("utf-8")
    m = re.search(r"^list_unreleased_merges\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "release.sh 里找不到 list_unreleased_merges"
    return m.group(0)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class ListUnreleasedMerges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        self.base = self.commit("base")
        self.online = self.merge("feature/a", "#145 Source only; no NAS deployment")
        self.target = self.merge("feature/b", "#147 dashboard")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, msg):
        (self.repo / "f").write_text(msg)
        git(self.repo, "add", "f")
        git(self.repo, "commit", "-q", "-m", msg)
        return git(self.repo, "rev-parse", "--short=12", "HEAD")

    def merge(self, branch, title):
        git(self.repo, "switch", "-q", "-c", branch)
        self.commit(f"work on {branch}")
        git(self.repo, "switch", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", f"Merge pull request {title}", branch)
        return git(self.repo, "rev-parse", "--short=12", "HEAD")

    def run_fn(self, prev, target):
        script = ("set -euo pipefail\nsay() { printf '%s\\n' \"$*\" >&2; }\n"
                  + function_source()
                  + f'list_unreleased_merges "{prev}" "{target}" "{self.repo}"\n')
        env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name, "LC_ALL": "en_US.UTF-8"}
        r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)      # 只摆出来，永远不拦发布
        return r.stderr

    def test_lists_every_first_parent_commit_between_online_and_target(self):
        out = self.run_fn(self.base, self.target)
        self.assertIn("first-parent 共 2 笔", out)
        self.assertIn("#145 Source only; no NAS deployment", out)
        self.assertIn("#147 dashboard", out)
        self.assertNotIn("work on feature", out)          # 只看主干那条线，不展开分支内部提交
        self.assertIn("准则 43", out)

    def test_nothing_new_when_target_is_online(self):
        out = self.run_fn(self.target, self.target)
        self.assertIn("first-parent 共 0 笔", out)

    def test_first_release_has_nothing_to_compare(self):
        self.assertIn("首次发布", self.run_fn("", self.target))

    def test_unknown_online_tag_is_said_not_guessed(self):
        out = self.run_fn("deadbeefcafe", self.target)
        self.assertIn("找不到对应提交", out)
        self.assertNotIn("共 ", out)

    def test_rollback_or_divergence_is_flagged(self):
        # 目标不包含线上版本：线上有、目标没有的改动会被撤下。
        out = self.run_fn(self.target, self.base)
        self.assertIn("不包含线上的", out)

    def test_release_mode_calls_it_after_reading_the_online_tag(self):
        text = RELEASE.read_text("utf-8")
        call = text.index('list_unreleased_merges "$PREV" "$SHA" "$REPO"')
        self.assertGreater(call, text.index('say "当前线上标签：$PREV"'))
        self.assertLess(call, text.index('say "[1/6] git archive'))   # 在动任何东西之前


if __name__ == "__main__":
    unittest.main()
