import os
import re
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "nas" / "redeploy.sh"
# 与 deploy/nas/redeploy.sh 的服务集保持一致。device_liveness 已从孤儿容器
# 补成 compose 服务；漏掉它，测试就盖不住它的暂存/回滚路径。
SERVICES = ("device_liveness", "ingester", "refinery", "kg_hub_server", "watchdog")

# 服务名 → 容器名。**必须与 deploy/nas/redeploy.sh 的 case 分支逐字一致**：
# 那里 device_liveness 有显式分支映到连字符 kg-hub-device-liveness，
# 而通用回退 "kg-hub-$service" 会算出下划线 kg-hub-device_liveness —— 名字对不上，
# docker ps --filter 就查不到容器，暂存/回滚整条路径静默失效。
CONTAINER = {"kg_hub_server": "kg-hub-server",
             "device_liveness": "kg-hub-device-liveness"}


def container_of(service: str) -> str:
    return CONTAINER.get(service, f"kg-hub-{service}")


def executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class RedeployTest(unittest.TestCase):
    def run_redeploy(
        self, *, fail_ups: int = 0, health_code: str = "200",
        fail_rename: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], Path, dict[str, str]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        temp = Path(temporary.name)
        bin_dir = temp / "bin"
        state = temp / "state"
        remote = temp / "remote"
        home = temp / "home"
        for directory in (bin_dir, state, remote, home):
            directory.mkdir()
        (state / "ghost").touch()

        old_states = {}
        for service in SERVICES:
            container = container_of(service)
            value = f"old-{service}|sha256:old-{service}|true\n"
            old_states[container] = value
            (state / f"container-{container}").write_text(value, encoding="ascii")

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
            printf '%s' "${HEALTH_CODE:-200}"
            """,
        )
        executable(bin_dir / "sleep", "#!/bin/sh\nexit 0\n")
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
            r"""
            #!/bin/sh
            set -eu
            state="$DEPLOY_TEST_STATE"
            printf '%s\n' "$*" >> "$state/docker.log"

            file_for_id() {
              wanted="$1"
              for path in "$state"/container-*; do
                [ -e "$path" ] || continue
                IFS='|' read -r id image running < "$path"
                if [ "$id" = "$wanted" ]; then
                  printf '%s\n' "$path"
                  return 0
                fi
              done
              return 1
            }
            set_running() {
              target="$1"
              path="$state/container-$target"
              if [ ! -e "$path" ]; then
                path=$(file_for_id "$target") || return 1
              fi
              IFS='|' read -r id image running < "$path"
              printf '%s|%s|true\n' "$id" "$image" > "$path"
            }
            remove_target() {
              target="$1"
              if [ "$target" = "123456789abc_kg-hub-refinery" ]; then
                rm -f "$state/ghost"
                return 0
              fi
              path="$state/container-$target"
              if [ ! -e "$path" ]; then
                path=$(file_for_id "$target") || return 0
              fi
              rm -f "$path"
            }

            if [ "$1" = compose ]; then
              for last do :; done
              case "$*" in
                *" build kg_hub_server") exit 0 ;;
                *" up -d --no-deps "*)
                  service="$last"
                  case "$service" in
                    kg_hub_server) container=kg-hub-server ;;
                    device_liveness) container=kg-hub-device-liveness ;;
                    *) container="kg-hub-$service" ;;
                  esac
                  count=0
                  [ ! -e "$state/up-count" ] || count=$(cat "$state/up-count")
                  count=$((count + 1))
                  printf '%s' "$count" > "$state/up-count"
                  if [ "$count" -le "${FAIL_UPS:-0}" ]; then
                    printf 'new-%s|sha256:new-%s|created\n' \
                      "$service" "$service" > "$state/container-$container"
                    touch "$state/ghost"
                    exit 1
                  fi
                  printf 'new-%s|sha256:new-%s|true\n' \
                    "$service" "$service" > "$state/container-$container"
                  exit 0
                  ;;
              esac
              exit 64
            fi

            if [ "$1" = ps ] && [ "$2" = -a ] && [ "$3" = --format ]; then
              [ ! -e "$state/ghost" ] || printf '%s\n' 123456789abc_kg-hub-refinery
              exit 0
            fi
            if [ "$1" = ps ] && [ "$2" = -a ] && [ "$3" = --filter ] \
                && [ "$4" = status=created ]; then
              for path in "$state"/container-*; do
                [ -e "$path" ] || continue
                IFS='|' read -r id image running < "$path"
                [ "$running" = created ] || continue
                basename=${path##*/container-}
                printf '%s\n' "$basename"
              done
              exit 0
            fi
            if [ "$1" = ps ] && [ "$2" = -a ] && [ "$3" = --filter ]; then
              name=${4#name=^/}
              name=${name%\$}
              path="$state/container-$name"
              if [ -e "$path" ]; then
                IFS='|' read -r id image running < "$path"
                printf '%s\n' "$id"
              fi
              exit 0
            fi
            if [ "$1" = inspect ]; then
              for last do :; done
              path=$(file_for_id "$last") || exit 1
              IFS='|' read -r id image running < "$path"
              [ "$running" = true ] && printf 'true\n' || printf 'false\n'
              exit 0
            fi
            if [ "$1" = stop ]; then
              path=$(file_for_id "$2") || exit 1
              IFS='|' read -r id image running < "$path"
              printf '%s|%s|false\n' "$id" "$image" > "$path"
              exit 0
            fi
            if [ "$1" = rename ]; then
              if [ "${FAIL_RENAME:-0}" = 1 ] \
                  && [ "$3" = kg-hub-device-liveness.rollback-before-redeploy ]; then
                exit 1
              fi
              path=$(file_for_id "$2") || exit 1
              mv "$path" "$state/container-$3"
              exit 0
            fi
            if [ "$1" = rm ] && [ "$2" = -f ]; then
              shift 2
              for target in "$@"; do remove_target "$target"; done
              exit 0
            fi
            if [ "$1" = start ]; then
              shift
              for target in "$@"; do set_running "$target"; done
              exit 0
            fi
            exit 64
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
                "FAIL_UPS": str(fail_ups),
                "FAIL_RENAME": "1" if fail_rename else "0",
                "HEALTH_CODE": health_code,
                # File-sync behavior is orthogonal to these deterministic tests.
                "FILES": "kg_hub_server.py",
                "KG_HUB_SKIP_SYNC": "1",
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
        return result, state, old_states

    def test_success_replaces_services_and_discards_staged_containers(self) -> None:
        result, state, _ = self.run_redeploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for service in SERVICES:
            container = container_of(service)
            current = (state / f"container-{container}").read_text("ascii")
            self.assertEqual(current, f"new-{service}|sha256:new-{service}|true\n")
            self.assertFalse((state / f"container-{container}.rollback-before-redeploy").exists())
        self.assertFalse((state / "ghost").exists())
        self.assertEqual((state / "up-count").read_text(), "5")
        docker_log = (state / "docker.log").read_text(encoding="utf-8")
        self.assertIn(
            "name=^/kg-hub-device-liveness$",
            docker_log,
        )
        # watchdog 必须晚于 server 启动，避免它在 server 重建窗口误报 server_down。
        # 只匹配 "up -d --no-deps <service>" 尾段：compose 前缀含 --env-file 与
        # model-gateway override，写死整条命令会让断言随部署参数变化而假失败。
        self.assertLess(
            docker_log.index("up -d --no-deps kg_hub_server"),
            docker_log.index("up -d --no-deps watchdog"),
        )

    def test_cleans_new_ghost_and_retries_once(self) -> None:
        result, state, _ = self.run_redeploy(fail_ups=1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # 首个被处理的服务（SERVICES[0]），不再硬编码服务名
        self.assertIn(f"{SERVICES[0]} 首次拉起失败", result.stdout)
        self.assertFalse((state / "ghost").exists())
        self.assertEqual((state / "up-count").read_text(), "6")

    def test_two_start_failures_restore_exact_prior_container_and_image(self) -> None:
        result, state, old_states = self.run_redeploy(fail_ups=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("恢复部署前的原容器", result.stdout)
        for container, prior in old_states.items():
            self.assertEqual(
                (state / f"container-{container}").read_text("ascii"), prior
            )
            self.assertFalse((state / f"container-{container}.rollback-before-redeploy").exists())
        self.assertEqual((state / "up-count").read_text(), "2")

    def test_failed_staging_rename_restarts_the_untouched_prior_container(self) -> None:
        result, state, old_states = self.run_redeploy(fail_rename=True)
        self.assertNotEqual(result.returncode, 0)
        # 暂存 rename 失败发生在**第一个**服务上，它的原容器必须完全没被动过
        container = container_of(SERVICES[0])
        self.assertEqual(
            (state / f"container-{container}").read_text("ascii"),
            old_states[container],
        )
        self.assertFalse(
            (state / f"container-{container}.rollback-before-redeploy").exists()
        )
        self.assertFalse((state / "up-count").exists())

    def test_final_non_200_health_exits_nonzero(self) -> None:
        result, _state, _ = self.run_redeploy(health_code="503")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("health=503", result.stdout)
        self.assertIn("最终健康检查失败", result.stderr)

    def test_default_manifest_is_the_explicit_minimal_cutover_set(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        match = re.search(r'^FILES="\$\{FILES:-(.*?)\}"$', source, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(
            match.group(1).split(),
            [
                "kg_hub_server.py",
                "graphiti_client.py",
                "kg_hub_env.py",
                "model_gateway_client.py",
                "docker-compose.yml",
                "deploy/model-gateway-network.override.yml",
            ],
        )

    def test_every_compose_invocation_uses_canonical_root_files(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(source.count("compose --env-file deploy/nas/.env"), 3)
        self.assertNotIn("compose -p kg-hub", source)
        self.assertNotIn("$REPO/.env}", source)
        self.assertIn("$REPO/deploy/nas/.env}", source)

    def test_sync_stages_an_archive_before_replacing_live_source(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('tar -cf - -C "$REPO" "$f" | ssh', source)
        self.assertIn('stage=\\$(mktemp -d', source)
        self.assertIn('tar -xf - -C \\"\\$stage\\"', source)
        self.assertIn('mv -f \\"\\$stage/$f\\" \\"$SRC/$f\\"', source)
        self.assertNotIn('cat "$REPO/$f" | ssh', source)


if __name__ == "__main__":
    unittest.main()
