"""Trust verdicts must come from Codex, including hashes, disabled hooks and load errors."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import codex_hooks as C
import hook_inventory as H
from tools import capture_probe as P


def hook(status="trusted", **overrides):
    value = {"key": "claude-mem@local:hooks/codex-hooks.json:post_compact:0:0",
             "eventName": "postCompact", "sourcePath": "/cache/13.39.0/hooks/codex-hooks.json",
             "source": "plugin", "pluginId": "claude-mem@local", "enabled": True,
             "currentHash": "sha256:current-definition", "trustStatus": status,
             "command": "node worker-service.cjs hook codex post-compact --secret NEVER_EXPOSE",
             "timeoutSec": 20}
    value.update(overrides)
    return value


class CodexTrustInventoryTests(unittest.TestCase):
    def inventory(self, hooks=None, error=None, warnings=None):
        tool = {"id": "codex", "label": "Codex", "expected": [
            {"component": "claude-mem", "required": True}]}
        components = [{"id": "claude-mem", "label": "claude-mem",
                       "patterns": ["worker-service.cjs"], "purpose": "采集"}]
        entry = {"hooks": [hook()] if hooks is None else hooks, "warnings": warnings or []}
        with mock.patch.object(C, "read_hooks", return_value=entry, side_effect=error):
            return H._codex_inventory(tool, components, {}, {}, {})

    def test_modified_hash_cannot_pass_due_to_existing_approval_key(self):
        result = self.inventory([hook("modified")])
        self.assertEqual(result["state"], "red")
        self.assertEqual(result["summary"]["hash_mismatch"], 1)
        self.assertEqual(result["summary"]["unapproved"], 1)
        self.assertIn("hash", result["hooks"][0]["status_detail"])

    def test_new_postcompact_is_unapproved_and_named_correctly(self):
        result = self.inventory([hook("untrusted")])
        self.assertEqual(result["state"], "red")
        self.assertEqual(result["hooks"][0]["event"], "PostCompact")
        self.assertEqual(result["hooks"][0]["approval"], "missing")

    def test_trusted_current_hash_and_full_identity_are_reported(self):
        result = self.inventory()
        self.assertEqual(result["state"], "green")
        self.assertEqual(result["hooks"][0]["current_hash"], "sha256:current-definition")
        self.assertEqual(result["hooks"][0]["key"], hook()["key"])
        self.assertNotIn("NEVER_EXPOSE", json.dumps(result))
        self.assertNotIn("command", result["hooks"][0])

    def test_inactive_cached_versions_are_not_scanned(self):
        with mock.patch.object(H, "_source_paths", side_effect=AssertionError("cache scan")):
            self.assertEqual(self.inventory()["summary"]["configured"], 1)

    def test_managed_hook_needs_no_user_hash(self):
        managed = hook("managed", source="system", pluginId=None)
        result = self.inventory([managed])
        self.assertEqual(result["state"], "green")
        self.assertEqual(result["hooks"][0]["approval"], "managed")

    def test_disabled_required_component_is_not_healthy(self):
        result = self.inventory([hook(enabled=False)])
        self.assertEqual(result["state"], "red")
        self.assertEqual(result["summary"]["disabled"], 1)
        self.assertEqual(result["summary"]["missing"], 1)

    def test_inspection_error_cannot_fall_back_to_cached_green(self):
        result = self.inventory(error=C.HookInspectionError("unavailable"))
        self.assertEqual(result["state"], "red")
        self.assertEqual(result["hooks"][0]["approval"], "unknown")

    def test_empty_runtime_inventory_detects_unloaded_plugin(self):
        result = self.inventory([])
        self.assertEqual(result["state"], "red")
        self.assertEqual(result["summary"]["missing"], 1)

    def test_unknown_trust_status_is_not_assumed_trusted(self):
        self.assertEqual(self.inventory([hook("new-status")])["state"], "red")

    def test_warnings_do_not_produce_unqualified_green(self):
        self.assertEqual(self.inventory(warnings=["configuration warning"])["state"], "amber")

    def test_topology_and_inventory_share_exact_verdict_without_reinspection(self):
        inventory = self.inventory([hook("modified")])
        with mock.patch.object(C, "read_hooks", side_effect=AssertionError("second scan")):
            node = next(n for n in P.probe_hooks({}, [inventory]) if n["id"] == "hook:codex")
        self.assertEqual(node["state"], inventory["state"])
        self.assertIn("hash", node["detail"])


class NativeHookProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = str(self.root.resolve())
        self.binary = self.root / "codex"

    def tearDown(self):
        self.tmp.cleanup()

    def server(self, entry, mode="normal"):
        # A real subprocess exercises streaming, errors, EOF and timeout cleanup.
        source = ("#!" + sys.executable + "\nimport sys,json,time\n"
                  + "entry=" + repr(entry) + "\nmode=" + repr(mode) + "\n"
                  + "for line in sys.stdin:\n"
                    " r=json.loads(line)\n"
                    " if r.get('method')=='initialize':\n"
                    "  print(json.dumps({'id':r['id'],'result':{}}),flush=True)\n"
                    " elif r.get('method')=='hooks/list':\n"
                    "  if mode=='eof': break\n"
                    "  if mode=='timeout': time.sleep(5);break\n"
                    "  print(json.dumps({'id':r['id'],'result':{'data':[entry]}}),flush=True)\n")
        self.binary.write_text(source)
        self.binary.chmod(0o755)
        return mock.patch.object(C, "find_codex", return_value=str(self.binary))

    def test_native_hash_and_plugin_selection_survive_stream_transport(self):
        entry = {"cwd": self.cwd, "hooks": [hook()], "errors": [], "warnings": []}
        with self.server(entry):
            result = C.read_hooks(self.cwd)
        self.assertEqual(result, entry)

    def test_native_load_errors_are_never_silent(self):
        entry = {"cwd": self.cwd, "hooks": [], "errors": [{"message": "bad plugin"}]}
        with self.server(entry), self.assertRaises(C.HookInspectionError):
            C.read_hooks(self.cwd)

    def test_missing_current_hash_is_an_inspection_failure(self):
        item = hook(); del item["currentHash"]
        entry = {"cwd": self.cwd, "hooks": [item], "errors": []}
        with self.server(entry), self.assertRaises(C.HookInspectionError):
            C.read_hooks(self.cwd)

    def test_eof_and_timeout_are_explicit_and_children_reaped(self):
        for mode in ("eof", "timeout"):
            with self.subTest(mode=mode), self.server({}, mode), self.assertRaises(C.HookInspectionError):
                C.read_hooks(self.cwd, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
