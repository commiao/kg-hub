#!/usr/bin/env python3
"""自动 prune 的删除判据：只删「git 历史还能原样吐出来」的那一份内容。

T-0084。这套检查要钉的不是「能不能删」，而是**它在该拒绝的时候拒绝** ——
误删一个生产独有且在跑的文件，就是把生产打掉，而且 kg-hub 的发布**没有整树
备份**（只有 .env 的事务备份），删了就真没了。
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "deploy/nas/release.sh"

_SPEC = importlib.util.spec_from_file_location(
    "drift", ROOT / "deploy/nas/check_source_drift.py")
drift = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(drift)


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout


class HistoricalHashesTests(unittest.TestCase):
    """判据本身：内容能不能被 git 历史原样吐回来。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"),
                           ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        self._real_repo = drift.REPO
        drift.set_repo(self.repo)
        self.addCleanup(lambda: drift.set_repo(self._real_repo))

    def commit(self, name: str, body: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", name)

    @staticmethod
    def sha(body: str) -> str:
        return hashlib.sha256(body.encode()).hexdigest()

    def test_a_deleted_file_can_still_be_produced_from_history(self):
        """正常要删的那一类：git 删过，但历史里原样存着。"""
        self.commit("app/dead.py", "退役了\n")
        (self.repo / "app/dead.py").unlink()
        git(self.repo, "commit", "-aqm", "删掉它")
        self.assertIn(self.sha("退役了\n"), drift.historical_hashes("app/dead.py", "HEAD"))

    def test_every_version_counts_not_just_the_last_one(self):
        """NAS 上停在哪一版都可能，所以历史里的每一版都要算。"""
        self.commit("app/x.py", "v1\n")
        self.commit("app/x.py", "v2\n")
        got = drift.historical_hashes("app/x.py", "HEAD")
        self.assertIn(self.sha("v1\n"), got)
        self.assertIn(self.sha("v2\n"), got)

    def test_a_path_git_never_saw_has_no_history_so_it_can_never_match(self):
        """这一条是整个安全边界：从没进过 git 的东西永远不可能被删。

        它替代了「维护一张生产独有文件的白名单」—— 白名单会忘记更新，
        而「历史里没有」是结构性的，忘不掉。
        """
        self.commit("app/x.py", "v1\n")
        self.assertEqual(drift.historical_hashes("deploy/hot_config.py", "HEAD"), set())

    def test_a_tracked_path_edited_on_production_does_not_match(self):
        """最阴的一档：路径 git 认识，但 NAS 上那份被人手改过。

        只查「曾被跟踪」会放行它，而放行 = 把那些改动删得无法恢复。
        """
        self.commit("app/x.py", "原样\n")
        (self.repo / "app/x.py").unlink()
        git(self.repo, "commit", "-aqm", "删掉")
        self.assertNotIn(self.sha("被人在生产上改过\n"),
                         drift.historical_hashes("app/x.py", "HEAD"))
        # 对照：它确实曾被跟踪 —— 所以"曾被跟踪"这个判据在这里是不够的。
        self.assertTrue(drift.was_ever_tracked("app/x.py"))

    def test_history_lookup_failure_means_do_not_delete(self):
        """失败方向必须倒向「不删」。"""
        drift.set_repo(self.repo / "不存在")
        self.assertEqual(drift.historical_hashes("app/x.py", "HEAD"), set())


    def test_content_from_a_commit_outside_the_deployed_line_is_not_prunable(self):
        """2026-09-21 真机撞到的那一种：NAS 上有个文件来自**比线上更新**的 commit。

        `tests/test_upstream_error_classification.py` 在 NAS 上，而线上跑的是
        49bc2d87 —— 那个文件来自 9ef37b5，更晚。用 `git log --all` 查，它「在 git
        历史里找得到」，于是被判成可删。**但它不是孤儿，是部署不完整的信号**，
        删掉等于把信号抹了，下次还会以同样的方式出现而没人知道为什么。
        `--all` 还会让任何分支上的文件都变成可删：别人从分支拷一份到生产，
        发布就会替他删掉。所以范围必须是 ref 的祖先链。
        """
        self.commit("app/base.py", "base\n")
        deployed = git(self.repo, "rev-parse", "HEAD").strip()
        self.commit("app/future.py", "来自更新的提交\n")
        self.assertNotIn(self.sha("来自更新的提交\n"),
                         drift.historical_hashes("app/future.py", deployed))
        # 对照：在更新的那个 ref 下它当然找得到 —— 差别只在范围。
        self.assertIn(self.sha("来自更新的提交\n"),
                      drift.historical_hashes("app/future.py", "HEAD"))



class TheDecidingLineActuallyUsesTheGateTests(unittest.TestCase):
    """真跑 `--list-prunable`，钉住**放行的那一行**用的是内容闸。

    上面那些用例只证明了 `historical_hashes` 自己是对的 —— 变异验证当场发现：
    把 main() 里的判据退回 `was_ever_tracked(name)`，它们照样全绿。
    **一个函数正确，不等于有人在用它。**
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"),
                           ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        (self.repo / "keep.py").write_text("keep\n", encoding="utf-8")
        (self.repo / "gone.py").write_text("原样内容\n", encoding="utf-8")
        git(self.repo, "add", "-A"); git(self.repo, "commit", "-q", "-m", "v1")
        (self.repo / "gone.py").unlink()
        git(self.repo, "commit", "-aqm", "删掉 gone.py")
        self.ref = git(self.repo, "rev-parse", "HEAD").strip()

        saved = {k: getattr(drift, k) for k in
                 ("REPO", "release_config", "live_commit", "compare", "remote_hashes")}
        self.addCleanup(lambda: [setattr(drift, k, v) for k, v in saved.items()])
        drift.set_repo(self.repo)
        drift.release_config = lambda *a, **k: {"ssh": "x", "src": "/x"}
        drift.live_commit = lambda *a: self.ref
        drift.compare = lambda *a: [("gone.py", drift.EXTRA_REASONS[0]),
                                    ("never_in_git.py", drift.EXTRA_REASONS[0])]
        self.nas = {"gone.py": hashlib.sha256("原样内容\n".encode()).hexdigest(),
                    "never_in_git.py": hashlib.sha256(b"prod only").hexdigest()}
        drift.remote_hashes = lambda ssh, src, names: dict(self.nas)

    def run_prunable(self) -> list[str]:
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            drift.main(["--list-prunable", "--repo", str(self.repo)])
        return [x for x in out.getvalue().splitlines() if x.strip()]

    def test_a_deleted_file_whose_content_history_still_has_is_listed(self):
        self.assertIn("gone.py", self.run_prunable())

    def test_a_file_git_never_saw_is_never_listed(self):
        """这条是安全边界本身：生产独有的文件绝不能进删除清单。"""
        self.assertNotIn("never_in_git.py", self.run_prunable())

    def test_editing_it_on_production_takes_it_out_of_the_delete_list(self):
        """判据必须是**内容**，不是路径。

        这条就是变异验证抓到的那个缺口：把 main() 的判据退回「路径曾被跟踪」，
        gone.py 照样会被列出来 —— 哪怕生产上那份已经被人改得面目全非。
        """
        self.nas["gone.py"] = hashlib.sha256("被人在生产上改过\n".encode()).hexdigest()
        self.assertNotIn("gone.py", self.run_prunable())

    def test_a_list_only_call_never_writes_a_verdict(self):
        """列举模式不出判决 —— 否则一次取清单会覆盖掉巡检的状态文件。"""
        calls = []
        real = drift.write_status
        drift.write_status = lambda *a, **k: calls.append(a)
        self.addCleanup(lambda: setattr(drift, "write_status", real))
        self.run_prunable()
        self.assertEqual(calls, [])



class EmptyDirCleanupNeverTouchesDotDirsTests(unittest.TestCase):
    """清空目录那一步绝不能碰点目录 —— `$SRC/.release.lock` 就是其中之一。

    正常情况锁目录含 owner 文件、非空、删不到。但 `mkdir $LOCK` 与写 owner 之间
    有窗口（取锁和抢占陈旧锁两条路径都有），写失败就留下一个**空的锁目录**，
    然后这一步把自己正握着的锁悄悄删掉 —— 没有任何报错，并发闸当场失效。

    第二个独立理由：漂移检测对根下点目录整体豁免，从来不报它们。
    **「删什么」不能超出「报什么」**，超出的那部分没有任何东西看着。
    """

    def test_the_find_prunes_dot_entries_before_deleting(self):
        # 续行要先拼成逻辑行：那条命令跨两行，按物理行取会把 -prune 落在另一行上，
        # 断言于是钉了半句话。（第一版就是这么写的，当场假红。）
        logical, buf = [], ""
        for raw in RELEASE.read_text("utf-8").splitlines():
            if raw.lstrip().startswith("#"):
                continue
            buf += raw.rstrip()
            if buf.endswith("\\"):
                buf = buf[:-1] + " "
                continue
            logical.append(buf.strip()); buf = ""
        hits = [x for x in logical if "-type d -empty" in x]
        self.assertTrue(hits, "找不到清空目录那条命令")
        for cmd in hits:
            self.assertIn("-name '.*' -prune", cmd,
                          "空目录清理没有排除点目录，会删掉正握着的 .release.lock")

    def test_it_really_spares_an_empty_lock_dir(self):
        """光看命令看不出 find 会怎么解析，所以真跑一次。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".release.lock").mkdir()      # 空锁：正是危险的那一刻
            (root / "normal_empty").mkdir()
            (root / "keep").mkdir()
            (root / "keep/f").write_text("x", encoding="utf-8")
            subprocess.run(
                ["find", str(root), "-mindepth", "1", "-name", ".*", "-prune",
                 "-o", "-type", "d", "-empty", "-exec", "rmdir", "{}", "+"],
                capture_output=True)
            self.assertTrue((root / ".release.lock").is_dir(),
                            "把正握着的锁删掉了")
            self.assertFalse((root / "normal_empty").exists(),
                             "对照组：普通空目录该被清掉，否则这条用例没在验东西")
            self.assertTrue((root / "keep/f").exists())


