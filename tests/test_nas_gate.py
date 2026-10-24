"""NAS 重操作闸门（tools/nas_gate.py）：全机锁、最小间隔、测温门槛，超温与读不到温度都不放行。

时钟和 sleep 注入（不真的等）；锁用真的 fcntl 锁和临时目录，不碰本机 ~/.kg-hub 下那把真锁；
命令行用例用假的 ssh（PATH 里一个打印温度的脚本），不连 NAS。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import nas_gate as ng

REPO = Path(__file__).resolve().parent.parent


class FakeTime:
    def __init__(self, start=1_000_000.0):
        self.now = start
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = {"ssh_target": "nas", "sensor_path": "/x", "sensor_divisor": 1000, "max_c": 90, "samples": 3,
                  "sample_interval_s": 2, "gap_s": {"dryrun": 60, "submit": 180}, "recheck_s": 120,
                  "max_misses": 5, "max_wait_s": 3600, "lock_dir": self.tmp.name}
        self.t = FakeTime()
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def hold(self, kind, temps):
        it = iter(temps)
        return ng.hold(kind, settings=self.s, read=lambda s: next(it), sleep=self.t.sleep, clock=self.t.clock,
                       log=self.logs.append)

    def state(self):
        p = Path(self.tmp.name) / "nas-heavy.state.json"
        return json.loads(p.read_text()) if p.exists() else None

    def test_passes_on_cool_average_and_records_end(self):
        with self.hold("submit", [95, 80, 80]) as info:        # 看的是 3 次均值（85），不是单次
            self.assertEqual(info["temp_c"], 85.0)
            self.t.now += 30
        self.assertEqual(self.state(), {"last_end": self.t.now, "last_kind": "submit"})

    def test_waits_minimum_gap_after_last_heavy_op(self):
        (Path(self.tmp.name) / "nas-heavy.state.json").write_text(json.dumps({"last_end": self.t.now - 50}))
        with self.hold("submit", [70] * 3):
            pass
        self.assertEqual(self.t.sleeps[0], 130)                  # 180 − 50
        self.t.sleeps.clear()
        with self.hold("dryrun", [70] * 3):                       # 刚结束：dry-run 也要等满 60 秒
            pass
        self.assertEqual(self.t.sleeps[0], 60)

    def test_over_temperature_never_passes_and_times_out(self):
        entered = []
        with self.assertRaises(ng.GateRefused) as cm:
            with self.hold("submit", [95] * 1000):
                entered.append(1)
        self.assertEqual(cm.exception.code, ng.EXIT_TIMEOUT)
        self.assertEqual(entered, [])
        self.assertIsNone(self.state())                          # 没跑就不记结束时间
        self.assertIn(120, self.t.sleeps)                         # 超温时每 120 秒复测
        self.assertTrue(any("> 90°C" in m for m in self.logs))

    def test_cools_down_then_passes(self):
        with self.hold("dryrun", [93, 92, 91] + [85] * 3):
            pass
        self.assertEqual(self.t.sleeps.count(120), 1)

    def test_unreadable_temperature_refuses_with_75(self):
        with self.assertRaises(ng.GateRefused) as cm:
            with self.hold("dryrun", [None] * 100):
                self.fail("读不到温度不该放行")
        self.assertEqual(cm.exception.code, ng.EXIT_NO_TEMP)
        self.assertIsNone(self.state())
        # 一轮里有一次读不到，整轮算读不到（不拿残缺样本放行）
        with self.assertRaises(ng.GateRefused):
            with self.hold("dryrun", [70, None, 70] * 100):
                self.fail("残缺样本不该放行")

    def test_lock_is_machine_wide_and_queues(self):
        lock = open(Path(self.tmp.name) / "nas-heavy.lock", "a")
        fcntl.flock(lock, fcntl.LOCK_EX)                          # 另一个执行者正在重操作
        self.s["max_wait_s"] = 30
        with self.assertRaises(ng.GateRefused) as cm:
            with self.hold("dryrun", [70] * 3):
                self.fail("锁被占着不该放行")
        self.assertEqual(cm.exception.code, ng.EXIT_TIMEOUT)
        self.assertTrue(any("排队等锁" in m for m in self.logs))
        # 对方做完放锁：排队的这个接着进
        real_sleep, released = self.t.sleep, []

        def sleep(s):
            if not released:
                fcntl.flock(lock, fcntl.LOCK_UN)
                released.append(1)
            real_sleep(s)
        self.s["max_wait_s"] = 3600
        with ng.hold("dryrun", settings=self.s, read=lambda s: 70, sleep=sleep, clock=self.t.clock, log=self.logs.append):
            pass
        lock.close()

    def test_body_failure_still_records_end_and_releases_lock(self):
        with self.assertRaises(RuntimeError):
            with self.hold("submit", [70] * 3):
                raise RuntimeError("提交中途失败")
        self.assertEqual(self.state()["last_kind"], "submit")
        with self.hold("dryrun", [70] * 3):                       # 锁已放开
            pass

    def test_settings_from_config_and_env(self):
        s = ng.load_settings(env={})
        self.assertEqual((s["max_c"], s["samples"], s["gap_s"]), (90, 3, {"dryrun": 60, "submit": 180}))
        s = ng.load_settings(env={"KG_HUB_NAS_TEMP_SSH": "me@nas2", "KG_HUB_NAS_TEMP_SENSOR": "/sys/t2",
                                  "KG_HUB_NAS_GATE_DIR": "/tmp/g"})
        self.assertEqual((s["ssh_target"], s["sensor_path"], s["lock_dir"]), ("me@nas2", "/sys/t2", "/tmp/g"))
        cfg = Path(self.tmp.name) / "g.json"
        cfg.write_text(json.dumps({"ssh_target": "", "sensor_path": "/s", "lock_dir": "/l"}))
        with self.assertRaises(SystemExit):                       # 没配 SSH 目标：直接报错，不猜
            ng.load_settings(env={"KG_HUB_NAS_GATE_CONFIG": str(cfg)})

    def test_read_temp_uses_configured_target_and_sensor(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="71500\n")
        self.assertEqual(ng.read_temp(self.s, runner), 71.5)
        self.assertEqual(calls[0][-3:], ["nas", "cat", "/x"])
        self.assertIsNone(ng.read_temp(self.s, lambda cmd, **kw: subprocess.CompletedProcess(cmd, 255, stdout="")))
        self.assertIsNone(ng.read_temp(self.s, lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="n/a")))


class CliTest(unittest.TestCase):
    """命令行：两个并发调用不重叠、退出码透传；假 ssh 打印温度。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "bin").mkdir()
        self.temp_file = t / "temp"
        self.temp_file.write_text("70000")
        ssh = t / "bin" / "ssh"
        ssh.write_text(f"#!/bin/sh\ncat {self.temp_file}\n")
        ssh.chmod(0o755)
        cfg = json.loads((REPO / "config" / "nas_gate.json").read_text())
        cfg.update(sample_interval_s=0, recheck_s=0.2, max_wait_s=5, lock_dir=str(t / "lock"))
        (t / "gate.json").write_text(json.dumps(cfg))
        self.env = {**os.environ, "PATH": f"{t / 'bin'}:{os.environ['PATH']}", "KG_HUB_NAS_GATE_CONFIG": str(t / "gate.json"),
                    "PYTHONPATH": str(REPO)}
        self.env.pop("KG_HUB_NAS_TEMP_SSH", None)
        self.env.pop("KG_HUB_NAS_GATE_DIR", None)

    def tearDown(self):
        self.tmp.cleanup()

    def gate(self, *args, wait=True):
        cmd = [sys.executable, "-m", "tools.nas_gate", *args]
        if wait:
            return subprocess.run(cmd, env=self.env, cwd=REPO, capture_output=True, text=True, timeout=60)
        return subprocess.Popen(cmd, env=self.env, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_concurrent_runs_never_overlap_and_exit_code_passes_through(self):
        log = Path(self.tmp.name) / "spans"
        job = f"import time; s=time.time(); time.sleep(0.8); open({str(log)!r},'a').write(f'{{s}} {{time.time()}}\\n')"
        procs = [self.gate("--kind", "dryrun", "--gap", "0", "--", sys.executable, "-c", job, wait=False) for _ in range(2)]
        for p in procs:
            p.wait(timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr.read())
        spans = sorted(tuple(map(float, ln.split())) for ln in log.read_text().splitlines())
        self.assertEqual(len(spans), 2)
        self.assertLessEqual(spans[0][1], spans[1][0])           # 第二个等第一个结束才开始
        r = self.gate("--kind", "submit", "--gap", "0", "--", sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual(r.returncode, 7)

    def test_hot_nas_blocks_the_command(self):
        self.temp_file.write_text("95000")
        marker = Path(self.tmp.name) / "ran"
        r = self.gate("--kind", "submit", "--gap", "0", "--", sys.executable, "-c", f"open({str(marker)!r},'w')")
        self.assertEqual(r.returncode, ng.EXIT_TIMEOUT)
        self.assertFalse(marker.exists())
        self.assertIn("95.0°C > 90°C", r.stderr)
        probe = self.gate("--probe")
        self.assertEqual(json.loads(probe.stdout)["temp_c"], 95.0)


if __name__ == "__main__":
    unittest.main()
