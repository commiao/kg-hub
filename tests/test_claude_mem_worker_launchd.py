"""launchd 启动器的版本发现：两条拉起路径必须收敛到同一个版本。

## 要治的病（2026-09-19 实测）

worker 有两条拉起路径：hook 和 launchd。它们各自做版本发现，而**单例端口只保证
「只有一个 worker」，不保证「是哪一个」** —— 谁先起谁说了算。

实测当时：

    hook     起 marketplace 的 13.25.1
    launchd  起 cache 的 13.24.23

成因是这个脚本给 marketplace 目录写死了最低版本键，于是只要 cache 里还剩任何一个
版本，launchd 就永远选 cache —— 哪怕它比 marketplace 旧两个补丁版本。

这不是洁癖问题：给 worker 打的补丁装在哪一份上，就只有那一条路径生效，而哪条路径
生效取决于开机时谁先抢到端口。
"""
from __future__ import annotations

import json
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "claude_mem_worker_launchd.sh"


def discover(home: Path) -> list[str]:
    """在一个假 HOME 上跑脚本里那段 find_service + pick，返回排好序的候选。"""
    body = SCRIPT.read_text("utf-8")
    start = body.index("find_service()")
    end = body.index("\npick()", start)
    harness = textwrap.dedent(f"""
        HOME={home}
        {body[start:end]}
        find_service | sort -k1,1r -k2,2n
    """)
    done = subprocess.run(["/bin/sh", "-c", harness], capture_output=True, text=True)
    return [line for line in done.stdout.splitlines() if line.strip()]


def install(root: Path, version: str, *, marketplace: bool = False) -> None:
    if marketplace:
        base = root / ".claude/plugins/marketplaces/thedotmack/plugin"
        (base).mkdir(parents=True, exist_ok=True)
        (base / "package.json").write_text(json.dumps({"version": version}), "utf-8")
        target = base / "scripts"
    else:
        target = root / f".claude/plugins/cache/thedotmack/claude-mem/{version}/scripts"
    target.mkdir(parents=True, exist_ok=True)
    (target / "worker-service.cjs").write_text("// stub\n", "utf-8")


class VersionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_a_newer_marketplace_beats_a_stale_cache(self):
        """本用例就是那个 bug 的形状：cache 13.24.23 / marketplace 13.25.1。"""
        install(self.home, "13.24.23")
        install(self.home, "13.25.1", marketplace=True)
        first = discover(self.home)[0]
        self.assertIn("marketplaces", first, f"又选回了旧 cache：{first}")
        self.assertIn("13.25.1", first)

    def test_same_version_prefers_the_plugin_managed_copy(self):
        """同版本时让插件自己管的那份赢 —— marketplace 只是「当前」那一份。"""
        install(self.home, "13.25.1")
        install(self.home, "13.25.1", marketplace=True)
        first = discover(self.home)[0]
        self.assertIn("/cache/", first)
        self.assertNotIn("marketplaces", first)

    def test_a_newer_cache_still_wins(self):
        """没有回退成「永远选 marketplace」—— 那只是把偏见换了个方向。"""
        install(self.home, "13.26.0")
        install(self.home, "13.25.1", marketplace=True)
        self.assertIn("13.26.0", discover(self.home)[0])

    def test_marketplace_alone_is_still_found(self):
        """cache 全缺席时仍然起得来。原来那条兜底语义不许丢。"""
        install(self.home, "13.25.1", marketplace=True)
        found = discover(self.home)
        self.assertEqual(len(found), 1, found)
        self.assertIn("marketplaces", found[0])

    def test_a_marketplace_without_a_readable_version_is_last_not_absent(self):
        """package.json 坏了也不能让它消失 —— 那会退回「cache 缺席就起不来」。"""
        install(self.home, "13.25.1", marketplace=True)
        (self.home / ".claude/plugins/marketplaces/thedotmack/plugin/package.json"
         ).write_text("{ not json", "utf-8")
        install(self.home, "13.24.23")
        found = discover(self.home)
        self.assertEqual(len(found), 2, found)
        self.assertIn("13.24.23", found[0])
        self.assertIn("marketplaces", found[1])
        # 钉住那个哨兵键本身，而不是只看它排在后面。
        # 2026-09-19 变异验证：把兜底整条去掉，版本键变成空串，它**照样**排最后
        # —— 于是「排最后」这条断言根本分不出来。那等于在隐式依赖 sort 对空字段
        # 的处理，而不是依赖我们写的兜底。钉值才钉得住。
        self.assertEqual(found[1].split()[0], "0" * 24)


if __name__ == "__main__":
    unittest.main()
