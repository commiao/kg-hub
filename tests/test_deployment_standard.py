"""发布准则（docs/deployment-standard.md）里可机检的那几条。

光写文档会漂：半年后没人记得为什么不能用 `:latest`，然后就有人加回去，回滚又变
成不可能。2026-09-08 kg-hub 的通用部署被禁用（T-0046），表面理由是"Compose 会删
掉重命名的旧容器备份"，但根子是 compose 把五个服务都指向 `kg-hub-server:latest`
——可变标签一 build 就把旧镜像覆盖成悬空层，"回滚"根本没有对象，才不得不去抢救
旧**容器**。这套测试钉住的就是别再回到那个状态。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
RELEASE = ROOT / "deploy" / "nas" / "release.sh"
BLOCKED = ROOT / "deploy" / "nas" / "redeploy.sh"
STANDARD = ROOT / "docs" / "deployment-standard.md"

# 第三方镜像不归本准则管，那是上游的事。
THIRD_PARTY = ("falkordb/",)


class ImageTagTests(unittest.TestCase):
    """准则二、三：自建镜像按 commit 打不可变标签，compose 硬引用。"""

    def setUp(self):
        self.compose = COMPOSE.read_text("utf-8")

    def own_images(self):
        for line in self.compose.splitlines():
            match = re.match(r"\s*image:\s*(\S+)", line)
            if match and not match.group(1).startswith(THIRD_PARTY):
                yield match.group(1)

    def test_no_self_built_image_uses_latest(self):
        offenders = [i for i in self.own_images() if i.endswith(":latest")]
        self.assertEqual(offenders, [], "自建镜像不许用 :latest —— 旧版本会被覆盖，回滚就没有对象了")

    def test_tag_is_a_hard_reference_not_a_soft_default(self):
        # `:-latest` 这种软默认等于没改：少了变量就悄悄发一个来路不明的镜像。
        for image in self.own_images():
            with self.subTest(image=image):
                self.assertIn("${", image, "标签必须来自变量，不能写死")
                self.assertIn(":?", image, "必须是硬引用 ${VAR:?...}，不许 ${VAR:-default}")
                self.assertNotIn(":-", image)

    def test_every_service_sharing_the_image_uses_the_same_variable(self):
        variables = {re.search(r"\$\{([A-Z_]+)", i).group(1) for i in self.own_images()}
        self.assertEqual(variables, {"KG_HUB_IMAGE_TAG"},
                         "同一个镜像必须由同一个变量控制，否则切换会切一半")


class ReleaseScriptTests(unittest.TestCase):
    """准则一、四、五：发 commit 不发工作区；回滚靠标签；项目名写死。"""

    def setUp(self):
        if not RELEASE.exists():
            self.fail("发布脚本不见了：deploy/nas/release.sh")
        self.source = RELEASE.read_text("utf-8")
        # 注释里会引用旧写法作为反面教材，检查实际代码时必须先把注释去掉，
        # 否则测试会因为「文档写得详细」而误报。
        self.code = "\n".join(line for line in self.source.splitlines()
                              if not line.lstrip().startswith("#"))

    def test_source_comes_from_git_not_the_working_tree(self):
        self.assertIn("git archive", self.code)
        # 旧脚本 `tar -cf - -C "$REPO"` 打的是工作区：本地脏文件会被发上线。
        self.assertNotRegex(self.code, r"tar -c[^|]*\$REPO")

    def test_release_refuses_a_commit_that_is_not_on_the_remote(self):
        # 线上跑的东西必须在别人的仓库里也找得到，否则无从对账、无法复现。
        self.assertIn("merge-base --is-ancestor", self.code)
        self.assertIn("origin/main", self.code)

    def test_the_env_gap_is_closed_before_the_slow_build(self):
        # 硬引用会影响别人：变量写进 .env 之前谁跑 compose 谁失败。补写必须排在
        # 构建之前，否则窗口有几分钟长。
        seed = self.code.index("grep -q '^KG_HUB_IMAGE_TAG='")
        build = self.code.index("build -t kg-hub-server:")
        self.assertLess(seed, build)

    def test_rollback_reuses_the_old_image_and_verifies_it_exists(self):
        self.assertIn("--rollback", self.source)  # 用法说明里也要有
        self.assertIn("image inspect kg-hub-server:", self.code)
        # 不许"回滚"时重新构建：那样拿到的不是当初跑的那个东西。
        rollback = self.source.split("回滚不重建", 1)
        self.assertEqual(len(rollback), 2, "回滚路径必须显式不重建")

    def test_health_check_runs_after_the_swap_and_auto_reverts(self):
        swap = self.code.index("up -d --no-deps --no-build")
        check = self.code.index("健康验收")
        self.assertLess(swap, check, "验收必须在切换之后，否则验的是旧的")
        self.assertIn("自动回到", self.code)

    def test_project_name_is_pinned_in_the_script(self):
        # 多 actor 用不同 -p 会各建一套容器抢同一个端口。
        self.assertIn('KG_HUB_COMPOSE_PROJECT:-kg-hub', self.code)
        self.assertIn("-p $PROJECT", self.code)


class GuardrailTests(unittest.TestCase):
    def test_the_unsafe_script_stays_disabled(self):
        # T-0046 明确写了不得通过删除 exit 或临时开关恢复使用。新路径是另起一条，
        # 不是把旧的解禁。
        if not BLOCKED.exists():
            self.skipTest("旧脚本已删除")
        source = BLOCKED.read_text("utf-8")
        head = source.split("\n", 40)
        self.assertIn("DEPLOY_BLOCKED", "\n".join(head[:40]))

    def test_the_standard_is_written_down(self):
        self.assertTrue(STANDARD.exists(), "准则必须在仓库里，不能只在某次对话里")
        text = STANDARD.read_text("utf-8")
        for rule in (":latest", "git archive", "--rollback", "项目名"):
            self.assertIn(rule, text)


if __name__ == "__main__":
    unittest.main()
