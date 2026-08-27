import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "nas" / "redeploy.sh"


def executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class RedeployTest(unittest.TestCase):
    def run_redeploy(self, fail_first_up: bool = False) -> tuple[subprocess.CompletedProcess[str], Path]:
        temp = Path(tempfile.mkdtemp())
        bin_dir = temp / "bin"
        state = temp / "state"
        remote = temp / "remote"
        home = temp / "home"
        for directory in (bin_dir, state, remote, home):
            directory.mkdir()
        (state / "ghost").touch()

        executable(
            bin_dir / "ssh",
            """
            #!/bin/sh
            for last do :; done
            exec sh -c "$last"
            """,
        )
        executable(
            bin_dir / "curl",
            """
            #!/bin/sh
            printf 200
            """,
        )
        executable(
            bin_dir / "sleep",
            """
            #!/bin/sh
            exit 0
            """,
        )
        executable(
            bin_dir / "timeout",
            """
            #!/bin/sh
            if [ "$1" = "-k" ]; then
              shift 2
            fi
            shift
            exec "$@"
            """,
        )
        executable(
            bin_dir / "docker",
            """
            #!/bin/sh
            set -eu
            state="$DEPLOY_TEST_STATE"
            printf '%s\\n' "$*" >> "$state/docker.log"
            case "$*" in
              "compose build kg_hub_server") exit 0 ;;
              "ps -a --format {{.Names}}")
                [ ! -e "$state/ghost" ] || printf '%s\\n' 123456789abc_kg-hub-refinery
                ;;
              "ps -a --filter name=^/kg-hub-"*" -q")
                printf '%s\\n' old-container
                ;;
              "rm -f "*)
                rm -f "$state/ghost"
                ;;
              "compose -p kg-hub up -d --no-deps "*)
                service="${7}"
                count=0
                [ ! -e "$state/up-count" ] || count=$(cat "$state/up-count")
                count=$((count + 1))
                printf '%s' "$count" > "$state/up-count"
                if [ "${FAIL_FIRST_UP:-0}" = 1 ] && [ "$count" = 1 ]; then
                  touch "$state/ghost"
                  exit 1
                fi
                [ ! -e "$state/ghost" ] || exit 42
                touch "$state/up-$service"
                ;;
              "ps -a --filter status=created --format {{.Names}}")
                printf '%s\\n' kg-hub-watchdog
                ;;
              "start kg-hub-watchdog")
                touch "$state/started"
                ;;
              *) exit 64 ;;
            esac
            """,
        )

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "HOME": str(home),
                "KG_HUB_NAS_SRC": str(remote),
                "KG_HUB_DOCKER": str(bin_dir / "docker"),
                "KG_HUB_REPO": str(ROOT),
                "DEPLOY_TEST_STATE": str(state),
                "FAIL_FIRST_UP": "1" if fail_first_up else "0",
                "FILES": "kg_hub_server.py",
            }
        )
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result, state

    def test_removes_existing_ghost_before_compose_up(self) -> None:
        result, state = self.run_redeploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for service in ("device_liveness", "watchdog", "ingester", "refinery", "kg_hub_server"):
            self.assertTrue((state / f"up-{service}").exists())
        self.assertTrue((state / "started").exists())
        self.assertFalse((state / "ghost").exists())
        self.assertEqual((state / "up-count").read_text(), "5")
        docker_log = (state / "docker.log").read_text(encoding="utf-8")
        self.assertIn(
            "name=^/kg-hub-device-liveness$",
            docker_log,
        )
        self.assertLess(
            docker_log.index("compose -p kg-hub up -d --no-deps kg_hub_server"),
            docker_log.index("compose -p kg-hub up -d --no-deps watchdog"),
        )

    def test_cleans_new_ghost_and_retries_once(self) -> None:
        result, state = self.run_redeploy(fail_first_up=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("device_liveness 首次拉起失败", result.stdout)
        for service in ("device_liveness", "watchdog", "ingester", "refinery", "kg_hub_server"):
            self.assertTrue((state / f"up-{service}").exists())
        self.assertFalse((state / "ghost").exists())
        self.assertEqual((state / "up-count").read_text(), "6")


if __name__ == "__main__":
    unittest.main()
