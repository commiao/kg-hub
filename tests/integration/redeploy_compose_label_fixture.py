"""Opt-in NAS Compose label reproduction; no production runtime is inspected.

Only a pre-existing, pinned Alpine image and uniquely labelled no-network,
no-volume containers are allowed. Does not invoke redeploy.sh or its cleanup.
"""
import argparse
import json
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path


IMAGE = "sha256:bf8527eb54c3680e728d5b4b383a8ba730d72dae7236fbc8dff97ed6b224a731"
DOCKER = ["sudo", "-n", "/var/packages/ContainerManager/target/usr/bin/docker"]
LABEL = "t0046.rollback-fixture"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-isolated-nas-fixture", action="store_true", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    nonce = "t0046-rollback-fixture-20260908-" + uuid.uuid4().hex[:12]
    events = []

    def call(command, *, document=None, check=True):
        result = subprocess.run(
            ["ssh", "-o", "ProxyJump=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "commiao@100.123.208.32", shlex.join(DOCKER + command)],
            input=json.dumps(document) if document is not None else None,
            text=True, capture_output=True, timeout=60,
        )
        events.append({"command": command, "returncode": result.returncode,
                       "stdout": result.stdout, "stderr": result.stderr})
        if check and result.returncode:
            raise RuntimeError("fixture Docker command failed: " + shlex.join(command))
        return result

    def inspect(target, *, require_limits=True):
        result = call(["inspect", target], check=False)
        # This helper only receives the fixed image, nonce-prefixed names, or IDs
        # returned by the exact nonce label query. Never serialize generic env.
        raw = json.loads(result.stdout)[0] if result.returncode == 0 else None
        events[-1]["stdout"] = "<projected below>"
        if raw is None:
            return None
        if target == IMAGE:
            assert raw["Id"] == IMAGE and not raw["Config"].get("Volumes")
            assert raw["Config"]["Env"] == ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
            return {"id": raw["Id"], "volumes": None, "env_keys": ["PATH"]}
        assert raw["Config"]["Labels"].get(LABEL) == nonce
        assert raw["Config"]["Labels"].get("com.docker.compose.project") == nonce
        assert not raw["Mounts"] and not raw["HostConfig"].get("PortBindings")
        assert raw["HostConfig"]["NetworkMode"] == "none"
        assert not raw["HostConfig"]["Privileged"]
        if require_limits:
            assert raw["HostConfig"]["Memory"] == 33554432
            assert raw["HostConfig"]["CpuShares"] == 2
            # Synology's installed Compose/engine drops both pids spellings.
            # This fixture was explicitly approved without a PID hard cap:
            # /bin/sleep only, actual 32 MiB limit, no network/mounts/credentials.
            # Record the actual value; never claim that pids=16 took effect.
        return {"id": raw["Id"], "name": raw["Name"], "labels": raw["Config"]["Labels"],
                "running": raw["State"]["Running"], "status": raw["State"]["Status"],
                "mounts": [], "network": "none", "ports": [],
                "memory": raw["HostConfig"]["Memory"],
                "cpu_shares": raw["HostConfig"]["CpuShares"],
                "pids_limit": raw["HostConfig"].get("PidsLimit")}

    def model(entrypoint):
        return {"services": {"fixture": {
            "image": IMAGE, "container_name": nonce + "-service", "network_mode": "none",
            "entrypoint": entrypoint, "command": [], "labels": {LABEL: nonce},
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "mem_limit": "32m", "cpu_shares": 2, "pids_limit": 16, "restart": "no",
            "deploy": {"resources": {"limits": {"pids": 16}}},
        }}}

    compose = ["compose", "--env-file", "/dev/null", "--project-directory", "/tmp",
               "-f", "-", "-p", nonce, "up", "-d", "--no-deps", "--no-build",
               "--pull", "never", "fixture"]
    evidence = {"project": nonce, "started_at": datetime.now(timezone.utc).isoformat(), "events": events}
    try:
        evidence["image"] = inspect(IMAGE)
        assert not call(["ps", "-aq", "--filter", "label=" + LABEL + "=" + nonce]).stdout.strip()
        call(compose, document=model(["/bin/sleep", "120"]))
        old = inspect(nonce + "-service")
        assert old and old["running"]
        evidence["old"] = old
        call(["stop", "--time", "2", old["id"]])
        call(["rename", old["id"], nonce + "-rollback"])
        evidence["renamed"] = inspect(nonce + "-rollback")
        assert evidence["renamed"]["labels"] == old["labels"]
        candidate = call(compose, document=model(["/t0046-intentionally-missing"]), check=False)
        assert candidate.returncode != 0, "start failure injection did not fire"
        evidence["old_after_failed_up"] = inspect(old["id"])
        evidence["candidate"] = inspect(nonce + "-service")
        evidence["rollback_point_destroyed"] = evidence["old_after_failed_up"] is None
        assert evidence["rollback_point_destroyed"], "hypothesis not reproduced"
    finally:
        # Only the exact nonce label's containers can be considered for cleanup.
        try:
            ids = call(["ps", "-aq", "--no-trunc", "--filter", "label=" + LABEL + "=" + nonce]).stdout.split()
            for container_id in ids:
                owned = inspect(container_id, require_limits=False)
                assert owned and owned["id"] == container_id
                call(["rm", "-f", container_id])
            evidence["remaining_fixture_ids"] = call([
                "ps", "-aq", "--filter", "label=" + LABEL + "=" + nonce]).stdout.split()
        finally:
            evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
            args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: evidence[key] for key in
                     ("project", "rollback_point_destroyed", "remaining_fixture_ids", "finished_at")}))


if __name__ == "__main__":
    main()
