"""NAS `device_liveness` producer 在线快照的可信度边界。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.device_liveness import device_state, load_status, parse_status
from topology import annotate_liveness, capture_stale_after_s


RAW = {
    "BackendState": "Running",
    "Peer": {
        "node-key:a": {
            "HostName": "MacBook-Pro-4",
            "DNSName": "macbook-pro-4.example.ts.net.",
            "Online": True,
            "LastSeen": "2026-08-26T00:00:00Z",
        },
        "node-key:b": {
            "HostName": "sleeping-mac",
            "DNSName": "sleeping-mac.example.ts.net.",
            "Online": False,
        },
    }
}


class DeviceLivenessTests(unittest.TestCase):
    def test_fresh_status_maps_host_and_dns_aliases(self) -> None:
        live = parse_status(RAW, age_s=10, max_age_s=180)
        self.assertEqual(device_state(live, "MacBook-Pro-4")[0], "online")
        self.assertEqual(device_state(live, "macbook-pro-4.example.ts.net")[0], "online")
        self.assertEqual(device_state(live, "sleeping-mac.local")[0], "offline")

    def test_duplicate_mutable_alias_is_unknown_but_stable_identity_resolves(self) -> None:
        duplicate = json.loads(json.dumps(RAW))
        duplicate["Peer"]["node-key:b"]["HostName"] = "MacBook-Pro-4"
        live = parse_status(duplicate, age_s=10)
        state, detail = device_state(live, "MacBook-Pro-4")
        self.assertEqual(state, "unknown")
        self.assertIn("同名冲突", detail)
        self.assertEqual(
            device_state(
                live, "MacBook-Pro-4",
                {"MacBook-Pro-4": ["node-key:a"]})[0],
            "online")

    def test_stale_status_downgrades_old_online_to_unknown(self) -> None:
        live = parse_status(RAW, age_s=181, max_age_s=180)
        self.assertEqual(device_state(live, "MacBook-Pro-4")[0], "unknown")

    def test_non_running_backend_cannot_refresh_old_peer_online(self) -> None:
        stopped = json.loads(json.dumps(RAW))
        stopped["BackendState"] = "Stopped"
        live = parse_status(stopped, age_s=1, max_age_s=180)
        self.assertNotEqual(live["source_state"], "fresh")
        self.assertEqual(device_state(live, "MacBook-Pro-4")[0], "unknown")

    def test_consumer_rejects_wrong_minimum_schema_too(self) -> None:
        live = parse_status({"BackendState": "Running", "Peer": []}, age_s=1)
        self.assertEqual(live["source_state"], "invalid")
        self.assertEqual(device_state(live, "MacBook-Pro-4")[0], "unknown")

    def test_loader_uses_file_mtime_as_observation_time(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tailscale-status.json"
            path.write_text(json.dumps(RAW), encoding="utf-8")
            now = time.time()
            os.utime(path, (now - 30, now - 30))
            self.assertEqual(load_status(path, now=now, max_age_s=60)["source_state"], "fresh")
            self.assertEqual(load_status(path, now=now, max_age_s=20)["source_state"], "stale")

    def test_missing_corrupt_and_future_files_are_unknown_sources(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tailscale-status.json"
            self.assertEqual(load_status(path)["source_state"], "missing")
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_status(path)["source_state"], "invalid")
            path.write_text(json.dumps(RAW), encoding="utf-8")
            now = time.time()
            os.utime(path, (now + 60, now + 60))
            self.assertEqual(load_status(path, now=now)["source_state"], "invalid")

    def test_dashboard_annotation_distinguishes_snapshot_age_from_alertable_stale(self) -> None:
        old = {"_host": "MacBook-Pro-4", "_age_s": 3600}
        online = annotate_liveness(dict(old), parse_status(RAW, age_s=10))
        self.assertTrue(online["_snapshot_stale"])
        self.assertTrue(online["_stale"])
        self.assertFalse(online["_disconnected"])

        raw_offline = json.loads(json.dumps(RAW))
        raw_offline["Peer"]["node-key:a"]["Online"] = False
        offline = annotate_liveness(dict(old), parse_status(raw_offline, age_s=10))
        self.assertTrue(offline["_snapshot_stale"])
        self.assertFalse(offline["_stale"])
        self.assertTrue(offline["_disconnected"])

        unknown = annotate_liveness(dict(old), parse_status(RAW, age_s=600))
        self.assertEqual(unknown["_device_state"], "unknown")
        self.assertFalse(unknown["_stale"])

        configured_threshold = annotate_liveness(
            {"_host": "MacBook-Pro-4", "_age_s": 120},
            parse_status(RAW, age_s=10), stale_after_s=60)
        self.assertTrue(configured_threshold["_snapshot_stale"])
        self.assertEqual(capture_stale_after_s({"capture_stale_after_min": 1}), 60)

    def test_dashboard_invalid_threshold_falls_back_to_30_minutes(self) -> None:
        for value in (-1, 0, "invalid", None):
            with self.subTest(value=value):
                self.assertEqual(
                    capture_stale_after_s({"capture_stale_after_min": value}),
                    1800)

    def test_dashboard_uses_capture_to_tailscale_identity_mapping(self) -> None:
        mismatched = json.loads(json.dumps(RAW))
        peer = mismatched["Peer"]["node-key:a"]
        peer["HostName"] = "MacBook Pro (3)"
        peer["DNSName"] = "mac-office.example.ts.net."
        aliases = {"MacBook-Pro-4": ["MacBook Pro (3)", "mac-office"]}
        snap = annotate_liveness(
            {"_host": "MacBook-Pro-4", "_age_s": 3600},
            parse_status(mismatched, age_s=10), aliases)
        self.assertEqual(snap["_device_state"], "online")
        self.assertTrue(snap["_stale"])

    def test_server_mounts_public_liveness_directory_not_notify_secrets(self) -> None:
        compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
        server = compose.split("  kg_hub_server:", 1)[1].split("  watchdog:", 1)[0]
        self.assertIn(
            "/device-liveness/runtime:/device-liveness:ro",
            server)
        self.assertIn(
            "/device-liveness.json:/device-liveness-config/device-liveness.json:ro",
            server)
        self.assertNotIn("/notify-config:/config", server)
        watchdog = compose.split("  watchdog:", 1)[1].split("  ingester:", 1)[0]
        self.assertIn("/device-liveness/runtime:/device-liveness:ro", watchdog)
        self.assertIn(
            "/device-liveness.json:/device-liveness-config/device-liveness.json:ro",
            watchdog)
        self.assertIn("/notify-config:/config:ro", watchdog)

    def test_liveness_producer_is_least_privilege_and_persistent(self) -> None:
        compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
        producer = compose.split("  device_liveness:", 1)[1].split("  kg_hub_server:", 1)[0]
        self.assertIn("restart: unless-stopped", producer)
        self.assertIn("read_only: true", producer)
        self.assertIn("network_mode: none", producer)
        self.assertIn("no-new-privileges:true", producer)
        self.assertIn(
            "/volume2/@appdata/Tailscale/tailscaled.sock:/tailscale-var/tailscaled.sock:ro",
            producer)
        self.assertNotIn("/volume2/@appdata/Tailscale:/tailscale-var:ro", producer)
        self.assertIn(
            "/device-liveness/runtime:/device-liveness", producer)
        self.assertNotIn("/device-liveness:/device-liveness\n", producer)
        self.assertIn('failures=$$((failures+1))', producer)
        self.assertIn(r'[ \"$$failures\" -lt 3 ] || exit 1', producer)

    def test_deploy_wrapper_syncs_runtime_files_and_recreates_services(self) -> None:
        root = Path(__file__).resolve().parent.parent
        deploy = root / "deploy" / "nas" / "deploy-device-liveness.sh"
        producer = root / "deploy" / "monitoring" / "nas" / "tailscale-liveness-snapshot.sh"
        text = deploy.read_text(encoding="utf-8")
        for runtime_file in ("topology.py", "tools/watchdog.py",
                             "utils/device_liveness.py", "docker-compose.yml"):
            self.assertIn(runtime_file, text)
        self.assertIn("deploy/nas/redeploy.sh", text)
        self.assertIn("device_liveness", text)
        self.assertIn(
            'MAP_OUT="$DATA_DIR/device-liveness.json"',
            text)
        self.assertIn('SNAPSHOT_OUT="$DATA_DIR/runtime/tailscale-status.json"', text)
        self.assertIn(r'deadline=\$((\$(date +%s) + 75))', text)
        self.assertIn(r'test \"\$after\" -gt \"\$before\"', text)
        self.assertNotIn("KG_HUB_DEVICE_LIVENESS_CONFIG_HOST", text)
        self.assertNotIn("sudo", text)
        self.assertNotIn("sh '$SRC/$REL'", text)
        self.assertTrue(deploy.stat().st_mode & stat.S_IXUSR)
        self.assertTrue(producer.stat().st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
