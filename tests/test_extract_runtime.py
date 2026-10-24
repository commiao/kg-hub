import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
from unittest import mock
from tools import extract_runtime as command


class RuntimeCLITest(unittest.TestCase):
    def test_exec_preserves_argv_and_only_changes_child_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "child.json"
            source = "import os,json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps({'proxy':os.environ.get('HTTPS_PROXY'),'arg':sys.argv[2]}))"
            literal = "literal $(do-not-execute); 'quoted'"
            with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy:8123"}):
                for mode, expected in (("none", None), ("env-only", "http://proxy:8123")):
                    self.assertEqual(command.main(["exec", "--proxy-mode", mode, "--", sys.executable,
                        "-S", "-c", source, str(output), literal]), 0)
                    data = json.loads(output.read_text())
                    self.assertEqual(data, {"proxy": expected, "arg": literal})
                    self.assertEqual(os.environ["HTTPS_PROXY"], "http://proxy:8123")

    def test_inspect_does_not_probe_or_import_scoring_dependencies(self):
        with mock.patch.object(command, "probe_model", side_effect=AssertionError("not a canary")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(command.main(["inspect", "--executor", "codex", "--proxy-mode", "env-only"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "not_probed")

    def test_existing_output_rejected_before_any_paid_call(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "receipt.json"
            path.write_text("kept")
            with mock.patch.object(command, "probe_model", side_effect=AssertionError("must not call")), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                command.main(["probe", "--executor", "codex", "--model", "m", "--out", str(path)])
            self.assertEqual(path.read_text(), "kept")

    def test_manual_tool_never_claims_canary_success(self):
        result = command.probe_model("cursor", "m")
        self.assertFalse(result["model_verified"])
        self.assertEqual(result["status"], "unsupported_tool_side_probe_required")

    def test_fake_executable_real_pipe_nonce_reports_requested_only(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "codex"
            executable.write_text("#!/usr/bin/python3\nimport sys,json,pathlib\n"
                "payload=sys.stdin.read()\n"
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(payload)\n"
                "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2}}))\n")
            executable.chmod(0o700)
            with mock.patch.dict(os.environ, {"PATH": temp}):
                receipt = command.probe_model("codex", "requested-model", proxy_mode="env-only")
            self.assertEqual(receipt["status"], "ready")
            self.assertEqual(receipt["model_identity_status"], "requested-only")
            self.assertEqual(receipt["actual_models"], [])
            self.assertEqual(receipt["cost"]["tokens"], 5)
            self.assertFalse(receipt["cost"]["credits_known"])
