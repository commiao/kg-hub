"""NAS 源码漂移检测的几条硬约束。

这套检测存在的理由：2026-09-18 发现 credvault 网关生产上有约 1400 行从未提交，
两条线各自实现了同一个功能、互不知情；而更早的 2026-09-07 已经做过一次「全量纳入
版本控制」的快照 —— **十天后又漂了 1400 行**。只补快照不建检测无效。

这里钉的不是"能跑"，是几条容易在照抄中丢掉、丢了就变成假绿的设计：

1. 清单与路径从 `release.sh` 解析，不在检测器里抄第二份
2. 两侧都要列、取并集 —— 只按 git 清单查，永远看不见"只在生产上存在"的文件
3. 点目录豁免必须精确到"不吞点文件"
4. 判决要带写入时刻 —— 没有它，巡检停掉之后陈旧结论会被当成现在
5. 只剩备份杂物时不许报成实质漂移 —— 永远红的检查等于没有

每条都做过变异验证：把被测的那行改坏，测试当场转红。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy" / "nas"))

import check_source_drift as D  # noqa: E402


class ManifestSourceTests(unittest.TestCase):
    """清单来源必须是发布脚本本身，解析不出来就拒绝跑。"""

    def test_parses_ssh_and_src_from_the_release_script(self):
        config = D.release_config()
        self.assertTrue(config["ssh"], "解析不出 ssh 目标")
        self.assertTrue(config["src"].startswith("/"), "源码目录必须是绝对路径")

    def test_refuses_when_the_release_script_no_longer_uses_git_archive(self):
        # 「清单 = 整棵树」这个前提依赖 release.sh 用 git archive。它改成白名单
        # 而这里没跟着改，就会静悄悄"对上"一个其实不同的版本。
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "release.sh"
            fake.write_text('NAS="${KG_HUB_NAS_SSH:-x@y}"\n'
                            'SRC="${KG_HUB_NAS_SRC:-/z}"\n'
                            'tar -cf - -C "$REPO" a b c\n', "utf-8")
            with self.assertRaises(SystemExit) as caught:
                D.release_config(fake)
            self.assertIn("git archive", str(caught.exception))

    def test_refuses_a_release_script_whose_variables_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "release.sh"
            fake.write_text("git archive HEAD\n", "utf-8")
            with self.assertRaises(SystemExit):
                D.release_config(fake)

    def test_refuses_a_missing_release_script(self):
        with self.assertRaises(SystemExit):
            D.release_config(ROOT / "deploy" / "nas" / "no-such-file.sh")


class ExclusionTests(unittest.TestCase):
    """排除法在扫描侧是安全的，但豁免必须精确 —— 它是唯一能藏住漂移的地方。"""

    def test_dot_directories_are_exempt(self):
        # 历次部署脚本留下的备份与暂存，实测 72 个文件。
        for name in (".deploy-backups/kg_hub_server.py",
                     ".effective-quota.Q0rDgi/.dockerignore",
                     ".dashboard-health.MCdbrv/topology.py"):
            self.assertTrue(D.is_allowed_untracked(name), name)

    def test_the_dot_rule_does_not_swallow_tracked_dot_files(self):
        # git 确实跟踪 .dockerighore / .gitignore 两个点**文件**。豁免写成
        # 「名字以点开头就放过」的话，这两个的漂移就永远看不见了。
        for name in (".dockerignore", ".gitignore"):
            self.assertFalse(D.is_allowed_untracked(name), name)

    def test_real_source_paths_are_never_exempt(self):
        for name in ("kg_hub_server.py", "deploy/nas/release.sh",
                     "tools/capture_probe.py", "deploy/mac/agents/x.plist"):
            self.assertFalse(D.is_allowed_untracked(name), name)

    def test_no_tracked_file_is_ever_exempt(self):
        """最强的一条：豁免不许吞掉任何 git 真正跟踪的文件。

        写死几个名字的测试挡不住这类错 —— 2026-09-18 实测：`.env` 那条豁免用了
        子串匹配，把仓库真正跟踪的 `deploy/nas/.env.example` 一起吞了，而当时的
        测试全绿。豁免是这套检测里唯一能藏住漂移的地方，所以拿**全量清单**来验。
        """
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                             capture_output=True, text=True, check=True).stdout
        swallowed = [n for n in out.splitlines()
                     if n.strip() and D.is_allowed_untracked(n)]
        self.assertEqual(swallowed, [], "这些被跟踪的文件永远查不出漂移")

    def test_tracked_dot_files_match_what_git_actually_tracks(self):
        # 上面那条测试写死了两个名字。如果哪天仓库开始跟踪点目录里的东西
        # （比如 .github/），豁免规则就不再精确 —— 这里让它当场失败。
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                             capture_output=True, text=True, check=True).stdout
        in_dot_dirs = [n for n in out.splitlines() if n.startswith(".") and "/" in n]
        self.assertEqual(in_dot_dirs, [],
                         "仓库开始跟踪点目录里的文件了，豁免规则要重新评估")


class BackupClassificationTests(unittest.TestCase):
    """备份杂物要和真漂移分开，否则真信号会被噪音淹掉。"""

    def test_backup_artifacts_are_recognised(self):
        for name in ("kg_hub_server.py.bak-20260821-131007",
                     "kg_hub_server.py.pre-boundaries-20260820-1544",
                     "backups/t0046/kg_hub_server.py",
                     # 实测真有这个：当年某条备份命令的引号写错了
                     "kg_hub_server.py.bak.$(date +%s)"):
            self.assertTrue(D.looks_like_backup(name), name)

    def test_real_files_are_not_mistaken_for_backups(self):
        for name in ("deploy/effective-quota.override.yml",
                     "deploy/mac/requirements.txt", "kg_hub_server.py"):
            self.assertFalse(D.looks_like_backup(name), name)


class UnionTests(unittest.TestCase):
    """两侧都要列。只按 git 清单查，最危险的那类漂移永远看不见。"""

    def compare_with(self, tracked, on_nas, hashes):
        with mock.patch.object(D, "tracked_at", return_value=tracked), \
             mock.patch.object(D, "remote_listing", return_value=on_nas), \
             mock.patch.object(D, "remote_hashes", return_value=hashes), \
             mock.patch.object(D, "git_hash", side_effect=lambda ref, n: hashes.get(n)):
            return dict(D.compare("x@y", "/src", "deadbeef"))

    def test_a_file_only_on_nas_is_reported(self):
        # 生产在跑、git 里连文件名都没有 —— 这是最危险的一种，也正是只查 git
        # 清单永远发现不了的那一种。
        bad = self.compare_with(["a.py"], ["a.py", "ghost.py"],
                                {"a.py": "1" * 64, "ghost.py": "2" * 64})
        self.assertEqual(bad.get("ghost.py"), "只在 NAS 上，git 未跟踪")

    def test_a_file_deleted_from_git_but_still_on_nas_is_reported(self):
        # release.sh 逐文件 mv、从不删除，所以从 git 删掉的文件会一直留在 NAS 上
        # 被执行。实测抓到两个：deploy/effective-quota.override.yml 与
        # deploy/mac/requirements.txt。
        bad = self.compare_with([], ["deploy/effective-quota.override.yml"],
                                {"deploy/effective-quota.override.yml": "3" * 64})
        self.assertIn("deploy/effective-quota.override.yml", bad)

    def test_content_mismatch_is_reported(self):
        with mock.patch.object(D, "tracked_at", return_value=["a.py"]), \
             mock.patch.object(D, "remote_listing", return_value=["a.py"]), \
             mock.patch.object(D, "remote_hashes", return_value={"a.py": "1" * 64}), \
             mock.patch.object(D, "git_hash", return_value="9" * 64):
            self.assertEqual(dict(D.compare("x@y", "/src", "ref")),
                             {"a.py": "内容不一致"})

    def test_a_tracked_file_missing_on_nas_is_reported(self):
        bad = self.compare_with(["a.py"], [], {})
        self.assertEqual(bad.get("a.py"), "NAS 上没有")

    def test_an_aligned_tree_reports_nothing(self):
        self.assertEqual(self.compare_with(["a.py"], ["a.py"], {"a.py": "1" * 64}), {})


class ListExtraTests(unittest.TestCase):
    """--list-extra：观察期的唯一产物，将来 release.sh 要照它 prune。

    这份清单有两条性质必须钉死，丢了任何一条都会变成「删错东西」：
      1. 它和漂移报告取自**同一处**定义（EXTRA_REASONS），不能各自演化
      2. 豁免文件（.env 等）绝不能出现在里面 —— 那是生产凭据
    """

    def run_list_extra(self, bad):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with mock.patch.object(D, "compare", return_value=bad), \
             mock.patch.object(D, "release_config",
                               return_value={"ssh": "x@y", "src": "/src"}), \
             mock.patch.object(D, "live_commit", return_value="d" * 40), \
             mock.patch.object(D, "write_status") as ws, \
             redirect_stdout(buf):
            rc = D.main(["--list-extra"])
        return rc, [l for l in buf.getvalue().splitlines() if l], ws

    def test_lists_only_the_nas_side_extras(self):
        rc, out, _ = self.run_list_extra([
            ("a.py", "内容不一致"),          # 不是多出来的，是改过的 —— 不能删
            ("b.py", "NAS 上没有"),          # 根本不在 NAS 上 —— 更不能"删"
            ("ghost.py", D.EXTRA_REASONS[0]),
            ("x.py.bak", D.EXTRA_REASONS[1]),
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(out, ["ghost.py", "x.py.bak"])

    def test_content_mismatch_never_enters_the_prune_list(self):
        # 最要命的误删形态：文件两边都有、只是内容不同。它属于"该重新发布"，
        # 不属于"该删除"。把它混进清单，一次 prune 就把生产文件删了。
        _, out, _ = self.run_list_extra([("kg_hub_server.py", "内容不一致")])
        self.assertEqual(out, [])

    def test_labels_come_from_one_definition(self):
        # compare() 打的标签必须就是 EXTRA_REASONS 里的那两个字符串。
        # 任一处被改成字面量而另一处没跟着改，这条立刻转红。
        with mock.patch.object(D, "tracked_at", return_value=[]), \
             mock.patch.object(D, "remote_listing",
                               return_value=["ghost.py", "x.py.bak"]), \
             mock.patch.object(D, "remote_hashes", return_value={}), \
             mock.patch.object(D, "git_hash", return_value=None):
            reasons = set(dict(D.compare("x@y", "/src", "ref")).values())
        self.assertTrue(reasons <= set(D.EXTRA_REASONS), reasons)

    def test_exempt_files_can_never_reach_the_list(self):
        """.env 是生产凭据，进了这份清单将来就会被 prune 删掉。

        它在 remote_listing 源头就被滤掉，所以进不了 compare 的结果、
        也就进不了清单。这里端到端锁一遍：给 remote_listing 喂进 .env，
        清单里必须没有它、而真正多余的那个必须在。
        """
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        raw = [".env", ".DS_Store", "a/__pycache__/x.pyc", "._foo", "ghost.py"]
        with mock.patch.object(D, "tracked_at", return_value=[]), \
             mock.patch.object(D, "remote_listing",
                               side_effect=lambda *a: [n for n in raw
                                                       if not D.is_allowed_untracked(n)]), \
             mock.patch.object(D, "remote_hashes", return_value={}), \
             mock.patch.object(D, "git_hash", return_value=None), \
             mock.patch.object(D, "release_config",
                               return_value={"ssh": "x@y", "src": "/src"}), \
             mock.patch.object(D, "live_commit", return_value="d" * 40), \
             mock.patch.object(D, "write_status"), \
             redirect_stdout(buf):
            D.main(["--list-extra"])
        listed = [l for l in buf.getvalue().splitlines() if l]
        self.assertEqual(listed, ["ghost.py"], listed)

    def test_list_extra_never_writes_a_verdict(self):
        # 观察期只读：不许写状态文件，否则会把巡检面板的判决覆盖掉。
        _, _, ws = self.run_list_extra([("ghost.py", D.EXTRA_REASONS[0])])
        ws.assert_not_called()


class StatusFileTests(unittest.TestCase):
    """判决必须带写入时刻，且只剩杂物时不许报成实质漂移。"""

    def test_status_line_carries_the_write_time(self):
        # 巡检自己停掉时文件会停在最后一条绿上。没有时刻，读的人就会把三天前的
        # 结论当成现在 —— 2026-09-18 一天栽了三次，都是新鲜时间戳盖着陈旧数字。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.status"
            D.write_status(str(path), "ok", "abc 说明")
            at, verdict, detail = path.read_text("utf-8").strip().split("\t", 2)
            self.assertRegex(at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
            self.assertEqual(verdict, "ok")
            self.assertEqual(detail, "abc 说明")

    def test_write_failure_does_not_break_the_check(self):
        # 写不动状态文件是小事，把巡检本身弄失败是大事。
        D.write_status("/proc/definitely-not-writable/x.status", "ok", "x")

    def test_no_status_file_requested_is_a_no_op(self):
        D.write_status(None, "ok", "x")

    def run_main(self, bad, status):
        with mock.patch.object(D, "release_config",
                               return_value={"ssh": "x@y", "src": "/src"}), \
             mock.patch.object(D, "live_commit", return_value="a" * 12), \
             mock.patch.object(D, "compare", return_value=bad), \
             mock.patch.object(subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="09-18 20:00 某次提交", returncode=0)
            return D.main(["--status-file", status])

    def test_only_junk_left_is_reported_as_ok_not_as_drift(self):
        # 永远红的检查等于没有 —— 没人会再看它。备份杂物照样列出来让人清，
        # 但它不是"线上源码与 git 不符"。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.status"
            code = self.run_main([("x.py.bak-2026", "备份杂物")], str(path))
            self.assertEqual(code, 0)
            _, verdict, detail = path.read_text("utf-8").strip().split("\t", 2)
            self.assertEqual(verdict, "ok")
            self.assertIn("备份杂物", detail)

    def test_real_drift_exits_nonzero_and_names_the_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.status"
            code = self.run_main([("ghost.py", "只在 NAS 上，git 未跟踪")], str(path))
            self.assertEqual(code, 1)
            _, verdict, detail = path.read_text("utf-8").strip().split("\t", 2)
            self.assertEqual(verdict, "drift")
            self.assertIn("ghost.py", detail)

    def test_a_clean_tree_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.status"
            self.assertEqual(self.run_main([], str(path)), 0)
            self.assertIn("\tok\t", path.read_text("utf-8"))



class PostReleaseRefreshTests(unittest.TestCase):
    """能改变判决的动作，自己负责刷新它。

    漂移判决是缓存，而巡检一天才跑一次。发布改的恰好是「线上」那一侧，于是发完
    之后 SessionStart 会继续拿发布前那条结论当现状显示，最长可达一天。2026-09-18
    在网关那边实测踩到：部署成功后巡检仍报「deploy/ 有 8 个文件漂了」，而那 8 个
    正是刚被这次发布对齐掉的。**年龄阈值救不了这种** —— 文件是一天之内写的，
    看起来就是新鲜的。
    """

    def setUp(self):
        self.source = (ROOT / "deploy" / "nas" / "release.sh").read_text("utf-8")
        self.code = "\n".join(l for l in self.source.splitlines()
                               if not l.lstrip().startswith("#"))

    def test_release_refreshes_the_verdict(self):
        self.assertIn("refresh_drift_verdict", self.code)
        self.assertIn("check_source_drift.py", self.code)

    def test_refresh_happens_after_the_swap_not_before(self):
        # 在换容器之前刷新，刷的是旧状态，等于没刷。
        swap = self.code.index("up -d --no-deps --no-build")
        refresh = self.code.index("refresh_drift_verdict\n")
        self.assertLess(swap, refresh)

    def test_a_failed_refresh_does_not_fail_the_release(self):
        # 发布本身已经成功；把它标成失败会触发不必要的回滚。
        body = self.code.split("refresh_drift_verdict() {", 1)[1][:600]
        self.assertNotIn("die ", body)
        self.assertIn("return 0", body)

    def test_dry_run_does_not_touch_the_status_file(self):
        body = self.code.split("refresh_drift_verdict() {", 1)[1][:300]
        self.assertIn('DRY_RUN', body)


def _git(*args, cwd):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class TrunkVerdictTests(unittest.TestCase):
    """「ok」只有一个含义：等于**主干这条线上**的某个 commit。

    补这段之前，kg-hub 的 ok 只说明「NAS 上的文件等于它自称的那个 commit」——
    那个 commit 可以在任何分支上、甚至从没推上来过。于是准则 18（只发主干）在
    这条检测里查不出来，只活在 release.sh 的闸上。而绕过发布脚本改生产正是这套
    检测存在的理由（credvault 实测被绕过两次，准则 21）。闸和检测同时只剩一个时，
    剩下的那个是检测。

    用真 git 仓库而不是 mock：要钉的是 git 的行为，mock 掉就什么也没钉。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "r"
        self.repo.mkdir()
        _git("init", "-q", "-b", "main", ".", cwd=self.repo)
        for k, v in (("user.email", "t@e.com"), ("user.name", "t"),
                     ("commit.gpgsign", "false")):
            _git("config", k, v, cwd=self.repo)
        self.shas = []
        for i in range(3):
            (self.repo / "f.txt").write_text(str(i))
            _git("add", "f.txt", cwd=self.repo)
            _git("commit", "-q", "-m", f"c{i}", cwd=self.repo)
            self.shas.append(_git("rev-parse", "HEAD", cwd=self.repo))
        _git("checkout", "-q", "-b", "side", self.shas[1], cwd=self.repo)
        (self.repo / "g.txt").write_text("x")
        _git("add", "g.txt", cwd=self.repo)
        _git("commit", "-q", "-m", "side", cwd=self.repo)
        self.side = _git("rev-parse", "HEAD", cwd=self.repo)
        _git("checkout", "-q", "main", cwd=self.repo)
        self._old = D.REPO
        D.set_repo(self.repo)
        self.addCleanup(D.set_repo, self._old)

    def verdict(self, ref, *, fetched=True):
        with mock.patch.object(D, "fetch_origin", return_value=fetched):
            return D.trunk_verdict(ref, "main")[0]

    def test_主干_tip_在主干上(self):
        self.assertEqual(self.verdict(self.shas[2]), "on")

    def test_更早的主干_commit_也算在主干上_回滚是正当操作(self):
        """is-ancestor 与「等于 tip」的分界就在这一条。写成相等，它当场转红。

        实况佐证：2026-09-21 NAS 跑着 49bc2d87，而主干 tip 已经是 c6b5f75 ——
        两者都正常，因为发布之后主干又前进了。写成相等的话，每次发布之后到
        下次发布之前，这条检查会一直红。
        """
        self.assertEqual(self.verdict(self.shas[0]), "on")

    def test_旁支上的_commit_不在主干上(self):
        self.assertEqual(self.verdict(self.side), "off")

    def test_仓库里根本没有的_commit_是最严重的一档(self):
        self.assertEqual(self.verdict("0" * 40), "off")

    def test_拉不到_origin_时_在主干上依然可信(self):
        """陈旧的 origin/main 只会造成单向的错。

        一个 commit 若是旧主干的祖先，它必然也是新主干的祖先 —— 所以 True 在
        陈旧基线下依然成立。不这么分的话，一次网络抖动就把一条正常的绿变成橙。
        **实测就是这么发现的**：第一版写成「fetch 失败一律不作判决」，
        credvault 那边的既有用例当场随机转红。
        """
        self.assertEqual(self.verdict(self.shas[0], fetched=False), "on")

    def test_拉不到_origin_时_看着不在主干上要降级成判不了(self):
        """False 这一侧不可信：可能是它其实已经合进去了，只是这次没拉到。

        把它报成 drift，就是拿这条检测里最响的警报去报一件不存在的事（准则 28）。
        """
        self.assertEqual(self.verdict(self.side, fetched=False), "unknown")

    def test_拉不到_origin_且本地没有这个_commit_也判不了(self):
        self.assertEqual(self.verdict("0" * 40, fetched=False), "unknown")


