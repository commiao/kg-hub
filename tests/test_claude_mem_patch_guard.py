"""claude-mem worker 补丁守护。

## 要治的病

T-0077 B 项的补丁装在第三方插件的构建产物里。`claude plugin update` 会覆盖它，
而覆盖之后**什么都不会报错** —— worker 照常跑、采集照常走，只是每次链路抖动
又开始重复付费。这类故障没有声音，所以必须有人定期去看。

## 最该防的不是「补丁掉了」

是**把旧版本盖回新版本**。插件真升级之后，把我们基于 13.25.1 的 bundle 按回去，
会静默降级整个 worker —— 比丢补丁糟得多。所以「版本对不上就一个字节都不写」
是这个文件里最重要的一条。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "tools" / "claude_mem_patch_guard.sh"

PATCHED = b"// patched worker with cm-op marker\n"
STOCK = b"// stock worker\n"
VERSION = "13.25.1"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "tools").mkdir(parents=True)
        # 补丁源放在仓库的同级目录，与真实布局一致。
        self.source = Path(self.tmp.name) / "claude-mem-fork/plugin/scripts/worker-service.cjs"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(PATCHED)
        (self.repo / "tools" / "claude_mem_patch_guard.sh").write_text(
            GUARD.read_text("utf-8"), "utf-8")
        (self.repo / "tools" / "claude_mem_patch_guard.sh").chmod(0o755)
        self.target = self.home / ".claude/plugins/marketplaces/thedotmack/plugin"
        (self.target / "scripts").mkdir(parents=True)
        self.bundle = self.target / "scripts" / "worker-service.cjs"
        self.log = Path(self.tmp.name) / "guard.log"
        self.write_manifest(sha(PATCHED), VERSION)
        self.write_target(STOCK, VERSION)

    def write_manifest(self, digest: str, version: str) -> None:
        (self.repo / "tools" / "claude_mem_patch.manifest").write_text(
            f"version={version}\nsha256={digest}\nmarker=cm-op\n"
            "source=../claude-mem-fork/plugin/scripts/worker-service.cjs\n"
            "target=.claude/plugins/marketplaces/thedotmack/plugin\n", "utf-8")

    def write_target(self, body: bytes, version: str) -> None:
        self.bundle.write_bytes(body)
        (self.target / "package.json").write_text(json.dumps({"version": version}), "utf-8")

    def run_guard(self) -> str:
        done = subprocess.run(
            ["/bin/sh", str(self.repo / "tools" / "claude_mem_patch_guard.sh")],
            capture_output=True, text=True,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin",
                 "CLAUDE_MEM_PATCH_LOG": str(self.log),
                 "CLAUDE_MEM_PATCH_STATE": str(Path(self.tmp.name) / "state")},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.log.read_text("utf-8") if self.log.exists() else ""

    # ── 核心那一条 ──────────────────────────────────────────────────
    def test_a_version_bump_is_never_overwritten(self):
        """插件真升级了就一个字节都不写。**这是本文件最重要的一条。**

        盖回去会把整个 worker 静默降级 —— 比丢补丁糟得多。
        """
        self.write_target(STOCK, "13.26.0")
        log = self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), STOCK, "把旧版本盖回了新版本")
        self.assertIn("版本已变", log)

    def test_a_version_bump_is_reported_not_swallowed(self):
        """而且必须出声：不还原 = 重复付费的防护当前失效，沉默就是在撒谎。"""
        self.write_target(STOCK, "13.26.0")
        self.run_guard()
        marks = list((Path(self.tmp.name) / "state").glob("patch-version-moved-*"))
        self.assertEqual(len(marks), 1, "版本漂移没有告警")

    # ── 正常还原 ────────────────────────────────────────────────────
    def test_an_overwritten_patch_is_restored(self):
        log = self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), PATCHED)
        self.assertIn("已还原", log)

    def test_an_intact_patch_stays_silent(self):
        """绝大多数轮次走这条。静默,否则日志会被刷爆、真事就淹了。"""
        self.write_target(PATCHED, VERSION)
        self.assertEqual(self.run_guard(), "")

    # ── 拒绝在不确定时动手 ──────────────────────────────────────────
    def test_a_source_that_does_not_match_the_manifest_is_refused(self):
        """源与清单指纹不符 = 不知道手里这份是什么，不许拿它还原。

        重新构建之后忘了同步清单，正是这个形态。
        """
        self.source.write_bytes(b"// rebuilt, manifest not updated\n")
        log = self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), STOCK)
        self.assertIn("指纹不符", log)

    def test_a_missing_source_is_loud_not_silent(self):
        """有清单、没源 —— 还原能力根本不存在。

        不出声的话，下一次插件升级会把补丁抹掉，而不会有任何人知道。
        """
        self.source.unlink()
        log = self.run_guard()
        self.assertIn("补丁源不存在", log)

    def test_an_unreadable_version_is_never_written_over(self):
        """读不出版本就不写 —— 不知道对面是什么版本时动手，可能就是降级。"""
        (self.target / "package.json").write_text("{ not json", "utf-8")
        log = self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), STOCK)
        self.assertIn("读不出版本", log)


if __name__ == "__main__":
    unittest.main()
