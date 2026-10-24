"""Host credential issue failure, concurrency, revocation and secret boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import scoped_tokens as issuer
from utils import token_auth


class IssuerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "access-control" / "read-tokens.json"
        self.addCleanup(token_auth._cache.update, stamp=None, entries=())

    def issue(self, name="dsh@nas"):
        output = self.root / "credentials" / name
        result = issuer.issue(self.registry, name=name, scopes=["extract"], token_out=output)
        return result, output

    def test_issue_only_writes_plaintext_exclusive_0600_and_preserves_existing_data(self):
        self.registry.parent.mkdir()
        original = {"name": "old-reader", "sha256": "a"*64, "scopes": ["read"], "custom": "keep"}
        self.registry.write_text(json.dumps({"tokens": [original], "metadata": {"preserve": True}}))
        result, output = self.issue()
        secret = output.read_text().strip()
        self.assertGreaterEqual(len(secret), 40)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o600)
        data = json.loads(self.registry.read_text())
        self.assertEqual(data["tokens"][0], original)
        self.assertEqual(data["metadata"], {"preserve": True})
        self.assertEqual(data["tokens"][1]["sha256"], token_auth.digest_for(secret))
        self.assertNotIn(secret, self.registry.read_text())
        self.assertNotIn(secret, json.dumps(result))
        with self.assertRaises(ValueError):
            self.issue()
        self.assertEqual(output.read_text().strip(), secret)
        with self.assertRaises(FileExistsError):
            issuer.issue(self.registry, name="other", scopes=["extract"], token_out=output)
        self.assertEqual(len(json.loads(self.registry.read_text())["tokens"]), 2)

    def test_registry_failure_removes_new_output_and_keeps_original_registry(self):
        self.issue("existing")
        before = self.registry.read_bytes()
        with patch.object(issuer.os, "replace", side_effect=OSError("injected atomic write failure")):
            with self.assertRaises(OSError):
                self.issue("failed-device")
        self.assertFalse((self.root / "credentials" / "failed-device").exists())
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(list(self.registry.parent.glob(".read-tokens.json.*")), [])

    def test_post_replace_fsync_failure_restores_existing_and_absent_registry(self):
        real_fsync = issuer.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory fsync failure after registry replace")
            return real_fsync(fd)
        for existed in (False, True):
            with self.subTest(existed=existed):
                if existed:
                    self.issue("existing")
                before = self.registry.read_bytes() if self.registry.exists() else None
                with patch.object(issuer.os, "fsync", side_effect=fail_directory):
                    with self.assertRaises(OSError):
                        self.issue("failed-after-replace")
                self.assertFalse((self.root / "credentials" / "failed-after-replace").exists())
                self.assertEqual(self.registry.read_bytes() if self.registry.exists() else None, before)

    def test_random_source_failure_does_not_create_or_leak_credential_output(self):
        with patch.object(issuer.secrets, "token_urlsafe", side_effect=RuntimeError("entropy unavailable")):
            with self.assertRaises(RuntimeError):
                self.issue("random-failed")
        self.assertFalse((self.root / "credentials" / "random-failed").exists())
        self.assertFalse(self.registry.exists())

    def test_concurrent_devices_are_preserved_and_tokens_are_distinct(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            outputs = list(pool.map(self.issue, ["device-" + str(i) for i in range(12)]))
        data = json.loads(self.registry.read_text())
        self.assertEqual(len(data["tokens"]), 12)
        self.assertEqual(len({output.read_text() for _, output in outputs}), 12)

    def test_invalid_registry_scope_and_symlink_refuse_without_secret_files(self):
        with self.assertRaises(ValueError):
            issuer.issue(self.registry, name="bad", scopes=["admin"], token_out=self.root / "bad")
        self.assertFalse((self.root / "bad").exists())
        self.registry.parent.mkdir(parents=True)
        for contents in ("invalid json", "[]", '{"tokens":{}}', '{"tokens":[1]}'):
            self.registry.write_text(contents)
            with self.assertRaises(ValueError):
                self.issue("bad-registry")
            self.assertEqual(self.registry.read_text(), contents)
            self.assertFalse((self.root / "credentials" / "bad-registry").exists())
        self.registry.unlink()
        self.registry.symlink_to(self.root / "missing-target")
        with self.assertRaises(ValueError):
            self.issue("bad-symlink")

    def test_absolute_cli_runs_from_unrelated_directory(self):
        stale = self.root / "stale" / "utils"
        stale.mkdir(parents=True)
        (stale / "__init__.py").write_text("")
        (stale / "token_auth.py").write_text("raise RuntimeError('stale import selected')")
        env = {**os.environ, "PYTHONPATH": str(stale.parent) + os.pathsep + str(issuer.CODE_ROOT)}
        completed = subprocess.run([sys.executable, str(Path(issuer.__file__).resolve()), "--help"],
                                   cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("issue", completed.stdout)
        self.assertIn("revoke", completed.stdout)

    def test_permission_is_exact_0600_even_with_restrictive_process_umask(self):
        self.registry.parent.mkdir(parents=True)
        (self.root / "credentials").mkdir()
        previous = os.umask(0o777)
        try:
            _, output = self.issue("strict-mask")
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o600)

    def test_revocation_is_immediate_and_cli_never_prints_secret_or_hash(self):
        stream = io.StringIO()
        output = self.root / "secret-file"
        with redirect_stdout(stream):
            code = issuer.main(["issue", "--registry", str(self.registry), "--name", "device", "--scope", "extract", "--token-out", str(output)])
        self.assertEqual(code, 0)
        secret = output.read_text().strip()
        with patch.object(token_auth, "ACCESS_CONTROL_PATH", self.registry):
            self.assertEqual(token_auth.principal_for(secret)["scopes"], ["extract"])
            with redirect_stdout(stream):
                self.assertEqual(issuer.main(["list", "--registry", str(self.registry)]), 0)
                self.assertEqual(issuer.main(["revoke", "--registry", str(self.registry), "--name", "device"]), 0)
            self.assertIsNone(token_auth.principal_for(secret))
        self.assertNotIn(secret, stream.getvalue())
        self.assertNotIn(token_auth.digest_for(secret), stream.getvalue())


if __name__ == "__main__":
    unittest.main()
