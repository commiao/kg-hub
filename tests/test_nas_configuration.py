"""NAS configuration helpers must persist secrets without exposing them."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModelGatewayTokenHelperTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        nas = root / "deploy/nas"
        nas.mkdir(parents=True)
        helper = nas / "configure-model-gateway-token.sh"
        helper.write_text(
            (ROOT / "deploy/nas/configure-model-gateway-token.sh").read_text("utf-8"),
            encoding="utf-8",
        )
        (nas / ".env.example").write_text(
            (ROOT / "deploy/nas/.env.example").read_text("utf-8"),
            encoding="utf-8",
        )
        return root, helper, nas / ".env"

    def run_helper(self, helper: Path, token_path: Path):
        env = {**os.environ, "PYTHON": sys.executable}
        return subprocess.run(
            ["sh", str(helper), str(token_path)], env=env,
            capture_output=True, text=True, check=False,
        )

    @staticmethod
    def dotenv(path: Path):
        values = {}
        for line in path.read_text("utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        return values

    def assert_strong_secret(self, value: str):
        self.assertRegex(value, r"\A[A-Za-z0-9_-]{32,256}\Z")
        self.assertGreaterEqual(len(set(value)), 12)

    def test_creates_and_atomically_updates_owner_only_env_without_output_secret(self):
        root, helper, env_path = self.fixture()
        token_path = root / "caller-token-kg-hub"
        first = secrets.token_urlsafe(32)
        token_path.write_text(first + "\n", encoding="ascii")
        token_path.chmod(0o600)

        result = self.run_helper(helper, token_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(first, result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
        document = env_path.read_text("utf-8")
        values = self.dotenv(env_path)
        self.assertIn("KG_HUB_DATA_ROOT=/volume2/4T/kg-hub-data", document)
        self.assertEqual(document.count("KG_HUB_MODEL_GATEWAY_TOKEN="), 1)
        self.assertIn("KG_HUB_MODEL_GATEWAY_TOKEN=" + first, document)
        managed = [
            values["FALKORDB_PASSWORD"],
            values["KG_HUB_API_TOKEN"],
            values["KG_HUB_MODEL_GATEWAY_TOKEN"],
        ]
        for value in managed:
            self.assert_strong_secret(value)
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertEqual(len(set(managed)), 3)

        # A normal rerun is stable: generated server secrets are not rotated.
        result = self.run_helper(helper, token_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(env_path.read_text("utf-8"), document)
        for value in managed:
            self.assertNotIn(value, result.stdout + result.stderr)

        second = secrets.token_urlsafe(32)
        env_path.write_text(
            document + "export KG_HUB_MODEL_GATEWAY_TOKEN=stale-duplicate\n",
            encoding="utf-8",
        )
        env_path.chmod(0o600)
        token_path.write_text(second + "\n", encoding="ascii")
        token_path.chmod(0o600)
        result = self.run_helper(helper, token_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(second, result.stdout + result.stderr)
        document = env_path.read_text("utf-8")
        self.assertNotIn(first, document)
        self.assertEqual(document.count("KG_HUB_MODEL_GATEWAY_TOKEN="), 1)
        self.assertIn("KG_HUB_MODEL_GATEWAY_TOKEN=" + second, document)
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

    def test_strong_custom_server_secrets_are_preserved(self):
        root, helper, env_path = self.fixture()
        caller_token = secrets.token_urlsafe(32)
        falkor_password = secrets.token_urlsafe(32)
        api_token = secrets.token_urlsafe(32)
        token_path = root / "caller-token-kg-hub"
        token_path.write_text(caller_token + "\n", encoding="ascii")
        token_path.chmod(0o600)
        example = env_path.with_name(".env.example").read_text("utf-8")
        example = re.sub(
            r"(?m)^FALKORDB_PASSWORD=.*$",
            "FALKORDB_PASSWORD=" + falkor_password,
            example,
        )
        example = re.sub(
            r"(?m)^KG_HUB_API_TOKEN=.*$",
            "KG_HUB_API_TOKEN=" + api_token,
            example,
        )
        env_path.write_text(example, encoding="utf-8")
        env_path.chmod(0o600)

        result = self.run_helper(helper, token_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = self.dotenv(env_path)
        self.assertEqual(values["FALKORDB_PASSWORD"], falkor_password)
        self.assertEqual(values["KG_HUB_API_TOKEN"], api_token)
        self.assertEqual(values["KG_HUB_MODEL_GATEWAY_TOKEN"], caller_token)
        for value in (caller_token, falkor_password, api_token):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_first_gateway_migration_preserves_legacy_server_secrets_only(self):
        root, helper, env_path = self.fixture()
        caller_token = secrets.token_urlsafe(32)
        # Current NAS history includes a short but already-live Falkor password.
        # Migrating provider credentials must not rotate it behind FalkorDB's back.
        falkor_password = "legacy-pass!"
        api_token = secrets.token_urlsafe(32)
        provider_token = "sk-provider-must-not-migrate"
        token_path = root / "caller-token-kg-hub"
        token_path.write_text(caller_token + "\n", encoding="ascii")
        token_path.chmod(0o600)
        legacy = root / ".env"
        legacy_document = "\n".join([
            "FALKORDB_PASSWORD=" + falkor_password,
            "KG_HUB_API_TOKEN=" + api_token,
            "ANTHROPIC_AUTH_TOKEN=" + provider_token,
            "ANTHROPIC_BASE_URL=https://provider.example/v1",
            "ANTHROPIC_MODEL=provider-model-name",
            "KG_HUB_DATA_ROOT=/legacy/data",
            "MODEL_GATEWAY_PRIVATE_NETWORK=legacy-network",
            "KG_HUB_PREDIGEST=1",
            "",
        ])
        legacy.write_text(legacy_document, encoding="utf-8")
        legacy.chmod(0o600)

        result = self.run_helper(helper, token_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = self.dotenv(env_path)
        self.assertEqual(values["FALKORDB_PASSWORD"], falkor_password)
        self.assertEqual(values["KG_HUB_API_TOKEN"], api_token)
        self.assertEqual(values["KG_HUB_MODEL_GATEWAY_TOKEN"], caller_token)
        self.assertEqual(values["KG_HUB_DATA_ROOT"], "/legacy/data")
        self.assertEqual(values["ANTHROPIC_BASE_URL"], "http://model-gateway:39000")
        self.assertEqual(values["ANTHROPIC_MODEL"], "kg_hub.entity_extract")
        self.assertEqual(
            values["MODEL_GATEWAY_PRIVATE_NETWORK"], "legacy-network"
        )
        self.assertEqual(values["KG_HUB_PREDIGEST"], "1")
        migrated = env_path.read_text("utf-8")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", migrated)
        self.assertNotIn(provider_token, migrated)
        self.assertEqual(legacy.read_text("utf-8"), legacy_document)
        self.assertIn("preserved legacy FALKORDB_PASSWORD", result.stdout)
        for value in (caller_token, falkor_password, api_token, provider_token):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_rerun_preserves_custom_deploy_configuration_and_strips_provider_keys(self):
        root, helper, env_path = self.fixture()
        caller_token = secrets.token_urlsafe(32)
        old_caller_token = secrets.token_urlsafe(32)
        falkor_password = secrets.token_urlsafe(32)
        api_token = secrets.token_urlsafe(32)
        provider_tokens = {
            "ANTHROPIC_AUTH_TOKEN": "sk-provider-anthropic-secret",
            "DASHSCOPE_API_KEY": "sk-provider-dashscope-secret",
            "OPENAI_API_KEY": "sk-provider-openai-secret",
        }
        token_path = root / "caller-token-kg-hub"
        token_path.write_text(caller_token + "\n", encoding="ascii")
        token_path.chmod(0o600)
        custom = {
            "FALKORDB_PASSWORD": falkor_password,
            "KG_HUB_API_TOKEN": api_token,
            "KG_HUB_MODEL_GATEWAY_TOKEN": old_caller_token,
            "KG_HUB_DATA_ROOT": "/volume9/custom-kg-data",
            "ANTHROPIC_BASE_URL": "https://gateway.nas.ts.net:39000",
            "ANTHROPIC_MODEL": "kg_hub.custom_extract",
            "MODEL_GATEWAY_PRIVATE_NETWORK": "custom-gateway-private",
            # Future/non-managed names must survive byte-for-byte too.
            "MODEL_GATEWAY_URL": "https://gateway.nas.ts.net:39000",
            "MODEL_GATEWAY_BUSINESS_KEY": "kg_hub.custom_extract",
            "KG_HUB_PREDIGEST": "1",
        }
        document = "# operator custom config\n" + "\n".join(
            f"{key}={value}" for key, value in custom.items()
        ) + "\n" + "\n".join(
            f"{key}={value}" for key, value in provider_tokens.items()
        ) + "\n"
        env_path.write_text(document, encoding="utf-8")
        env_path.chmod(0o600)

        result = self.run_helper(helper, token_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = self.dotenv(env_path)
        for key, value in custom.items():
            expected = caller_token if key == "KG_HUB_MODEL_GATEWAY_TOKEN" else value
            self.assertEqual(values[key], expected, key)
        migrated = env_path.read_text("utf-8")
        for key, value in provider_tokens.items():
            self.assertNotIn(key + "=", migrated)
            self.assertNotIn(value, migrated)
        for secret in (
            caller_token, old_caller_token, falkor_password, api_token,
            *provider_tokens.values(),
        ):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_caller_token_must_be_independent_from_server_credentials(self):
        root, helper, env_path = self.fixture()
        shared_token = secrets.token_urlsafe(32)
        falkor_password = secrets.token_urlsafe(32)
        token_path = root / "caller-token-kg-hub"
        token_path.write_text(shared_token + "\n", encoding="ascii")
        token_path.chmod(0o600)
        original = env_path.with_name(".env.example").read_text("utf-8")
        original = re.sub(
            r"(?m)^FALKORDB_PASSWORD=.*$",
            "FALKORDB_PASSWORD=" + falkor_password,
            original,
        )
        original = re.sub(
            r"(?m)^KG_HUB_API_TOKEN=.*$",
            "KG_HUB_API_TOKEN=" + shared_token,
            original,
        )
        env_path.write_text(original, encoding="utf-8")
        env_path.chmod(0o600)

        result = self.run_helper(helper, token_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(env_path.read_text("utf-8"), original)
        self.assertNotIn(shared_token, result.stdout + result.stderr)

    def test_caller_token_must_not_reuse_a_provider_credential(self):
        root, helper, env_path = self.fixture()
        shared_token = secrets.token_urlsafe(32)
        token_path = root / "caller-token-kg-hub"
        token_path.write_text(shared_token + "\n", encoding="ascii")
        token_path.chmod(0o600)
        original = env_path.with_name(".env.example").read_text("utf-8")
        original += f'export ANTHROPIC_AUTH_TOKEN="{shared_token}"\n'
        env_path.write_text(original, encoding="utf-8")
        env_path.chmod(0o600)

        result = self.run_helper(helper, token_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(env_path.read_text("utf-8"), original)
        self.assertNotIn(shared_token, result.stdout + result.stderr)

    def test_weak_existing_secret_fails_without_mutating_env(self):
        root, helper, env_path = self.fixture()
        token_path = root / "caller-token-kg-hub"
        token_path.write_text(secrets.token_urlsafe(32) + "\n", encoding="ascii")
        token_path.chmod(0o600)
        original = env_path.with_name(".env.example").read_text("utf-8").replace(
            "FALKORDB_PASSWORD=replace-with-real-password",
            "FALKORDB_PASSWORD=custom-but-weak",
        )
        env_path.write_text(original, encoding="utf-8")
        env_path.chmod(0o600)

        result = self.run_helper(helper, token_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(env_path.read_text("utf-8"), original)

    def test_symlinks_are_rejected_and_race_defense_is_present(self):
        root, helper, env_path = self.fixture()
        real_token = root / "real-token"
        token = secrets.token_urlsafe(32)
        real_token.write_text(token + "\n", encoding="ascii")
        real_token.chmod(0o600)
        token_link = root / "caller-token-kg-hub"
        token_link.symlink_to(real_token)
        result = self.run_helper(helper, token_link)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(env_path.exists())

        token_link.unlink()
        token_link.write_text(token + "\n", encoding="ascii")
        token_link.chmod(0o600)
        victim = root / "victim"
        victim.write_text("do-not-touch\n", encoding="utf-8")
        victim.chmod(0o600)
        env_path.symlink_to(victim)
        result = self.run_helper(helper, token_link)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(victim.read_text("utf-8"), "do-not-touch\n")

        source = helper.read_text("utf-8")
        self.assertIn('getattr(os, "O_NOFOLLOW", 0)', source)
        self.assertIn("os.fstat(fd)", source)
        self.assertIn("(opened.st_dev, opened.st_ino)", source)

    def test_exact_documented_first_run_command_creates_private_env(self):
        root, _helper, env_path = self.fixture()
        migration = (ROOT / "deploy/nas/MIGRATION.md").read_text("utf-8")
        documented = (
            "sh deploy/nas/configure-model-gateway-token.sh "
            "/absolute/path/to/caller-token-kg-hub"
        )
        self.assertIn(documented, migration)
        self.assertNotIn("cp deploy/nas/.env.example deploy/nas/.env", migration)

        caller_token = secrets.token_urlsafe(32)
        token_path = root / "caller-token-kg-hub"
        token_path.write_text(caller_token + "\n", encoding="ascii")
        token_path.chmod(0o600)
        command = documented.replace(
            "/absolute/path/to/caller-token-kg-hub", str(token_path)
        ).split()
        result = subprocess.run(
            command,
            cwd=root,
            env={**os.environ, "PYTHON": sys.executable},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
        values = self.dotenv(env_path)
        managed = [
            values["FALKORDB_PASSWORD"],
            values["KG_HUB_API_TOKEN"],
            values["KG_HUB_MODEL_GATEWAY_TOKEN"],
        ]
        self.assertEqual(len(set(managed)), 3)
        for value in managed:
            self.assert_strong_secret(value)
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_unsafe_or_weak_token_never_creates_env(self):
        root, helper, env_path = self.fixture()
        token_path = root / "caller-token-kg-hub"
        for token, mode in (("weak", 0o600), (secrets.token_urlsafe(32), 0o644)):
            with self.subTest(mode=oct(mode), length=len(token)):
                token_path.write_text(token + "\n", encoding="ascii")
                token_path.chmod(mode)
                result = self.run_helper(helper, token_path)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(token, result.stdout + result.stderr)
                self.assertFalse(env_path.exists())


class ComposeDataRootTests(unittest.TestCase):
    def test_all_persistent_data_binds_derive_from_required_single_root(self):
        compose_text = (ROOT / "docker-compose.yml").read_text("utf-8")
        example = (ROOT / "deploy/nas/.env.example").read_text("utf-8")
        self.assertIn("KG_HUB_DATA_ROOT=/volume2/4T/kg-hub-data", example)
        self.assertNotIn("/volume2/4T/kg-hub-data/", compose_text)
        # 宿主工具与他项目拥有的只读输入不是 kg-hub 的持久数据,不受单根约束;
        # 但必须在此显式列出且以 :ro 挂载,防止无意间把数据写到根外。
        external_readonly = {
            "/var/packages/Tailscale/target/bin/tailscale",
            "/volume2/@appdata/Tailscale/tailscaled.sock",
            "/volume1/docker/model-gateway-witness",   # 网关回滚见证库(成本看板只读)
        }
        persistent_binds = []
        for line in compose_text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("- ") or ":/" not in stripped:
                continue
            source = stripped[2:].split(":/", 1)[0]
            if "=" in source:
                continue
            if source == "/run/synostorage/disks":
                continue
            if source in external_readonly:
                self.assertTrue(stripped.endswith(":ro"), stripped)
                continue
            persistent_binds.append(source)
        self.assertTrue(persistent_binds)
        self.assertTrue(all(
            source.startswith("${KG_HUB_DATA_ROOT:?")
            for source in persistent_binds
        ), persistent_binds)

    def test_migration_and_redeploy_use_canonical_env_project_and_data_root(self):
        migration = (ROOT / "deploy/nas/MIGRATION.md").read_text("utf-8")
        redeploy = (ROOT / "deploy/nas/redeploy.sh").read_text("utf-8")
        self.assertIn("KG_HUB_DATA_ROOT=/volume2/4T/kg-hub-data", migration)
        self.assertNotIn("/volume1/docker/kg-hub/falkordb", migration)
        self.assertNotIn("cp deploy/nas/.env.example deploy/nas/.env", migration)
        self.assertIn(
            "sh deploy/nas/configure-model-gateway-token.sh "
            "/absolute/path/to/caller-token-kg-hub",
            migration,
        )
        for source in (migration, redeploy):
            self.assertIn("--env-file deploy/nas/.env", source)
            self.assertIn("-f docker-compose.yml", source)
            self.assertIn("-f deploy/model-gateway-network.override.yml", source)
            self.assertIn("-p kg-hub", source)

    def test_active_integration_guide_separates_server_and_client_credentials(self):
        guide = (ROOT / "docs/INTEGRATION-GUIDE.md").read_text("utf-8")
        self.assertIn(
            "sh deploy/nas/configure-model-gateway-token.sh "
            "/absolute/path/to/caller-token-kg-hub",
            guide,
        )
        self.assertIn("--env-file deploy/nas/.env", guide)
        self.assertIn("-f deploy/model-gateway-network.override.yml", guide)
        self.assertIn("KG_HUB_MODEL_GATEWAY_TOKEN", guide)
        self.assertIn("KG_HUB_API_TOKEN", guide)
        self.assertNotIn("所有工具共用一份 env", guide)


if __name__ == "__main__":
    unittest.main()
