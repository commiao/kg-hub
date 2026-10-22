"""PUSH hook 的项目关键词：worktree / 工具目录要回落到真实项目名，认不出就不注入。

2026-10-22 统计 data/.push_hook.log（10-21 起）注入成功记录，约 15% 关键词取错：
- cwd=~/.claude → kw=.claude（7 次）
- cwd=~/Documents/Qoder/<日期>/<哈希> → kw=aa532fee（5 次）
- cwd=~/workspace/libtv-m 首查没命中 → 回落上级 kw=workspace（2 次，注入 kg-hub DESIGN）
Codex / Cursor 从 stdin 拿 cwd，会落在 ~/.codex/worktrees/<id>/<仓库> 或
<仓库>/.claude/worktrees/<名字>，旧逻辑取到 worktree 名 / id。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import kg_push_hook as H
from tools.kg_push_hook import derive_project_keywords

HAVE_GIT = shutil.which("git") is not None


def _git(*args, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        # 沙箱本身在系统临时目录里，换成沙箱内的假临时根，其余路径才算「不在临时目录」
        self.tmp_root = self.root / "tmp-root"
        patcher = mock.patch.object(H, "_TEMP_ROOTS", (self.tmp_root,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def mk(self, rel):
        d = self.root / rel
        d.mkdir(parents=True, exist_ok=True)
        return d


class Layout(Base):
    """不起 git 也认得出的 worktree 形态（目录已被删、git 不在时也成立）。"""

    def test_claude_worktree(self):
        d = self.root / "kg-hub" / ".claude" / "worktrees" / "affectionate-hypatia-68fd16"
        self.assertEqual(derive_project_keywords(str(d)), ["kg-hub"])

    def test_claude_worktree_subdir(self):
        d = self.root / "kg-hub" / ".claude" / "worktrees" / "brave-noether" / "tools"
        self.assertEqual(derive_project_keywords(str(d)), ["kg-hub"])

    def test_codex_worktree(self):
        d = self.root / ".codex" / "worktrees" / "3293" / "sd-server"
        self.assertEqual(derive_project_keywords(str(d)), ["sd-server"])

    def test_codex_worktree_subdir(self):
        d = self.root / ".codex" / "worktrees" / "bef1" / "sd-server" / "src" / "main"
        self.assertEqual(derive_project_keywords(str(d)), ["sd-server"])

    def test_codex_worktree_id_only_is_not_a_project(self):
        # ~/.codex/worktrees 下有带名字的 id（如 actionable-review-repair），旧逻辑会拿它当项目
        d = self.root / ".codex" / "worktrees" / "actionable-review-repair"
        self.assertEqual(derive_project_keywords(str(d)), [])

    def test_claude_worktrees_dir_itself_falls_through(self):
        # .claude/worktrees 本身不是 worktree；落到旧逻辑，两段都被过滤
        d = self.root / "kg-hub" / ".claude" / "worktrees"
        with mock.patch.object(H, "_project_from_git", return_value=None):
            self.assertEqual(derive_project_keywords(str(d)), [])

    def test_layout_wins_without_git(self):
        d = self.root / "kg-hub" / ".claude" / "worktrees" / "x"
        with mock.patch.object(H.subprocess, "run", side_effect=AssertionError("不该起 git")):
            self.assertEqual(derive_project_keywords(str(d)), ["kg-hub"])


@unittest.skipUnless(HAVE_GIT, "需要 git")
class Git(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.mk("workspace/libtv-m")
        _git("init", "-q", cwd=self.repo)
        _git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)

    def test_repo_root_drops_container_parent(self):
        # 旧逻辑：['libtv-m', 'workspace']，首查没命中就拿 workspace 去检索
        self.assertEqual(derive_project_keywords(str(self.repo)), ["libtv-m"])

    def test_repo_inside_temp_dir_still_named(self):
        # 临时目录只拦认不出仓库的那一档；/tmp 下的克隆、worktree 照样按仓库名认
        with mock.patch.object(H, "_TEMP_ROOTS", (self.root,)):
            self.assertEqual(derive_project_keywords(str(self.repo)), ["libtv-m"])

    def test_repo_subdir(self):
        d = self.repo / "app" / "screens"
        d.mkdir(parents=True)
        self.assertEqual(derive_project_keywords(str(d)), ["libtv-m"])

    def test_worktree_elsewhere_maps_to_main_checkout(self):
        # 任意位置的 worktree（如 Cursor 的 ~/.cursor/worktrees/<仓库>/<名字>）靠 --git-common-dir 回主检出
        wt = self.root / "elsewhere" / "feature-xyz-9f3a"
        _git("worktree", "add", "-q", str(wt), cwd=self.repo)
        self.assertEqual(derive_project_keywords(str(wt)), ["libtv-m"])

    def test_short_repo_name_gives_nothing_not_parent(self):
        repo = self.mk("workspace_codex/api")
        _git("init", "-q", cwd=repo)
        self.assertEqual(derive_project_keywords(str(repo)), [])

    def test_hidden_repo_gives_nothing(self):
        # ~/.claude 若被做成 git 仓库，也不拿 .claude 检索
        repo = self.mk(".claude")
        _git("init", "-q", cwd=repo)
        self.assertEqual(derive_project_keywords(str(repo)), [])

    def test_ignores_inherited_git_dir(self):
        other = self.mk("other-repo")
        _git("init", "-q", cwd=other)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            self.assertEqual(derive_project_keywords(str(self.repo)), ["libtv-m"])

    def test_git_timeout_falls_back_silently(self):
        boom = subprocess.TimeoutExpired(cmd="git", timeout=H.GIT_TIMEOUT_SEC)
        with mock.patch.object(H.subprocess, "run", side_effect=boom):
            self.assertEqual(derive_project_keywords(str(self.repo)), ["libtv-m"])

    def test_git_missing_falls_back_silently(self):
        with mock.patch.object(H.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(derive_project_keywords(str(self.repo)), ["libtv-m"])


class Fallback(Base):
    """认不出项目（非 git 目录）时的旧逻辑 + 过滤。"""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(H, "_project_from_git", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_session_dir_under_temp_root(self):
        # 2026-10-22 实测：scratchpad 取到关键词 scratchpad
        d = self.mk("tmp-root/claude-501/-Users-mac-work-ai-kg-hub/e287560d-e4aa-4b74-95b4-9a93b92b76f7/scratchpad")
        self.assertEqual(derive_project_keywords(str(d)), [])
        self.assertEqual(derive_project_keywords(str(self.mk("tmp-root/release-check"))), [])

    def test_tool_config_dir(self):
        # 真实位置是 ~/.claude，上级 mac 是系统目录
        self.assertEqual(derive_project_keywords(str(self.mk("Users/mac/.claude"))), [])
        self.assertEqual(derive_project_keywords(str(self.mk("Users/mac/.codex"))), [])

    def test_qoder_session_dir(self):
        d = self.mk("Documents/Qoder/2026-10-22/aa532fee")
        self.assertEqual(derive_project_keywords(str(d)), [])

    def test_uuid_dir(self):
        d = self.mk("Library/5e0536e4-2c6d-4cb8-8604-76c36a6b2ad3")
        self.assertEqual(derive_project_keywords(str(d)), [])

    def test_hex_word_without_digit_is_kept(self):
        d = self.mk("Documents/facade")
        self.assertEqual(derive_project_keywords(str(d)), ["facade"])

    def test_container_parent_dropped(self):
        d = self.mk("workspace/libtv-enterprise")
        self.assertEqual(derive_project_keywords(str(d)), ["libtv-enterprise"])

    def test_named_parent_kept(self):
        d = self.mk("workspace_codex/hello-app")
        self.assertEqual(derive_project_keywords(str(d)), ["hello-app", "workspace_codex"])

    def test_system_dirs(self):
        self.assertEqual(derive_project_keywords("/tmp"), [])
        self.assertEqual(derive_project_keywords("/Users"), [])


class RealTempRoots(unittest.TestCase):
    """不替换 _TEMP_ROOTS：日志里的真实形态（目录不存在也一样，git 认不出）。"""

    def test_claude_scratchpad(self):
        cwd = ("/private/tmp/claude-501/-Users-mac-work-ai-kg-hub--claude-worktrees-gateway-x"
               "/e287560d-e4aa-4b74-95b4-9a93b92b76f7/scratchpad")
        self.assertEqual(derive_project_keywords(cwd), [])
        self.assertEqual(derive_project_keywords("/tmp/oss-duration-check/notes"), [])


if __name__ == "__main__":
    unittest.main()
