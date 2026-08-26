"""host-side producer 必须原子更新，并在采样失败时保留 last-good。"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCER = ROOT / "deploy" / "monitoring" / "nas" / "tailscale-liveness-snapshot.sh"


class ProducerTests(unittest.TestCase):
    def test_success_replaces_snapshot_and_failure_retains_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            fake = temp / "tailscale"
            fake.write_text(textwrap.dedent("""\
                #!/bin/sh
                [ -z "${ARGS_OUT:-}" ] || printf '%s\\n' "$*" > "$ARGS_OUT"
                [ "${FAIL:-0}" != 1 ] || exit 9
                if [ "${MALFORMED:-0}" = 1 ]; then
                  printf '%s\\n' '{malformed'
                  exit 0
                fi
                if [ "${WRONG_SCHEMA:-0}" = 1 ]; then
                  printf '%s\\n' '{"BackendState":"Running","Peer":[]}'
                  exit 0
                fi
                if [ "${STOPPED:-0}" = 1 ]; then
                  printf '%s\\n' '{"BackendState":"Stopped","Peer":{"k":{"HostName":"mac","Online":true}}}'
                  exit 0
                fi
                printf '%s\\n' '{"BackendState":"Running","Peer":{"k":{"HostName":"mac","Online":true}}}'
            """), encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            out = temp / "config" / "tailscale-status.json"
            args_out = temp / "tailscale.args"
            env = os.environ.copy()
            env.update({"KG_HUB_TAILSCALE_BIN": str(fake),
                        "KG_HUB_DEVICE_LIVENESS_PATH": str(out),
                        "KG_HUB_TAILSCALE_SOCKET": "/tailscale-var/tailscaled.sock",
                        "ARGS_OUT": str(args_out)})

            ok = subprocess.run(["sh", str(PRODUCER)], env=env, text=True,
                                capture_output=True, check=False)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            good = out.read_text(encoding="utf-8")
            self.assertIn('"Online":true', good)
            self.assertEqual(
                args_out.read_text(encoding="utf-8").strip(),
                "--socket=/tailscale-var/tailscaled.sock status --json")
            self.assertEqual(list(out.parent.glob("*.tmp.*")), [])

            env["FAIL"] = "1"
            failed = subprocess.run(["sh", str(PRODUCER)], env=env, text=True,
                                    capture_output=True, check=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(out.read_text(encoding="utf-8"), good)
            self.assertEqual(list(out.parent.glob("*.tmp.*")), [])

            env.pop("FAIL")
            env["MALFORMED"] = "1"
            malformed = subprocess.run(["sh", str(PRODUCER)], env=env, text=True,
                                       capture_output=True, check=False)
            self.assertNotEqual(malformed.returncode, 0)
            self.assertEqual(out.read_text(encoding="utf-8"), good)
            self.assertEqual(list(out.parent.glob("*.tmp.*")), [])

            env.pop("MALFORMED")
            env["WRONG_SCHEMA"] = "1"
            wrong_schema = subprocess.run(["sh", str(PRODUCER)], env=env, text=True,
                                          capture_output=True, check=False)
            self.assertNotEqual(wrong_schema.returncode, 0)
            self.assertEqual(out.read_text(encoding="utf-8"), good)

            env.pop("WRONG_SCHEMA")
            env["STOPPED"] = "1"
            stopped = subprocess.run(["sh", str(PRODUCER)], env=env, text=True,
                                     capture_output=True, check=False)
            self.assertNotEqual(stopped.returncode, 0)
            self.assertEqual(out.read_text(encoding="utf-8"), good)


if __name__ == "__main__":
    unittest.main()
