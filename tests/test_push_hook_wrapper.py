"""PUSH hook 必须经 tools/kg_push_hook.sh 从发布产物运行，并拿到 NAS 地址。

事故（2026-10-14）：Claude Code 的 SessionStart hook 直接调
spike-graphiti/.venv/bin/python tools/kg_push_hook.py。venv 已不存在；更早之前 hook
进程就拿不到 KG_HUB_URL，回落到已退役的 127.0.0.1:8080，data/.push_hook.log 里 10-09
起 54 次会话启动全是 Connection refused。hook 失败时会话照常开，所以没人发现。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "tools" / "kg_push_hook.sh"

# 假解释器：把收到的参数和关键环境变量打出来，代替真正的 kg_push_hook.py
FAKE_PY = """#!/bin/sh
echo "hook=$1"
shift
echo "args=$*"
echo "url=${KG_HUB_URL:-}"
echo "log=${KG_HUB_PUSH_HOOK_LOG:-}"
"""


def clean_env(home, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("KG_HUB_", "ANTHROPIC_"))}
    env["HOME"] = str(home)
    env.update(extra)
    return env


class Wrapper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.root = self.home / ".local" / "share" / "kg-hub"
        py = self.root / "venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text(FAKE_PY)
        py.chmod(0o755)
        hook = self.root / "current" / "tools" / "kg_push_hook.py"
        hook.parent.mkdir(parents=True)
        hook.write_text("# release copy\n")
        env_file = self.home / ".claude-mem" / ".env"
        env_file.parent.mkdir()
        env_file.write_text("KG_HUB_URL=http://100.123.208.32:17171\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_wrapper(self, *args, **env):
        return subprocess.run(["/bin/sh", str(WRAPPER), *args], capture_output=True,
                              text=True, env=clean_env(self.home, **env))

    def fields(self, out):
        return dict(line.split("=", 1) for line in out.strip().splitlines())

    def test_runs_release_code_with_nas_url_and_passes_args(self):
        r = self.run_wrapper("--format", "codex")
        self.assertEqual(r.returncode, 0, r.stderr)
        f = self.fields(r.stdout)
        self.assertEqual(f["hook"], str(self.root / "current" / "tools" / "kg_push_hook.py"))
        self.assertEqual(f["args"], "--format codex")
        self.assertEqual(f["url"], "http://100.123.208.32:17171")

    def test_log_goes_where_the_readers_look_unless_overridden(self):
        f = self.fields(self.run_wrapper().stdout)
        self.assertEqual(f["log"], str(self.home / "work-ai" / "kg-hub" / "data" / ".push_hook.log"))
        f = self.fields(self.run_wrapper(KG_HUB_PUSH_HOOK_LOG="/x/y.log").stdout)
        self.assertEqual(f["log"], "/x/y.log")

    def test_missing_release_fails_loudly_without_falling_back(self):
        (self.root / "current" / "tools" / "kg_push_hook.py").unlink()
        r = self.run_wrapper("--format", "claude")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("发布产物不完整", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_missing_env_file_still_runs(self):
        (self.home / ".claude-mem" / ".env").unlink()
        r = self.run_wrapper()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fields(r.stdout)["url"], "")


class HookLogPath(unittest.TestCase):
    def log_path(self, **env):
        code = "import tools.kg_push_hook as K; print(K.LOG_PATH)"
        r = subprocess.run([_sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=ROOT, env=clean_env(Path.home(), PYTHONPATH=str(ROOT), **env))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_env_overrides_log_path(self):
        self.assertEqual(self.log_path(KG_HUB_PUSH_HOOK_LOG="/tmp/x.log"), "/tmp/x.log")

    def test_default_log_path_unchanged(self):
        self.assertEqual(self.log_path(), str(ROOT / "data" / ".push_hook.log"))


class HookConfigs(unittest.TestCase):
    def test_codex_hook_uses_release_wrapper(self):
        cfg = json.loads((ROOT / "plugin" / "hooks" / "codex-hooks.json").read_text())
        cmds = [h["command"] for groups in cfg["hooks"].values()
                for g in groups for h in g["hooks"] if "kg_push_hook" in h["command"]]
        self.assertTrue(cmds)
        for c in cmds:
            self.assertIn("/.local/share/kg-hub/current/tools/kg_push_hook.sh", c)
            self.assertNotIn("spike-graphiti", c)
            self.assertNotIn("kg_push_hook.py", c)

    def test_inventory_recognises_wrapper_and_direct_calls(self):
        import tools.hook_inventory as H
        registry = json.loads((ROOT / "config" / "hook_registry.json").read_text())
        push = [c for c in registry["components"] if c["id"] == "kg-push"]
        for cmd in ("/bin/sh ~/.local/share/kg-hub/current/tools/kg_push_hook.sh --format claude",
                    "python tools/kg_push_hook.py --format cursor"):
            self.assertEqual(H._action(cmd), "push")
            self.assertEqual(H._component(cmd, push)["id"], "kg-push")


if __name__ == "__main__":
    unittest.main()
