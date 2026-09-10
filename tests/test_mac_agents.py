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
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "deploy" / "mac" / "agents"
INSTALL = ROOT / "deploy" / "mac" / "install.sh"
REQUIREMENTS = ROOT / "deploy" / "mac" / "requirements.txt"
README = ROOT / "deploy" / "mac" / "README.md"

EXPECTED = {
    "com.kg-hub.capsule-watch", "com.kg-hub.capture-probe",
    "com.kg-hub.claude-mem-guard", "com.kg-hub.claude-mem-ingest",
    "com.kg-hub.feedback-digest", "com.kg-hub.weekly-report",
}
SECRETISH = re.compile(r"open\.feishu\.cn/open-apis/bot|xox[bp]-|Bearer\s+\S|[A-Za-z0-9_-]{32,}")


class TemplateTests(unittest.TestCase):
    def templates(self):
        return sorted(AGENTS.glob("com.kg-hub.*.plist"))

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
                text = (path.read_text("utf-8")
                        .replace("__REPO__", "/repo").replace("__HOME__", "/home"))
                text = re.sub(r"@[A-Z_]+@", "x", text)
                document = plistlib.loads(text.encode("utf-8"))
                self.assertEqual(document["Label"], path.stem,
                                 "Label 必须与文件名一致，否则 launchctl 装到别的名下")
                self.assertTrue(document.get("ProgramArguments"))


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.source = INSTALL.read_text("utf-8")

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

    def test_check_mode_runs_clean_against_this_machine(self):
        # 模板必须能逐字节还原出机器上正在跑的那份，否则「进仓库」这件事本身就
        # 引入了行为变化。这台机器上装了才跑；别的机器跳过。
        target = Path.home() / "Library/LaunchAgents/com.kg-hub.capture-probe.plist"
        if not target.exists():
            self.skipTest("这台机器上没装 kg-hub 的 launchd 服务")
        done = subprocess.run(["bash", str(INSTALL), "--check"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)


class ManifestTests(unittest.TestCase):
    def test_mac_dependencies_are_pinned(self):
        lines = [l for l in REQUIREMENTS.read_text("utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
        self.assertTrue(lines)
        unpinned = [l for l in lines if "==" not in l]
        self.assertEqual(unpinned, [], "不钉版本就不叫清单")

    def test_readme_is_honest_about_the_drift_from_the_container(self):
        # Mac venv 与容器 requirements 已经有 5 个包对不上。装作一致比不写更糟。
        text = README.read_text("utf-8")
        self.assertIn("deploy/nas/requirements.txt", text)
        self.assertIn("不保证一致", text)


if __name__ == "__main__":
    unittest.main()
