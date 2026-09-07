"""Offline deploy preflight tests: fake Docker only, never a NAS connection."""
import importlib.util
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

module_spec = importlib.util.spec_from_file_location(
    "quota_deploy", Path(__file__).resolve().parent.parent / "deploy/deploy-effective-quota.py")
D = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(D)


class DeployPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        controller = self.root / "controller.sh"
        controller.write_text("# fixture: never execute\n")
        state = self.root / "state.json"
        state.write_text('{}')
        self.spec = {"kind": "gateway", "docker_argv": ["fake-docker"],
            "container_name": "model-gateway", "container_id": "cid", "image_id": "sha256:image",
            "file_sha256": {str(controller): D.digest(controller.read_bytes())},
            "container_source_sha256": {"/app/model_gateway.py": "source-sha"},
            "controller": str(controller), "deployment_state_file": str(state),
            "deployment_state_sha256": D.digest(b'{}')}
        self.container = {"Id": "cid", "Image": "sha256:image", "State": {"Running": True}}

    def invoke(self):
        spec = self.root / "spec.json"
        spec.write_text(json.dumps(self.spec))
        def fake(argv, **_):
            self.assertEqual(argv[0], "fake-docker", "preflight must never run controller")
            if argv[1] == "inspect":
                return json.dumps([self.container]).encode()
            self.assertEqual(argv[1], "exec")
            return b"source-sha\n"
        with mock.patch.object(D, "run", side_effect=fake) as runner:
            with mock.patch("sys.argv", ["deploy-effective-quota.py", str(spec)]):
                D.main()
            return runner.call_args_list

    def test_default_is_read_only_and_creates_no_backups(self):
        calls = self.invoke()
        self.assertEqual(len(calls), 2)
        self.assertFalse(any(path.is_dir() for path in self.root.iterdir()))

    def test_container_image_drift_is_rejected(self):
        self.container["Image"] = "sha256:other"
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.invoke()

    def test_durable_state_drift_is_rejected(self):
        Path(self.spec["deployment_state_file"]).write_text('{"changed":true}')
        with self.assertRaisesRegex(RuntimeError, "identity drift"):
            self.invoke()


class OverlayStubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        names = ["compose.yml", "env", "Dockerfile", "topology.py"]
        files = {}
        for name in names:
            path = self.root / name
            path.write_text("fixture-" + name)
            files[str(path)] = D.digest(path.read_bytes())
        self.before = {"Id": "cid", "Image": "sha256:base", "State": {"Running": True},
            "Config": {"Env": ["SECRET=not-printed"], "Labels": {
                "com.docker.compose.service": "kg_hub_server", "com.docker.compose.project": "kg-hub"}},
            "HostConfig": {"ReadonlyRootfs": True}, "Mounts": [],
            "NetworkSettings": {"Networks": {"model-gateway-private": {}}}}
        self.current = copy.deepcopy(self.before)
        self.calls = []
        self.after_build_drift = False
        self.compose_failure = False
        self.add_network_on_up = False
        self.spec = {"kind": "overlay", "docker_argv": ["fake-docker"],
            "container_name": "kg-hub-server", "container_id": "cid", "image_id": "sha256:base",
            "file_sha256": files, "container_source_sha256": {"/app/topology.py": "source-sha"},
            "compose_matches_live_reviewed": True, "service": "kg_hub_server", "project": "kg-hub",
            "project_directory": str(self.root), "env_file": str(self.root / "env"),
            "compose_files": [str(self.root / "compose.yml")], "compose_config_sha256": D.digest(b'{}'),
            "runtime_sha256": D.digest(json.dumps(D.stable_runtime(self.before), sort_keys=True).encode()),
            "backup_parent": str(self.root), "overlay_context": str(self.root),
            "dockerfile": str(self.root / "Dockerfile"), "payload_relative_path": "topology.py",
            "target_tag": "kg-hub:quota-test"}

    def fake(self, argv, **_):
        self.calls.append(argv)
        self.assertEqual(argv[0], "fake-docker")
        if argv[1] == "inspect":
            return json.dumps([self.current]).encode()
        if argv[1] == "exec":
            return b"source-sha\n"
        if argv[1:3] == ["image", "inspect"]:
            return b'[{"Id":"sha256:candidate"}]'
        if argv[1] == "build":
            if self.after_build_drift:
                self.current["Image"] = "sha256:another-task"
            return b""
        if argv[1] == "compose":
            if "config" in argv:
                return b'{}'
            self.assertIn("up", argv)
            if self.compose_failure:
                raise RuntimeError("simulated compose error")
            self.current["Image"] = "sha256:candidate"
            self.current["Id"] = "new-cid"
            if self.add_network_on_up:
                self.current["NetworkSettings"]["Networks"]["model-gateway-private"] = {}
            return b""
        raise AssertionError("unexpected command: " + repr(argv))

    def invoke(self, apply=False):
        path = self.root / "spec.json"
        path.write_text(json.dumps(self.spec))
        with mock.patch.object(D, "run", side_effect=self.fake):
            with mock.patch("sys.argv", ["deploy-effective-quota.py", str(path)] + (["--apply"] if apply else [])):
                D.main()

    def test_overlay_default_does_not_build_or_create_backup(self):
        self.invoke()
        self.assertFalse(any("build" in call or "up" in call for call in self.calls))
        self.assertFalse(list(self.root.glob("effective-quota-*")))

    def test_apply_uses_exact_base_only_named_service_no_dependencies(self):
        self.invoke(apply=True)
        build = next(call for call in self.calls if call[1] == "build")
        self.assertIn("BASE_IMAGE=sha256:base", build)
        deploy = next(call for call in self.calls if "up" in call)
        self.assertIn("--no-deps", deploy)
        self.assertIn("--no-build", deploy)
        self.assertEqual(deploy[-1], "kg_hub_server")
        self.assertFalse(any("refinery" in " ".join(call) for call in self.calls))
        backup = next(self.root.glob("effective-quota-*"))
        self.assertEqual(json.loads((backup / "rollback.json").read_text()),
                         {"services": {"kg_hub_server": {"image": "sha256:base"}}})

    def test_new_external_image_during_build_is_never_overwritten(self):
        self.after_build_drift = True
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.invoke(apply=True)
        self.assertEqual(self.current["Image"], "sha256:another-task")
        self.assertFalse(any("up" in call for call in self.calls))

    def test_failed_compose_does_not_blindly_rollback(self):
        self.compose_failure = True
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            self.invoke(apply=True)
        self.assertEqual(len([call for call in self.calls if "up" in call]), 1)

    def test_unreviewed_payload_or_compose_is_rejected(self):
        self.spec["payload_relative_path"] = "refinery.py"
        with self.assertRaisesRegex(RuntimeError, "payload"):
            self.invoke()

    def test_dockerfiles_copy_only_one_expected_file(self):
        deploy = Path(__file__).resolve().parent.parent / "deploy"
        for name, payload in [("server", "topology.py"), ("exporter", "tools/export_gateway_usage.py")]:
            lines = (deploy / ("effective-quota-" + name + ".Dockerfile")).read_text().splitlines()
            self.assertEqual([line for line in lines if line.startswith("COPY ")],
                             ["COPY " + payload + " /app/" + payload])

    def test_only_watchdog_exact_private_network_addition_is_allowed(self):
        self.spec["allowed_network_additions"] = ["unexpected-network"]
        with self.assertRaisesRegex(RuntimeError, "network addition"):
            self.invoke()
        self.spec["allowed_network_additions"] = ["model-gateway-private"]
        with self.assertRaisesRegex(RuntimeError, "network addition"):
            self.invoke()  # server must not get this exception

    def test_runtime_order_only_differences_are_equivalent_but_values_are_not(self):
        left = D.stable_runtime(self.before)
        left["config"]["Env"] = ["A=1", "B=2"]
        left["host"]["Binds"] = ["/x:/x:ro", "/y:/y:ro"]
        right = copy.deepcopy(left)
        right["config"]["Env"].reverse()
        right["host"]["Binds"].reverse()
        self.assertTrue(D.runtime_equivalent(left, right))
        right["config"]["Env"][0] = "B=wrong"
        self.assertFalse(D.runtime_equivalent(left, right))
        left["config"]["Env"] = ["A=1", "A=2"]
        right = copy.deepcopy(left)
        right["config"]["Env"].reverse()
        self.assertFalse(D.runtime_equivalent(left, right))
        self.assertFalse(D.runtime_equivalent(left, left))
        left["config"]["Env"] = ["A=1"]
        left["host"]["Binds"] = ["/x:/same:ro", "/y:/same:ro"]
        self.assertFalse(D.runtime_equivalent(left, left))


if __name__ == "__main__":
    unittest.main()
