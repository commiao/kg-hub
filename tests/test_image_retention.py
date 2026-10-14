"""发布成功后清旧镜像（2026-10-14）：apply_image_retention 真跑一遍，参数对不对。

规则本身在 fleet-ops lib/fleetops_retention.py 测；这里钉的是 kg-hub 这一侧的接线：
保护这次的 $SHA 和回滚目标 $PREV、只动 kg-hub-server、真的 --apply、
找不到 fleet-ops 产物时说出来但不让发布失败、dry-run 时什么都不做。
"""
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = (ROOT / "deploy" / "nas" / "release.sh").read_text("utf-8")


def function_body(name: str) -> str:
    start = RELEASE.index(f"{name}() {{")
    return RELEASE[start:RELEASE.index("\n}\n", start) + 3]


class ImageRetentionTests(unittest.TestCase):
    def run_fn(self, lib, extra: dict) -> subprocess.CompletedProcess:
        script = textwrap.dedent("""
            set -euo pipefail
            say() { printf '%s\\n' "$*" >&2; }
            NAS=nas-target; DK="sudo -n /docker"
        """) + function_body("apply_image_retention") + "\napply_image_retention\necho done\n"
        env = {"PATH": os.getenv("PATH", "/usr/bin:/bin"), "HOME": os.getenv("HOME", "/tmp"), **extra}
        if lib is not None:
            env["FLEET_OPS_LIB"] = str(lib)
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)

    def stub_lib(self, tmp: str, rc: int = 0):
        lib = Path(tmp, "lib")
        lib.mkdir()
        record = Path(tmp, "args.txt")
        (lib / "fleetops_retention.py").write_text(
            "import sys\n"
            f"open({str(record)!r}, 'w').write('\\n'.join(sys.argv[1:]))\n"
            "print('镜像 kg-hub-server：保留 5 个，删除 3 个')\n"
            f"sys.exit({rc})\n")
        return lib, record

    def test_protects_this_release_and_the_rollback_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib, record = self.stub_lib(tmp)
            r = self.run_fn(lib, {"SHA": "abc123", "PREV": "def456"})
            self.assertEqual(r.returncode, 0, r.stderr)
            args = record.read_text().split("\n")
            self.assertEqual(args[:5], ["images", "--ssh", "nas-target", "--docker", "sudo -n /docker"])
            self.assertIn("kg-hub-server:abc123", args)
            self.assertIn("kg-hub-server:def456", args)
            self.assertEqual(args[args.index("--repo") + 1], "kg-hub-server")
            self.assertIn("--apply", args)
            self.assertIn("--prune-dangling", args)
            self.assertIn("保留 5 个", r.stderr)

    def test_first_release_without_a_previous_tag_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib, record = self.stub_lib(tmp)
            r = self.run_fn(lib, {"SHA": "abc123", "PREV": ""})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("kg-hub-server:", record.read_text().replace("kg-hub-server:abc123", ""))

    def test_a_failing_cleanup_does_not_fail_the_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib, _ = self.stub_lib(tmp, rc=1)
            r = self.run_fn(lib, {"SHA": "abc123", "PREV": "def456"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("done", r.stdout)

    def test_missing_fleet_ops_is_said_out_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_fn(Path(tmp, "nowhere"), {"SHA": "abc123", "PREV": "def456"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("没有清理旧镜像", r.stderr)

    def test_dry_run_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib, record = self.stub_lib(tmp)
            r = self.run_fn(lib, {"SHA": "abc123", "PREV": "def456", "DRY_RUN": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(record.exists())

    def test_it_runs_only_after_the_release_is_declared_done(self):
        tail = RELEASE[RELEASE.index('say "✅ 发布完成'):]
        self.assertEqual(tail.split("\n")[1].strip(), "apply_image_retention")


if __name__ == "__main__":
    unittest.main()
