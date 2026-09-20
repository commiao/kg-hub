"""Mac 本机服务定义进仓库后的几条硬约束。

2026-09-10 盘点：这 6 个 launchd 服务的定义只活在一台机器的
`~/Library/LaunchAgents/` 里，仓库里一个字都没有——`capsule-watch` 连名字都没在
代码里出现过。脚本一直在 git 里，缺的是「应该跑什么」这一半。

补进来时踩到一个真坑：`capsule-watch.plist` 里**明文存着飞书 webhook**。所以进
仓库的是模板，不是 plist 本身。这套测试钉住模板别再把机密和绝对路径带回去。
"""
from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "deploy" / "mac" / "agents"
INSTALL = ROOT / "deploy" / "mac" / "install.sh"
SHARED_REQUIREMENTS = ROOT / "deploy" / "nas" / "requirements.txt"
README = ROOT / "deploy" / "mac" / "README.md"

EXPECTED = {
    "com.kg-hub.capsule-watch", "com.kg-hub.capture-probe",
    "com.kg-hub.claude-mem-guard", "com.kg-hub.claude-mem-ingest",
    "com.kg-hub.feedback-digest", "com.kg-hub.weekly-report",
    # 日更的源码漂移巡检：查 NAS 上跑的源码是不是等于 git 里的某个 commit。
    "com.kg-hub.source-drift",
    # 不姓 kg-hub 但同样由本仓库管：保留 claude-mem 自己的 label，这样装上去是
    # **替换**插件那份、而不是与它并存（并存会有两个 job 各起一份 worker）。
    "com.claude-mem.worker",
}
SECRETISH = re.compile(r"open\.feishu\.cn/open-apis/bot|xox[bp]-|Bearer\s+\S|[A-Za-z0-9_-]{32,}")


