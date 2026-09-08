"""Name-only Compose mocks are replaced by the disabled-entrypoint contract.

Real Compose label-loss evidence is retained in tests/integration, opt-in only.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "nas" / "redeploy.sh"


class RedeployGuardTests(unittest.TestCase):
    def assert_blocked(self, updates=None, arguments=(), source=False):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            trace = directory / "external-actions"
            for command in ("ssh", "docker", "curl", "tar", "sleep", "timeout", "sudo",
                            "scp", "rsync", "mv", "rm", "mkdir", "chmod", "cp"):
                stub = directory / command
                stub.write_text('#!/bin/sh\nprintf called >> "$ACTION_TRACE"\nexit 91\n')
                stub.chmod(0o700)
            env = dict(os.environ, PATH=str(directory), ACTION_TRACE=str(trace))
            env.update(updates or {})
            command = (["/bin/bash", "-c", 'source "$1"', "guard-test", str(SCRIPT)]
                       if source else ["/bin/bash", str(SCRIPT), *arguments])
            result = subprocess.run(command, env=env,
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("DEPLOY_BLOCKED", result.stderr)
            self.assertIn("未同步、构建或重启", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(trace.exists(), "disabled entrypoint executed an external action")

    def test_default_invocation_has_no_external_actions(self):
        self.assert_blocked()

    def test_skip_sync_cannot_bypass_guard(self):
        self.assert_blocked({"KG_HUB_SKIP_SYNC": "1"})

    def test_legacy_environment_overrides_cannot_bypass_guard(self):
        self.assert_blocked({"KG_HUB_NAS_SSH": "fixture.invalid", "KG_HUB_DOCKER": "docker",
                             "KG_HUB_NAS_SRC": "/not-used", "KG_HUB_REPO": "/not-used",
                             "FILES": "untrusted-file", "KG_HUB_SSH_ALLOW_PROXYJUMP": "1"})

    def test_no_force_opt_out(self):
        self.assert_blocked({"KG_HUB_FORCE": "1", "KG_HUB_ALLOW_UNSAFE_REDEPLOY": "1"})

    def test_cli_flags_do_not_execute_deployment(self):
        for arguments in (("--help",), ("--force",), ("--apply",), ("kg_hub_server",)):
            with self.subTest(arguments=arguments):
                self.assert_blocked(arguments=arguments)

    def test_ordinary_source_exits_without_external_actions(self):
        self.assert_blocked(source=True)

    def test_shell_syntax(self):
        subprocess.run(["/bin/bash", "-n", str(SCRIPT)], check=True)

    # Retained static constraints for the unreachable legacy implementation.
    # They are not evidence that its deployment or rollback can safely execute.
    def test_legacy_default_manifest_is_explicit_minimal_cutover_set(self):
        source = SCRIPT.read_text(encoding="utf-8")
        match = re.search(r'^FILES="\$\{FILES:-(.*?)\}"$', source, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).split(), ["kg_hub_server.py", "graphiti_client.py",
            "kg_hub_env.py", "model_gateway_client.py", "docker-compose.yml",
            "deploy/model-gateway-network.override.yml"])

    def test_legacy_compose_invocations_use_canonical_root_files(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(source.count("compose --env-file deploy/nas/.env"), 3)
        self.assertNotIn("compose -p kg-hub", source)
        self.assertNotIn("$REPO/.env}", source)
        self.assertIn("$REPO/deploy/nas/.env}", source)

    def test_legacy_sync_stages_archive_before_replacing_live_source(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('tar -cf - -C "$REPO" "$f" | ssh', source)
        self.assertIn('stage=\\$(mktemp -d', source)
        self.assertIn('tar -xf - -C \\"\\$stage\\"', source)
        self.assertIn('mv -f \\"\\$stage/$f\\" \\"$SRC/$f\\"', source)
        self.assertNotIn('cat "$REPO/$f" | ssh', source)


if __name__ == "__main__":
    unittest.main()
