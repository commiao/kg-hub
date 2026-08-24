"""拓扑探针对 refinery 的存活判定必须只看独立心跳。"""
from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import tools.capture_probe as probe


def refinery_node(status: dict | str | None, *, status_mtime_age_s: int | None = 0,
                  started_age_s: int = 0) -> dict:
    now = datetime.now(tz=timezone.utc)
    status_text = json.dumps(status) if isinstance(status, dict) else (status or "")
    status_mtime = (str(int(time.time() - status_mtime_age_s))
                    if status_mtime_age_s is not None else "")
    raw = "\n".join((
        "size=1048576",
        "integ=ok",
        "max=7",
        f"mtime={int(time.time())}",
        f"status_mtime={status_mtime}",
        f"refinery_started={(now - timedelta(seconds=started_age_s)).isoformat()}",
        "status_begin",
        status_text,
        "status_end",
        "kg-hub-refinery|Up 1 hour",
        "kg-hub-ingester|Up 1 hour",
    ))
    with patch.object(probe, "sh", return_value=(raw, None)):
        return probe.probe_nas_chain("nas", 7)[1]


class ProbeRefineryHeartbeatTests(unittest.TestCase):
    def test_fresh_heartbeat_beats_stale_job_progress(self):
        now = datetime.now(tz=timezone.utc)
        node = refinery_node({
            "ts": (now - timedelta(hours=1)).isoformat(),
            "heartbeat_at": now.isoformat(),
            "idle_outside_window": True,
        })
        self.assertEqual(node["state"], probe.AMBER)
        self.assertIn("心跳", node["detail"])

    def test_missing_heartbeat_is_amber_during_migration_grace(self):
        node = refinery_node({
            "ts": (datetime.now(tz=timezone.utc) - timedelta(minutes=14)).isoformat(),
            "idle_outside_window": True,
        })
        self.assertEqual(node["state"], probe.AMBER)
        self.assertIn("heartbeat_at", node["detail"])

    def test_missing_or_invalid_heartbeat_turns_red_after_grace(self):
        old = (datetime.now(tz=timezone.utc) - timedelta(minutes=16)).isoformat()
        missing = refinery_node({"ts": old, "idle_outside_window": True}, started_age_s=16 * 60)
        invalid = refinery_node(
            {"ts": old, "heartbeat_at": "bad", "idle_outside_window": True},
            started_age_s=16 * 60)
        self.assertEqual(missing["state"], probe.RED)
        self.assertEqual(invalid["state"], probe.RED)

    def test_future_status_mtime_is_not_a_liveness_fallback(self):
        node = refinery_node({"heartbeat_at": "bad"}, status_mtime_age_s=-60,
                             started_age_s=16 * 60)
        self.assertEqual(node["state"], probe.RED)

    def test_legacy_status_gets_container_start_migration_grace(self):
        fresh = (datetime.now(tz=timezone.utc) - timedelta(seconds=30)).isoformat()
        new_container = refinery_node({"ts": fresh}, started_age_s=14 * 60)
        old_container = refinery_node({"ts": fresh}, started_age_s=16 * 60)
        self.assertEqual(new_container["state"], probe.AMBER)
        self.assertEqual(old_container["state"], probe.RED)

    def test_missing_status_uses_container_start_for_migration_grace(self):
        new_container = refinery_node(None, status_mtime_age_s=None, started_age_s=14 * 60)
        old_container = refinery_node(None, status_mtime_age_s=None, started_age_s=16 * 60)
        self.assertEqual(new_container["state"], probe.AMBER)
        self.assertEqual(old_container["state"], probe.RED)

    def test_stale_heartbeat_is_red_even_when_old_progress_looks_fresh(self):
        now = datetime.now(tz=timezone.utc)
        node = refinery_node({
            "ts": now.isoformat(),
            "heartbeat_at": (now - timedelta(seconds=probe.REFINERY_STALE_S + 1)).isoformat(),
            "idle_outside_window": False,
        })
        self.assertEqual(node["state"], probe.RED)
        self.assertIn("心跳", node["detail"])


if __name__ == "__main__":
    unittest.main()