class TemplateTests(unittest.TestCase):
    def templates(self):
        # 与 install.sh 的扫描范围一字不差。写窄了的后果很隐蔽：新模板照样被
        # 安装，却逃过下面所有安全断言（无机密、无绝对路径、plist 合法）。
        return sorted(AGENTS.glob("com.*.plist"))

    def test_every_known_service_is_tracked(self):
        got = {p.stem for p in self.templates()}
        self.assertEqual(got, EXPECTED,
                         "机器上跑着但仓库里没有的服务，就是下一个没人知道的黑箱")

    def test_no_secret_survives_into_the_repo(self):
        # capsule-watch 原本明文存着飞书 webhook。
        for path in self.templates():
            with self.subTest(agent=path.stem):
                text = path.read_text("utf-8")
                self.assertIsNone(SECRETISH.search(text), f"{path.name} 里疑似有机密")
                for name in re.findall(r"<key>([A-Z_]*(?:TOKEN|SECRET|WEBHOOK|PASSWORD)[A-Z_]*)</key>", text):
                    value = re.search(rf"<key>{name}</key>\s*<string>([^<]*)</string>", text)
                    if value:
                        self.assertRegex(value.group(1), r"^@[A-Z_]+@$",
                                         f"{name} 必须是占位符，不能是真值")

    def test_no_machine_specific_absolute_path(self):
        # 写死 /Users/<某人> 的模板换台机器就废了。
        for path in self.templates():
            with self.subTest(agent=path.stem):
                self.assertNotIn("/Users/", path.read_text("utf-8"))

    def test_templates_are_valid_plists_once_rendered(self):
        for path in self.templates():
            with self.subTest(agent=path.stem):
                text = path.read_text("utf-8")
                for name, value in (("__CODE__", "/code"), ("__VENV__", "/venv"),
                                    ("__GITREPO__", "/repo"), ("__HOME__", "/home")):
                    text = text.replace(name, value)
                text = re.sub(r"@[A-Z_]+@", "x", text)
                document = plistlib.loads(text.encode("utf-8"))
                self.assertEqual(document["Label"], path.stem,
                                 "Label 必须与文件名一致，否则 launchctl 装到别的名下")
                self.assertTrue(document.get("ProgramArguments"))


    def test_code_comes_from_the_release_not_the_worktree(self):
        """准则 20 的机器检查：脚本和解释器都不许来自开发工作树。

        2026-09-19 盘点时 Mac 侧 14 个作业**全部**指向工作树，而准则当时已经写好
        了 —— 不参与判断的准则会被绕过。后果是三类实测事故：分支上改了不生效
        （所有会话被迫在同一棵树上直接改生产）、脚本正被执行时被原地改写
        （sh 边读边执行、字节偏移错位崩溃，本仓库 a5325e0）、说不清线上是哪一版。
        """
        for path in self.templates():
            with self.subTest(agent=path.stem):
                text = path.read_text("utf-8")
                self.assertNotIn(
                    "__CODE__/spike-graphiti/.venv", text,
                    "解释器要用 __VENV__（产物之外的环境），不能从产物里取")
                argv = plistlib.loads(
                    _render_for_test(text).encode("utf-8"))["ProgramArguments"]
                for item in argv:
                    if item.endswith((".py", ".sh")) or item.endswith("/bin/python"):
                        self.assertFalse(
                            item.startswith("/repo"),
                            f"{path.stem} 的代码或解释器来自工作树：{item}")

    def test_the_worktree_path_only_appears_as_the_drift_checker_input(self):
        """工作树路径可以作为**数据**出现，但必须是报备过的那个开关的值。

        没有这一条，上面那条检查就能靠「把路径挪到下一个参数里」绕过去。
        目前只有一个正当用法：漂移巡检要比对 git 仓库，而发布产物里没有 .git ——
        仓库是它的输入，不是它的代码来源。
        """
        for path in self.templates():
            with self.subTest(agent=path.stem):
                argv = plistlib.loads(
                    _render_for_test(path.read_text("utf-8")).encode("utf-8")
                )["ProgramArguments"]
                for index, item in enumerate(argv):
                    if not str(item).startswith("/repo"):
                        continue
                    previous = argv[index - 1] if index else None
                    self.assertEqual(
                        previous, "--repo",
                        f"{path.stem} 第 {index} 个参数带工作树路径，前面却是 {previous!r}")

    def test_secrets_are_read_from_outside_the_release(self):
        """机密既不在 git 里也不在发布产物里，运行时必须显式指到产物外。

        guard 脚本默认读 `$SCRIPT_DIR/../.env` —— 那是工作树布局的假设，
        在发布产物里根本不存在，于是它会静默地拿不到 webhook。
        """
        guard = AGENTS / "com.kg-hub.claude-mem-guard.plist"
        document = plistlib.loads(
            _render_for_test(guard.read_text("utf-8")).encode("utf-8"))
        env = document.get("EnvironmentVariables") or {}
        self.assertIn("KG_HUB_ENV_FILE", env)
        self.assertFalse(str(env["KG_HUB_ENV_FILE"]).startswith(("/code", "/repo")),
                         "机密不该从发布产物或工作树里取")