class TrunkVerdictReachesTheStatusFileTests(unittest.TestCase):
    """判决要真的走到状态文件上 —— helper 对了而 main 没接，等于没做。"""

    def run_main(self, trunk_result, *, bad=(), argv=()):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with mock.patch.object(D, "compare", return_value=list(bad)), \
             mock.patch.object(D, "release_config",
                               return_value={"ssh": "x@y", "src": "/src"}), \
             mock.patch.object(D, "live_commit", return_value="d" * 40), \
             mock.patch.object(D, "trunk_verdict", return_value=trunk_result), \
             mock.patch.object(D, "write_status") as ws, \
             redirect_stdout(buf):
            rc = D.main(list(argv))
        verdict = ws.call_args[0][1] if ws.call_args else None
        detail = ws.call_args[0][2] if ws.call_args else ""
        return rc, verdict, detail, buf.getvalue()

    def test_不在主干上时_即使文件全对也不许出_ok(self):
        """最要害的一条：文件与它自称的 commit 完全一致，但那个 commit 不在主干上。

        补这段之前这里出的是 ok —— 一条看不出任何问题的绿灯，而线上正跑着
        主干上不存在的代码。
        """
        rc, verdict, detail, _ = self.run_main(("off", "dddddddddddd 不在 origin/main 这条线上"))
        self.assertEqual(verdict, "drift")
        self.assertEqual(rc, 1)
        self.assertIn("文件本身与它一致", detail)     # 别让人以为是文件问题

    def test_不在主干上且文件也有差异时_两件事都说(self):
        rc, verdict, detail, _ = self.run_main(
            ("off", "dddddddddddd 不在 origin/main 这条线上"),
            bad=[("a.py", "内容不一致"), ("b.py", "NAS 上没有")])
        self.assertEqual(verdict, "drift")
        self.assertIn("2 处文件差异", detail)

    def test_在主干上且文件干净_才是_ok(self):
        rc, verdict, _, _ = self.run_main(("on", "在主干上"))
        self.assertEqual(verdict, "ok")
        self.assertEqual(rc, 0)

    def test_判不了时出第三态_不出_ok_也不出_drift(self):
        """kg-hub 此前**根本没有第三态**（词表只有 ok/drift），失败路径全是
        SystemExit、写状态之前就退了 —— 状态文件停在上一条判决，消费侧要等
        36 小时年龄阈值才发现。最长 36 小时的陈旧绿灯，而那正是消费侧注释里
        写着要防的事。
        """
        rc, verdict, detail, _ = self.run_main(("unknown", "这次没拉到 origin"))
        self.assertEqual(verdict, "error")
        self.assertEqual(rc, 2)

    def test_list_extra_不受主干判影响(self):
        """纯列举模式给 release.sh 提供清单，不出判决。主干判属于判决。"""
        rc, verdict, _, out = self.run_main(
            ("off", "不在主干上"), bad=[("ghost.py", D.EXTRA_REASONS[0])],
            argv=["--list-extra"])
        self.assertEqual(rc, 0)
        self.assertIsNone(verdict)
        self.assertEqual(out.split(), ["ghost.py"])


if __name__ == "__main__":
    unittest.main()
