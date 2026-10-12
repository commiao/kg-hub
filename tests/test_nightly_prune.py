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


def stubborn(ready: Path) -> list[str]:
    """忽略 SIGTERM 的子进程；装好处理器后才写 ready 文件。

    2026-10-13 镜像里实测：在慢盘的 NAS 上子进程还没执行到 signal.signal，看护的
    SIGTERM 就到了，于是被 SIGTERM 杀掉（rc=-15），测不到 SIGKILL 那条路径。
    """
    return [sys.executable, "-c",
            "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open(sys.argv[1], 'w').close(); time.sleep(60)", str(ready)]


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
        import threading
        ready = self.control / "ready"

        def cancel_once_ready():                 # 子进程忽略 SIGTERM 之后才要求停
            deadline = time.monotonic() + 30
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            (self.control / "CANCEL").touch()

        threading.Thread(target=cancel_once_ready, daemon=True).start()
        report = self.run_guarded(stubborn(ready), grace=0.5)
        self.assertTrue(ready.exists(), "child never got to ignore SIGTERM")
        self.assertEqual(report["result"], "cancelled")
        self.assertEqual(report["rc"], -9)                       # SIGKILL
        self.assertLess(report["elapsed"], 10)

    def test_status_is_written_atomically(self):
        N.write_status(self.control, state="waiting", next_run="x", last=None)
        data = json.loads((self.control / "status.json").read_text())
        self.assertEqual(data["state"], "waiting")
        self.assertFalse((self.control / "status.json.tmp").exists())


class CatchUpWindowTests(unittest.TestCase):
    """2026-10-12：服务 23:59 上线，原逻辑把首次运行排到第二天 23:05。"""

    def test_current_night_across_midnight(self):
        # 北京 10-12 23:30 → 10-12 那一晚；10-13 03:00 仍属 10-12 那一晚
        self.assertEqual(N.current_night(utc(2026, 10, 12, 15, 30)), "2026-10-12")
        self.assertEqual(N.current_night(utc(2026, 10, 12, 19, 0)), "2026-10-12")
        self.assertEqual(N.current_night(utc(2026, 10, 12, 15, 5)), "2026-10-12")   # 23:05 整
        self.assertIsNone(N.current_night(utc(2026, 10, 12, 22, 0)))                # 06:00 整
        self.assertIsNone(N.current_night(utc(2026, 10, 12, 4, 0)))                 # 12:00
        self.assertIsNone(N.current_night(utc(2026, 10, 12, 15, 4)))                # 23:04

    def test_a_same_day_window_works_too(self):
        self.assertEqual(N.current_night(utc(2026, 10, 12, 18, 0), "01:00", "06:00"), "2026-10-13")
        self.assertIsNone(N.current_night(utc(2026, 10, 12, 16, 30), "01:00", "06:00"))

    def test_due_night_runs_each_night_once(self):
        now = utc(2026, 10, 12, 16, 30)                                             # 00:30
        self.assertEqual(N.due_night(now, None), "2026-10-12")
        self.assertEqual(N.due_night(now, "2026-10-11"), "2026-10-12")
        self.assertIsNone(N.due_night(now, "2026-10-12"))
        self.assertIsNone(N.due_night(utc(2026, 10, 12, 4, 0), None))               # 白天

    def test_last_night_is_read_back_from_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp)
            self.assertIsNone(N.read_last_night(control))
            N.write_status(control, state="waiting", last_night="2026-10-12")
            self.assertEqual(N.read_last_night(control), "2026-10-12")
            (control / "status.json").write_text("not json")
            self.assertIsNone(N.read_last_night(control))


class MainLoopTests(unittest.TestCase):
    """用假时钟把主循环跑起来：窗口内启动补跑一次，跑过就等下一晚。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.control = Path(tmp.name)
        self.runs = []

    def run_main(self, now):
        from unittest import mock
        stop = N._Stop()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        def fake_run(cmd, **kw):
            self.runs.append(cmd)
            return {"result": "done", "rc": 0, "seconds": 0.1}

        def fake_sleep(seconds):
            stop.requested = True                     # 进入等待即结束测试

        with mock.patch.dict("os.environ", {"KG_HUB_PRUNE_DIR": str(self.control)}), \
             mock.patch.object(N, "datetime", Clock), \
             mock.patch.object(N, "_Stop", return_value=stop), \
             mock.patch.object(N, "run_once", side_effect=fake_run), \
             mock.patch.object(N.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(N.signal, "signal"):
            N.main()
        return json.loads((self.control / "status.json").read_text())

    def test_starting_inside_the_window_catches_up_once(self):
        status = self.run_main(utc(2026, 10, 12, 16, 30))                          # 00:30
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(status["last_night"], "2026-10-12")
        self.assertEqual(status["last"]["result"], "done")

    def test_a_night_already_run_is_not_run_again_after_a_restart(self):
        N.write_status(self.control, state="waiting", last_night="2026-10-12")
        status = self.run_main(utc(2026, 10, 12, 18, 0))                           # 02:00
        self.assertEqual(self.runs, [])
        self.assertEqual(status["last_night"], "2026-10-12")

    def test_starting_in_the_daytime_waits_for_the_window(self):
        self.run_main(utc(2026, 10, 12, 4, 0))                                     # 12:00
        self.assertEqual(self.runs, [])

    def test_a_cancel_file_skips_the_night_and_counts_it(self):
        (self.control / "CANCEL").touch()
        status = self.run_main(utc(2026, 10, 12, 16, 30))
        self.assertEqual(self.runs, [])
        self.assertEqual(status["last_night"], "2026-10-12")
        self.assertEqual(status["last"]["result"], "skipped")


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
        self.assertEqual(env["KG_HUB_PRUNE_UNTIL"], "06:00")
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