def _render_for_test(text: str) -> str:
    for name, value in (("__CODE__", "/code"), ("__VENV__", "/venv"),
                        ("__GITREPO__", "/repo"), ("__HOME__", "/home")):
        text = text.replace(name, value)
    return re.sub(r"@[A-Z_]+@", "x", text)


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.source = INSTALL.read_text("utf-8")

    def test_install_renders_code_paths_to_the_release(self):
        """渲染目标必须是发布产物，不是 install.sh 自己所在的那棵树。

        原来 `__REPO__` 直接渲染成工作树路径 —— 于是「装好了」等于「把生产指回了
        开发目录」，而这正是准则 20 要消掉的东西。
        """
        render = [line for line in self.source.splitlines()
                  if "sed -e" in line or (line.strip().startswith("-e")
                                          and "__" in line)]
        self.assertTrue(render, "找不到渲染那几行")
        joined = "\n".join(render)
        self.assertIn("__CODE__", joined)
        self.assertIn("__VENV__", joined)
        # 只看渲染逻辑，不看注释：注释里提到旧占位符是讲历史，不是行为。
        self.assertNotIn("__REPO__", joined,
                         "还在把代码路径渲染成开发工作树")
        # 钉赋值那一行，不是"文件里出现过这个字符串"。
        # 第一版就写成了后者，而我自己刚在文件头的注释里写了同一串路径 ——
        # 于是把 CODE 改回 $REPO 的变异照样绿。**注释会替代码背书。**
        assignment = [line.strip() for line in self.source.splitlines()
                      if line.startswith("CODE=")]
        self.assertEqual(len(assignment), 1, f"CODE 赋值不止一处：{assignment}")
        self.assertIn(".local/share/kg-hub/current", assignment[0])
        self.assertNotIn("$REPO", assignment[0],
                         "CODE 指回了开发工作树")

    def test_has_a_drift_check_that_changes_nothing(self):
        # 「避免各种版本互相冲突」靠的就是这个：能发现有人手改了 plist 却没回写仓库。
        self.assertIn("--check", self.source)
        self.assertIn("机器上的与仓库里的不一致", self.source)

    def test_refuses_to_install_with_an_unresolved_secret(self):
        # 装一个带着 @VAR@ 字面量的 plist 上去，服务会以一种很难查的方式坏掉。
        self.assertIn("缺机密", self.source)
        self.assertIn("plutil -lint", self.source)

    def test_reloads_by_bootout_then_bootstrap(self):
        # 只 kickstart 的话 launchd 用的还是旧定义，改了等于没改。
        boot_out = self.source.index("launchctl bootout")
        boot_strap = self.source.index("launchctl bootstrap")
        self.assertLess(boot_out, boot_strap)

    def test_no_variable_is_glued_to_a_cjk_character(self):
        """`$label：` 在非 UTF-8 locale 下会被读成变量名 `label\xef`。

        2026-09-20 实测：`--check` 的失败分支因此直接崩在
        `line 116: label\xef: unbound variable`。**这个 bug 在失败路径上藏了很久** ——
        只要机器和仓库一直一致，那几行就从没被执行过；这次把代码路径改成发布产物、
        第一次真出现不一致，它才冒出来。

        launchd 跑作业时的 locale 与人在终端里的不同，所以"我本地跑没事"说明不了
        任何事。CJK 相邻的展开一律写 `${var}`。
        """
        source = INSTALL.read_text("utf-8")
        glued = [line.strip() for line in source.splitlines()
                 if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]", line)]
        self.assertEqual(glued, [], f"变量紧贴非 ASCII 字符：{glued}")

    def test_the_failure_branch_survives_a_stripped_locale(self):
        """失败分支必须能在被剥干净的环境里跑完，而不是自己崩掉。

        上一条钉的是写法，这一条钉的是**行为** —— 光看源码看不出 bash 会怎么解析。
        """
        done = subprocess.run(
            ["bash", str(INSTALL), "--check", "com.kg-hub.does-not-exist"],
            capture_output=True, timeout=60,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C"})
        stderr = done.stderr.decode("utf-8", "replace")
        self.assertNotIn("unbound variable", stderr)
        self.assertNotIn("\ufffd", stderr, "输出里有解不出来的字节")

    def test_check_mode_runs_clean_against_this_machine(self):
        # 模板必须能逐字节还原出机器上正在跑的那份，否则「进仓库」这件事本身就
        # 引入了行为变化。这台机器上装了才跑；别的机器跳过。
        target = Path.home() / "Library/LaunchAgents/com.kg-hub.capture-probe.plist"
        if not target.exists():
            self.skipTest("这台机器上没装 kg-hub 的 launchd 服务")
        done = subprocess.run(["bash", str(INSTALL), "--check"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_git_repo_placeholder_does_not_follow_whichever_checkout_ran_it(self):
        """`__GITREPO__` 必须解析成**这台机器约定的那棵工作树**，
        而不是「谁碰巧执行了安装」。

        2026-09-20 实测的坑：默认值曾是 `$REPO`（install.sh 自己在哪个检出里）。
        在主工作树里它恰好等于机器上装的那个值，所以一直没人发现。两层后果：

        1. `--check` 在任何非主工作树里必红 —— 而现在的纪律恰恰是「在独立 worktree
           上检出 origin/main、在那里跑全量再发布」。那个红与机器状态无关，
           正好淹掉它本该抓的真漂移。
        2. **更要紧**：谁要是从一个临时发布 worktree 跑过一次 install.sh，
           漂移巡检就被永久指向那个临时目录 —— 而它随后会被删掉。
           失效方式是安静的：巡检照跑、照报绿。

        这条按 linked worktree 里跑一遍来验 —— 那正是它当初漏掉的场景。
        （deploy-standard 准则 28：判断的两端要取自同一来源。）
        """
        import tempfile
        probe = Path(tempfile.mkdtemp(prefix="kg-gitrepo-probe-"))
        checkout = probe / "checkout"
        add = subprocess.run(["git", "worktree", "add", "-q", "--detach",
                              str(checkout), "HEAD"],
                             cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        if add.returncode != 0:
            self.skipTest(f"建不了 worktree：{add.stderr[:120]}")
        try:
            # 要钉的属性是**不变性**，不是「等于某个具体路径」：
            # 从两个不同的检出解析，必须得到同一个答案。
            # 第一版我拿 ROOT（测试当前所在的检出）当期望值 —— 而这条测试本身就
            # 可能在 worktree 里跑，于是期望值跟着执行位置变，又犯了它要防的那个病。
            def resolve_from(path: Path) -> Path:
                done = subprocess.run(
                    ["bash", "-c",
                     'cd "$1"; git rev-parse --path-format=absolute --git-common-dir',
                     "_", str(path)],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)
                return Path(done.stdout.strip()).parent.resolve()

            from_here = resolve_from(ROOT)
            from_worktree = resolve_from(checkout)
            self.assertEqual(from_worktree, from_here,
                             "解析结果跟着执行位置跑了 —— 正是这条要防的")
            self.assertNotEqual(from_worktree, checkout.resolve(),
                                "解析成了临时检出本身；它随后会被删掉")
            # 真跑一遍 --check，而且跑的必须是**当前工作树里这一版**。
            #
            # `git worktree add HEAD` 取的是已提交内容，直接跑它等于测上一个版本 ——
            # 本地改了还没提交时会红，而红的原因跟被测属性无关。所以把当前的
            # deploy/ 覆盖进去再跑：这样验的是手上这份代码在 worktree 里的行为。
            shutil.rmtree(checkout / "deploy", ignore_errors=True)
            shutil.copytree(ROOT / "deploy", checkout / "deploy")
            target = Path.home() / "Library/LaunchAgents/com.kg-hub.capture-probe.plist"
            if not target.exists():
                self.skipTest("这台机器上没装 kg-hub 的 launchd 服务")
            done = subprocess.run(["bash", str(checkout / "deploy/mac/install.sh"),
                                   "--check"], capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0,
                             f"--check 从 worktree 跑红了：{done.stdout[-700:]}")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(checkout)],
                           cwd=str(ROOT), capture_output=True, timeout=60)


class ManifestTests(unittest.TestCase):
    """依赖只有一份清单，Mac 的 venv 必须与它一致。

    2026-09-10 一度加过第二份 `deploy/mac/requirements.txt`，理由是「Mac 与容器
    已经漂了 5 个包」。核实后发现那 5 个是比对脚本的 bug（venv 那侧把下划线规范
    成了连字符，清单那侧没有），实际差异为零。两份内容相同的清单没有任何好处，
    只会给真正的漂移留一个藏身处，所以删掉了，改成在这里真的比一遍。
    """

    @staticmethod
    def normalise(name):
        return re.sub(r"[-_.]+", "-", name).lower()

    def declared(self):
        out = {}
        for line in SHARED_REQUIREMENTS.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"([A-Za-z0-9._-]+)\s*==\s*([^\s;]+)", line)
            self.assertIsNotNone(match, f"不钉版本就不叫清单：{line}")
            out[self.normalise(match.group(1))] = match.group(2)
        return out

    def test_the_only_manifest_is_fully_pinned(self):
        self.assertTrue(self.declared())
        self.assertFalse((ROOT / "deploy" / "mac" / "requirements.txt").exists(),
                         "不要第二份清单：内容相同则无用，内容不同则是事故")

    def test_the_mac_venv_matches_the_manifest(self):
        venv = list((ROOT / "spike-graphiti" / ".venv" / "lib").glob("python*/site-packages"))
        if not venv:
            self.skipTest("这台机器上没有 kg-hub 的 venv")
        installed = {}
        for item in venv[0].iterdir():
            if item.name.endswith(".dist-info"):
                stem = item.name[: -len(".dist-info")]
                name, _, version = stem.rpartition("-")
                installed[self.normalise(name)] = version
        declared = self.declared()
        missing = sorted(k for k in declared if k not in installed)
        wrong = sorted(f"{k}: 清单 {declared[k]} / 实装 {installed[k]}"
                       for k in declared if k in installed and declared[k] != installed[k])
        self.assertEqual(missing, [], "清单里有、venv 里没装")
        self.assertEqual(wrong, [], "版本对不上")


if __name__ == "__main__":
    unittest.main()