class ReleaseScriptWiringTests(unittest.TestCase):
    """接线：发布脚本真的用了收窄后的那个出口，而不是全集。

    断言钉在**放行的那一行本身**，不钉「这些字眼在不在源码里」——
    2026-09-20 一晚撞了三次：注释和 docstring 会替断言通过。
    """

    def setUp(self):
        self.source = RELEASE.read_text("utf-8")
        self.code = [line for line in self.source.splitlines()
                     if not line.lstrip().startswith("#")]

    def test_the_line_that_builds_the_delete_list_uses_list_prunable(self):
        calls = [line.strip() for line in self.code
                 if "check_source_drift.py" in line or "$checker" in line]
        deciding = [line for line in calls if "--list-" in line]
        self.assertTrue(deciding, "找不到取清单的那一行")
        for line in deciding:
            self.assertIn("--list-prunable", line,
                          f"取删除清单用了全集出口：{line}")
            self.assertNotIn("--list-extra", line)

    def test_the_guard_rejects_anything_that_is_not_a_clean_success(self):
        """写「等于我预料到的失败码」会漏掉预料之外的那些。

        `--list-prunable` 设计上返 0，而未捕获异常返 1 —— 判据写成 `= 2`
        就会让 rc=1 顺着 happy path 走成「没有多余文件」。
        """
        guards = [line.strip() for line in self.code
                  if re.search(r'"\$rc"\s*(!=|=)\s*"0"', line)]
        self.assertTrue(guards, "prune 没有按「非明确成功即失败」把守返回码")

    def test_prune_runs_before_the_verdict_is_refreshed(self):
        """prune 改变「NAS 上有什么」，而判决正是对这件事的结论（准则 10）。"""
        order = [line.strip() for line in self.code
                 if line.strip() in ("prune_orphans", "refresh_drift_verdict")]
        self.assertEqual(order[:2], ["prune_orphans", "refresh_drift_verdict"],
                         "判决刷在 prune 前面，会被 prune 自己弄过期")

    def test_deletion_refuses_absolute_and_traversing_paths(self):
        self.assertIn("/*|*..*)", self.source,
                      "清单是外部命令输出，必须拒绝绝对路径和 ..")

    def test_there_is_an_escape_hatch(self):
        self.assertIn("NO_PRUNE", self.source)


if __name__ == "__main__":
    unittest.main()
