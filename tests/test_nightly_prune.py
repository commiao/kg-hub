"""T-0213：每晚 23:05（北京时间）清理账本的调度与看护。

第一次清理（2026-10-11/12）靠宿主上的一次性脚本，看护用了镜像里没有的 pkill 又吞掉
报错，「停止」之后清理照常跑完。这里锁住：时间算对、IO 高/CANCEL/SIGTERM 都真的停下
子进程（忽略 SIGTERM 的也会被 SIGKILL）、compose 与发布脚本把它当正式服务管理。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import nightly_prune as N  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
STUBBORN = [sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class ScheduleTests(unittest.TestCase):
    def test_next_run_is_tonight_beijing_before_2305(self):
        # 2026-10-12 12:00 UTC = 20:00 北京 → 当晚 23:05 北京 = 15:05 UTC
        self.assertEqual(N.next_run(utc(2026, 10, 12, 12, 0)), utc(2026, 10, 12, 15, 5))

    def test_next_run_is_tomorrow_once_2305_has_passed(self):
        self.assertEqual(N.next_run(utc(2026, 10, 12, 15, 5)), utc(2026, 10, 13, 15, 5))
        self.assertEqual(N.next_run(utc(2026, 10, 12, 15, 40)), utc(2026, 10, 13, 15, 5))

    def test_next_run_across_utc_midnight(self):
        # 2026-10-12 17:00 UTC = 10-13 01:00 北京 → 10-13 23:05 北京
        self.assertEqual(N.next_run(utc(2026, 10, 12, 17, 0)), utc(2026, 10, 13, 15, 5))

    def test_io_load_reads_the_synology_io_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "syno_loadavg"
            path.write_text("1.67 2.11 1.30 0.70 0.54 0.38\n")
            self.assertEqual(N.io_load(path), 1.67)
            self.assertIsNone(N.io_load(Path(tmp) / "missing"))

    def test_command_applies_with_seven_day_retention(self):
        cmd = N.command("7", "3600")
        self.assertEqual(cmd[1:], ["-m", "utils.journal_prune", "--apply",
                                   "--retention-days", "7", "--max-seconds", "3600"])


class GuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.control = Path(tmp.name)
        self.stop = N._Stop()

    def run_guarded(self, cmd, loads=None, **kw):
        loads = iter(loads or [])
        started = time.monotonic()
        report = N.run_once(cmd, control=self.control, io_limit=4.0, stop=self.stop,
                            interval=0.05, poll=0.02, grace=kw.pop("grace", 1.0),
                            read_io=lambda: next(loads, 0.0), **kw)
        report["elapsed"] = time.monotonic() - started
        return report

    def test_a_finished_prune_is_done(self):
        report = self.run_guarded([sys.executable, "-c", "pass"])
        self.assertEqual((report["result"], report["rc"]), ("done", 0))

    def test_a_failed_prune_is_reported(self):
        report = self.run_guarded([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual((report["result"], report["rc"]), ("failed", 3))

    def test_four_high_io_samples_in_a_row_stop_it(self):
        report = self.run_guarded(SLEEPER, loads=[5, 5, 5, 5])
        self.assertEqual(report["result"], "io_high")
        self.assertLess(report["elapsed"], 10)

    def test_a_low_sample_resets_the_streak(self):
        report = self.run_guarded(SLEEPER, loads=[5, 5, 5, 1, 5, 5, 5, 5])
        self.assertEqual(report["result"], "io_high")
        # 至少 8 次采样：第 4 次低负载把计数清零
        self.assertGreaterEqual(report["elapsed"], 8 * 0.05)

    def test_an_unreadable_io_load_never_stops_it(self):
        report = self.run_guarded([sys.executable, "-c", "import time; time.sleep(0.5)"],
                                  loads=[None] * 50)
        self.assertEqual(report["result"], "done")

    def test_a_cancel_file_stops_it(self):
        (self.control / "CANCEL").touch()
        report = self.run_guarded(SLEEPER)
        self.assertEqual(report["result"], "cancelled")
        self.assertLess(report["elapsed"], 10)

    def test_sigterm_to_the_scheduler_stops_the_child(self):
        self.stop.requested = True
        report = self.run_guarded(SLEEPER)
        self.assertEqual(report["result"], "terminated")

    def test_a_child_ignoring_sigterm_is_killed_after_the_grace(self):
        (self.control / "CANCEL").touch()
        report = self.run_guarded(STUBBORN, grace=0.5)
        self.assertEqual(report["result"], "cancelled")
        self.assertEqual(report["rc"], -9)                       # SIGKILL
        self.assertLess(report["elapsed"], 10)

    def test_status_is_written_atomically(self):
        N.write_status(self.control, state="waiting", next_run="x", last=None)
        data = json.loads((self.control / "status.json").read_text())
        self.assertEqual(data["state"], "waiting")
        self.assertFalse((self.control / "status.json.tmp").exists())


class DeploymentTests(unittest.TestCase):
    def service(self):
        import yaml
        return yaml.safe_load((ROOT / "docker-compose.yml").read_text("utf-8"))["services"]["journal_prune"]

    def test_compose_runs_the_scheduler_with_the_approved_settings(self):
        svc = self.service()
        self.assertEqual(svc["command"], ["python", "-m", "tools.nightly_prune"])
        self.assertTrue(svc["image"].startswith("kg-hub-server:${KG_HUB_IMAGE_TAG:?"))
        env = dict(item.split("=", 1) for item in svc["environment"])
        self.assertEqual(env["KG_HUB_PRUNE_AT"], "23:05")
        self.assertEqual(env["KG_HUB_PRUNE_RETENTION_DAYS"], "7")
        self.assertEqual(env["KG_HUB_INGEST_BACKUP_PATH"], "/backup/ingest-backup.jsonl")
        self.assertEqual(env["KG_HUB_FALKORDB_HOST"], "falkordb")
        self.assertEqual(env["KG_HUB_FALKORDB_PASSWORD"], "${FALKORDB_PASSWORD}")
        self.assertIn("falkordb", svc["depends_on"])
        self.assertEqual(svc["restart"], "unless-stopped")

    def test_compose_is_locked_down_and_mounts_only_what_it_needs(self):
        svc = self.service()
        self.assertTrue(svc["read_only"])
        # 账本目录在容器里是 1027 的 0700；去掉 capability 的 root 写不了（首次发布实测）
        self.assertEqual(svc["user"], "${KG_HUB_JOURNAL_PRUNE_USER:-1027:100}")
        self.assertEqual(svc["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", svc["security_opt"])
        root = "${KG_HUB_DATA_ROOT:?set absolute kg-hub data root}"
        self.assertEqual(sorted(svc["volumes"]),
                         sorted([f"{root}/ingest-backup:/backup",
                                 f"{root}/journal-prune:/journal-prune"]))

    def test_release_and_redeploy_manage_it(self):
        release = (ROOT / "deploy/nas/release.sh").read_text("utf-8")
        services = release.split('SERVICES="${KG_HUB_SERVICES:-', 1)[1].split("}", 1)[0].split()
        self.assertIn("journal_prune", services)
        redeploy = (ROOT / "deploy/nas/redeploy.sh").read_text("utf-8")
        self.assertIn("journal_prune) container='kg-hub-journal-prune'", redeploy)
        loop = redeploy.split("  for service in ", 1)[1].split(";", 1)[0].split()
        self.assertIn("journal_prune", loop)
        self.assertEqual(loop[-1], "watchdog")                     # watchdog 仍最后启动


if __name__ == "__main__":
    unittest.main()
