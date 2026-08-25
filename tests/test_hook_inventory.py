import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import hook_inventory as H


def _registry(tmp_path, tools):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({
        "version": 1,
        "components": [{
            "id": "task-hub", "label": "task-hub",
            "patterns": ["task-hub.py", "session_context.py"],
            "purpose": "任务上下文",
        }, {
            "id": "claude-mem", "label": "claude-mem",
            "patterns": ["worker-service.cjs"],
            "purpose": "采集",
        }],
        "actions": {"session-context": "注入任务看板", "context": "注入历史"},
        "tools": tools,
    }))
    return path


class HookInventoryTest(unittest.TestCase):
    def test_cursor_flat_hook_is_classified_without_leaking_command(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            home = tmp_path / "home"
            hooks = home / ".cursor" / "hooks.json"
            hooks.parent.mkdir(parents=True)
            hooks.write_text(json.dumps({
                "version": 1,
                "hooks": {"sessionStart": [{
                    "command": "python3 ./hooks/task-hub.py session-start --secret NEVER_SHOW"
                }]},
            }))
            reg = _registry(tmp_path, [{
                "id": "cursor", "label": "Cursor",
                "sources": [{"kind": "json", "path": "~/.cursor/hooks.json", "scope": "用户"}],
                "expected": [{"component": "task-hub", "required": True}],
            }])
            with mock.patch.object(H, "HOME", home), \
                    mock.patch.object(H, "REGISTRY_PATH", reg), \
                    mock.patch.object(H, "PUSH_LOG", tmp_path / "none.log"):
                result = H.collect()
            hook = result[0]["hooks"][0]
            self.assertEqual(hook["component"], "task-hub")
            self.assertEqual(hook["action"], "session-context")
            self.assertEqual(hook["purpose"], "注入任务看板")
            self.assertNotIn("command", hook)
            self.assertNotIn("NEVER_SHOW", json.dumps(result))

    def test_codex_unapproved_hook_is_red(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            hook_path = tmp_path / "codex-hooks.json"
            hook_path.write_text(json.dumps({
                "hooks": {"SessionStart": [{
                    "hooks": [{"command": "node worker-service.cjs hook codex context"}]
                }]}
            }))
            config = tmp_path / "config.toml"
            config.write_text("[hooks.state]\n")
            reg = _registry(tmp_path, [{
                "id": "codex", "label": "Codex",
                "sources": [{"kind": "json", "path": str(hook_path), "scope": "插件"}],
                "approval": str(config),
                "expected": [{"component": "claude-mem", "required": True}],
            }])
            with mock.patch.object(H, "REGISTRY_PATH", reg), \
                    mock.patch.object(H, "PUSH_LOG", tmp_path / "none.log"):
                result = H.collect()
            self.assertEqual(result[0]["state"], "red")
            self.assertEqual(result[0]["summary"]["unapproved"], 1)
            self.assertEqual(result[0]["hooks"][0]["approval"], "missing")

    def test_required_component_missing_is_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            reg = _registry(tmp_path, [{
                "id": "claude", "label": "Claude Code", "sources": [],
                "expected": [{"component": "task-hub", "required": True}],
            }])
            with mock.patch.object(H, "REGISTRY_PATH", reg), \
                    mock.patch.object(H, "PUSH_LOG", tmp_path / "none.log"):
                result = H.collect()
            missing = result[0]["hooks"][0]
            self.assertEqual(result[0]["state"], "red")
            self.assertFalse(missing["configured"])
            self.assertEqual(missing["state"], "red")

    def test_project_only_hook_does_not_claim_global_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            hook_path = tmp_path / "hooks.json"
            hook_path.write_text(json.dumps({
                "hooks": {"beforeSubmitPrompt": [{
                    "command": "node worker-service.cjs hook cursor context"
                }]}
            }))
            reg = _registry(tmp_path, [{
                "id": "cursor", "label": "Cursor",
                "sources": [{"kind": "json", "path": str(hook_path),
                             "scope": "某项目"}],
                "expected": [{"component": "claude-mem", "required": True,
                              "scope": "user"}],
            }])
            with mock.patch.object(H, "REGISTRY_PATH", reg), \
                    mock.patch.object(H, "PUSH_LOG", tmp_path / "none.log"):
                result = H.collect()
            self.assertEqual(result[0]["state"], "amber")
            self.assertEqual(result[0]["summary"]["limited_scope"], 1)
            self.assertIn("其它 workspace", result[0]["hooks"][0]["coverage"])


if __name__ == "__main__":
    unittest.main()
