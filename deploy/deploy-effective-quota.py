#!/usr/bin/env python3
"""Pinned, opt-in NAS deployment entry point. No production defaults or discovery writes.

See docs/effective-quota-20260907.md for required operator-reviewed pins.
preflight is read-only; apply is explicit. This file has NOT been run on NAS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def run(argv, **kwargs):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, **kwargs)
    if result.returncode:
        # Compose stderr/stdout may contain expanded environment/credentials.
        raise RuntimeError("command failed (output suppressed); inspect privately")
    return result.stdout


def stable_runtime(item):
    """Everything an image-only overlay must preserve, excluding runtime identities."""
    config = item["Config"]
    host = item["HostConfig"]
    return {
        "config": {key: config.get(key) for key in (
            "Env", "Cmd", "Entrypoint", "User", "WorkingDir", "Healthcheck",
            "StopSignal", "StopTimeout", "ExposedPorts", "Volumes")},
        "host": {key: host.get(key) for key in (
            "Binds", "Mounts", "PortBindings", "RestartPolicy", "ReadonlyRootfs",
            "CapAdd", "CapDrop", "SecurityOpt", "Tmpfs", "Privileged", "Devices",
            "Memory", "NanoCpus", "PidsLimit", "Init", "NetworkMode", "LogConfig")},
        "mounts": sorted((mount["Type"], mount["Source"], mount["Destination"],
                          mount["RW"]) for mount in item.get("Mounts", [])),
        "networks": sorted(item["NetworkSettings"]["Networks"]),
    }


def private_json(path, data):
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)


def runtime_equivalent(expected, actual):
    """Docker/Compose reorder env and bind lists; preserve their exact values."""
    def normalize(runtime):
        value = json.loads(json.dumps(runtime))
        env = value["config"].get("Env") or []
        # Duplicate names make ordering meaningful: reject rather than guess.
        if len({entry.split("=", 1)[0] for entry in env}) != len(env):
            return None
        value["config"]["Env"] = sorted(env)
        if isinstance(value["host"].get("Binds"), list):
            binds = value["host"]["Binds"]
            parts = [entry.split(":") for entry in binds]
            if any(len(item) not in {2, 3} for item in parts):
                return None
            if len({item[1] for item in parts}) != len(parts):
                return None
            value["host"]["Binds"] = sorted(binds)
        return value
    if expected.get("mounts") != actual.get("mounts"):
        return False
    left, right = normalize(expected), normalize(actual)
    return left is not None and right is not None and left == right


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    docker = spec["docker_argv"]
    if not isinstance(docker, list) or not docker:
        raise RuntimeError("docker_argv required")

    def inspect():
        return json.loads(run(docker + ["inspect", spec["container_name"]]))[0]

    def file_pins():
        for filename, sha in spec["file_sha256"].items():
            path = Path(filename)
            if not path.is_absolute() or path.is_symlink() or digest(path.read_bytes()) != sha:
                raise RuntimeError("reviewed file drift: " + filename)

    def compare_live():
        item = inspect()
        if (item["Id"] != spec["container_id"] or item["Image"] != spec["image_id"]
                or not item["State"]["Running"]):
            raise RuntimeError("live container/image changed; re-review required")
        for source, sha in spec["container_source_sha256"].items():
            actual = run(docker + ["exec", item["Id"], "python", "-c",
                "import hashlib,pathlib,sys;print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())",
                source]).decode().strip()
            if actual != sha:
                raise RuntimeError("live source changed: " + source)
        return item

    file_pins()
    before = compare_live()
    if spec["kind"] == "gateway":
        # The existing controller owns deployment/policy locks, draining, old
        # evidence snapshots, rollback and final host+container identity checks.
        controller = spec["controller"]
        if controller not in spec["file_sha256"]:
            raise RuntimeError("controller must be hash-pinned")
        env = dict(os.environ, **spec.get("controller_environment", {}))
        state = json.loads(Path(spec["deployment_state_file"]).read_text())
        if digest(json.dumps(state, sort_keys=True).encode()) != spec["deployment_state_sha256"]:
            raise RuntimeError("durable deployment config/identity drift")
        if not args.apply:
            print("GATEWAY_PREFLIGHT_OK; no changes")
            return
        file_pins()
        compare_live()
        run(["sh", controller, "--confirm-independent-witness",
             "I_ACKNOWLEDGE_INDEPENDENT_WITNESS_ROLLBACK_DOMAIN", "cutover"], env=env)
        print("GATEWAY_CONTROLLER_CUTOVER_OK; verify fresh readiness before next phase")
        return

    if spec["kind"] != "overlay" or spec.get("compose_matches_live_reviewed") is not True:
        raise RuntimeError("overlay requires reviewed compose-to-live equivalence")
    service = spec["service"]
    network_additions = spec.get("allowed_network_additions", [])
    if network_additions and (service != "watchdog"
                              or network_additions != ["model-gateway-private"]):
        raise RuntimeError("only approved watchdog private-network addition is allowed")
    labels = before["Config"].get("Labels") or {}
    if (labels.get("com.docker.compose.service") != service
            or labels.get("com.docker.compose.project") != spec["project"]):
        raise RuntimeError("compose ownership mismatch")
    # Compose emits the effective config to memory only; never print it.
    compose = docker + ["compose", "--project-directory", spec["project_directory"],
                        "--env-file", spec["env_file"], "-p", spec["project"]]
    for filename in spec["compose_files"]:
        if filename not in spec["file_sha256"]:
            raise RuntimeError("all compose files must be pinned")
        compose += ["-f", filename]
    if spec["env_file"] not in spec["file_sha256"]:
        raise RuntimeError("compose environment must be pinned")
    config_hash = digest(run(compose + ["config", "--format", "json"]))
    if config_hash != spec["compose_config_sha256"]:
        raise RuntimeError("effective compose configuration drift")
    if digest(json.dumps(stable_runtime(before), sort_keys=True).encode()) != spec["runtime_sha256"]:
        raise RuntimeError("running environment/mount/network drift")
    context = Path(spec["overlay_context"])
    dockerfile = spec["dockerfile"]
    payload = str(context / spec["payload_relative_path"])
    if (spec["payload_relative_path"] not in {"topology.py", "tools/export_gateway_usage.py"}
            or payload not in spec["file_sha256"]):
        raise RuntimeError("exact overlay payload must be hash-pinned")
    if dockerfile not in spec["file_sha256"]:
        raise RuntimeError("overlay Dockerfile must be hash-pinned")
    if not args.apply:
        print("OVERLAY_PREFLIGHT_OK; no changes")
        return

    backup_root = Path(spec["backup_parent"])
    if not backup_root.is_absolute() or not backup_root.is_dir():
        raise RuntimeError("explicit existing backup parent required")
    backup = Path(tempfile.mkdtemp(prefix="effective-quota-", dir=backup_root))
    private_json(backup / "reviewed-spec.json", spec)
    private_json(backup / "runtime-before.json", before)
    private_json(backup / "rollback.json", {"services": {service: {"image": before["Image"]}}})
    run(docker + ["build", "--network", "none", "--build-arg", "BASE_IMAGE=" + before["Image"],
                  "-t", spec["target_tag"], "-f", dockerfile, str(context)])
    target = json.loads(run(docker + ["image", "inspect", spec["target_tag"]]))[0]["Id"]
    private_json(backup / "candidate.json", {"services": {service: {"image": target}}})
    file_pins()
    compare_live()
    if digest(run(compose + ["config", "--format", "json"])) != config_hash:
        raise RuntimeError("compose drift during build")
    run(compose + ["-f", str(backup / "candidate.json"), "up", "-d", "--no-deps",
                   "--no-build", "--timeout", "120", service])
    after = inspect()
    expected_runtime = stable_runtime(before)
    expected_runtime["networks"] = sorted(set(expected_runtime["networks"]) | set(network_additions))
    if after["Image"] != target or not runtime_equivalent(expected_runtime, stable_runtime(after)):
        # No automatic rollback using an unverified/moved target or stale config.
        # Keep backup and report failed; operator re-CASes before rollback below.
        raise RuntimeError("post-deploy image/runtime mismatch; backup=" + str(backup))
    private_json(backup / "runtime-after.json", after)
    print("OVERLAY_DEPLOYED backup=" + str(backup) + "; business acceptance still required")


if __name__ == "__main__":
    main()
