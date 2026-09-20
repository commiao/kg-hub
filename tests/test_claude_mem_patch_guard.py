"""claude-mem worker 补丁守护。

## 要治的病

T-0077 B 项的补丁装在第三方插件的构建产物里。`claude plugin update` 会覆盖它，
而覆盖之后**什么都不会报错** —— worker 照常跑、采集照常走，只是每次链路抖动
又开始重复付费。这类故障没有声音，所以必须有人定期去看。

## 最该防的不是「补丁掉了」

是**把旧版本盖回新版本**。插件真升级之后，把我们基于 13.25.1 的 bundle 按回去，
会静默降级整个 worker —— 比丢补丁糟得多。所以「版本对不上就一个字节都不写」
是这个文件里最重要的一条。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "tools" / "claude_mem_patch_guard.sh"

PATCHED = b"// patched worker with cm-op marker\n"
STOCK = b"// stock worker\n"
VERSION = "13.25.1"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "tools").mkdir(parents=True)
        # 补丁源放在仓库的同级目录，与真实布局一致。
        self.source = Path(self.tmp.name) / "claude-mem-fork/plugin/scripts/worker-service.cjs"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(PATCHED)
        (self.repo / "tools" / "claude_mem_patch_guard.sh").write_text(
            GUARD.read_text("utf-8"), "utf-8")
        (self.repo / "tools" / "claude_mem_patch_guard.sh").chmod(0o755)
        self.target = self.home / ".claude/plugins/marketplaces/thedotmack/plugin"
        (self.target / "scripts").mkdir(parents=True)
        self.bundle = self.target / "scripts" / "worker-service.cjs"
        self.log = Path(self.tmp.name) / "guard.log"
        self.worker_health = "http://127.0.0.1:1/health"   # 必然连不上
        self.write_manifest(sha(PATCHED), VERSION)
        self.write_target(STOCK, VERSION)

    def write_manifest(self, digest: str, version: str) -> None:
        (self.repo / "tools" / "claude_mem_patch.manifest").write_text(
            f"version={version}\nsha256={digest}\nmarker=cm-op\n"
            "source=../claude-mem-fork/plugin/scripts/worker-service.cjs\n"
            "target=.claude/plugins/marketplaces/thedotmack/plugin\n", "utf-8")

    def write_target(self, body: bytes, version: str) -> None:
        self.bundle.write_bytes(body)
        (self.target / "package.json").write_text(json.dumps({"version": version}), "utf-8")

    def run_guard(self) -> str:
        done = subprocess.run(
            ["/bin/sh", str(self.repo / "tools" / "claude_mem_patch_guard.sh")],
            capture_output=True, text=True,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin",
                 "CLAUDE_MEM_PATCH_LOG": str(self.log),
                 "CLAUDE_MEM_PATCH_STATE": str(Path(self.tmp.name) / "state"),
                 # 指到一个必然连不上的地址：用例绝不能打到真机上在跑的 worker。
                 # 2026-09-20 第一版没有这一行，「静默」那条被真机实况弄红了 ——
                 # 它报得没错，只是那不是用例该看的东西。
                 "CLAUDE_MEM_WORKER_HEALTH": self.worker_health},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.log.read_text("utf-8") if self.log.exists() else ""

    # ── 核心那一条 ──────────────────────────────────────────────────
    def test_a_version_bump_is_never_overwritten(self):
        """插件真升级了就一个字节都不写。**这是本文件最重要的一条。**

        盖回去会把整个 worker 静默降级 —— 比丢补丁糟得多。
        """
        self.write_target(STOCK, "13.26.0")
        log = self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), STOCK, "把旧版本盖回了新版本")
        self.assertIn("版本已变", log)

    def test_a_version_bump_is_reported_not_swallowed(self):
        """而且必须出声：不还原 = 重复付费的防护当前失效，沉默就是在撒谎。"""
        self.write_target(STOCK, "13.26.0")
        self.run_guard()
        marks = list((Path(self.tmp.name) / "state").glob("patch-version-moved-*"))
        self.assertEqual(len(marks), 1, "版本漂移没有告警")

    # ── 正常还原 ────────────────────────────────────────────────────
    def test_an_overwritten_patch_is_restored(self):
        log = self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), PATCHED)
        self.assertIn("已还原", log)

    def test_an_intact_patch_stays_silent(self):
        """绝大多数轮次走这条。静默,否则日志会被刷爆、真事就淹了。"""
        self.write_target(PATCHED, VERSION)
        self.assertEqual(self.run_guard(), "")

    # ── 拒绝在不确定时动手 ──────────────────────────────────────────
    def test_a_source_that_does_not_match_the_manifest_is_refused(self):
        """源与清单指纹不符 = 不知道手里这份是什么，不许拿它还原。

        重新构建之后忘了同步清单，正是这个形态。
        """
        self.source.write_bytes(b"// rebuilt, manifest not updated\n")
        log = self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), STOCK)
        self.assertIn("指纹不符", log)

    def test_a_missing_source_is_loud_not_silent(self):
        """有清单、没源 —— 还原能力根本不存在。

        不出声的话，下一次插件升级会把补丁抹掉，而不会有任何人知道。
        """
        self.source.unlink()
        log = self.run_guard()
        self.assertIn("补丁源不存在", log)

    def test_an_unreadable_version_is_never_written_over(self):
        """读不出版本就不写 —— 不知道对面是什么版本时动手，可能就是降级。"""
        (self.target / "package.json").write_text("{ not json", "utf-8")
        log = self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), STOCK)
        self.assertIn("读不出版本", log)


class RestartAfterRestoreTests(PatchGuardTests):
    """还原文件不等于生效 —— 必须把 worker 也重启。

    2026-09-19 的教训，代价是一天半：守护 01:42:50 把补丁按回文件里，而 worker
    01:41:03 就已经起来了。bun 启动时把 bundle 读进内存，**进程跑的一直是补丁
    写入之前那一份**。盘上对、跑的错，34 小时没人知道。

    当时我在告警文案里写了「worker 需重启才生效」，然后从没执行那一步。
    **写在文字里的后续动作等于没有后续动作** —— 所以它必须变成代码。
    """

    def setUp(self):
        super().setUp()
        # 假 bun：把「被要求 stop」记进文件，不真的动任何进程。
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.stopped = Path(self.tmp.name) / "stopped"
        (self.bin / "bun").write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> {self.stopped}\nexit 0\n', "utf-8")
        (self.bin / "bun").chmod(0o755)

    def run_guard(self) -> str:
        import subprocess
        done = subprocess.run(
            ["/bin/sh", str(self.repo / "tools" / "claude_mem_patch_guard.sh")],
            capture_output=True, text=True,
            env={"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
                 "CLAUDE_MEM_PATCH_LOG": str(self.log),
                 "CLAUDE_MEM_PATCH_STATE": str(Path(self.tmp.name) / "state"),
                 "CLAUDE_MEM_WORKER_HEALTH": self.worker_health},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.log.read_text("utf-8") if self.log.exists() else ""

    def test_restoring_the_patch_also_restarts_the_worker(self):
        log = self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), PATCHED)
        self.assertTrue(self.stopped.exists(), "还原了文件却没重启 worker")
        self.assertIn("stop", self.stopped.read_text("utf-8"))
        self.assertIn("已排空并重启 worker", log)

    def test_an_intact_patch_does_not_restart_anything(self):
        """没还原就别动在跑的进程。"""
        self.write_target(PATCHED, VERSION)
        self.run_guard()
        self.assertFalse(self.stopped.exists(), "什么都没改却重启了 worker")

    def test_the_restart_happens_only_after_every_target_is_restored(self):
        """重启必须排在**所有**目标都还原之后。

        2026-09-20 第一版把重启写在循环里，还原完第一个目标就重启 —— worker 起来
        时恰好可能加载到那些还没轮到的目标，于是「重启让它加载新补丁」反而把未打
        补丁的那份装进了内存。**和它要治的那个病一模一样，只是快了几百毫秒。**

        这里让假 bun 在被调用的那一刻记下每个目标的内容，事后检查。
        """
        second = self.home / ".claude/plugins/cache/thedotmack/claude-mem/13.25.1"
        (second / "scripts").mkdir(parents=True)
        (second / "scripts" / "worker-service.cjs").write_bytes(STOCK)
        (second / "package.json").write_text(json.dumps({"version": VERSION}), "utf-8")
        manifest = self.repo / "tools" / "claude_mem_patch.manifest"
        manifest.write_text(manifest.read_text("utf-8")
                            + ".claude/plugins/cache/thedotmack/claude-mem/13.25.1\n"
                            .join(["target=", ""]), "utf-8")
        snapshot = Path(self.tmp.name) / "snapshot"
        (self.bin / "bun").write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> {self.stopped}\n'
            f'cat "{self.bundle}" "{second}/scripts/worker-service.cjs" > {snapshot}\nexit 0\n',
            "utf-8")
        (self.bin / "bun").chmod(0o755)

        self.run_guard()
        self.assertTrue(snapshot.exists(), "没重启")
        self.assertEqual(snapshot.read_bytes(), PATCHED + PATCHED,
                         "重启时还有目标没还原 —— worker 可能加载到旧的那份")

    def test_one_restart_per_patch_version_not_every_five_minutes(self):
        """一个 sha 只重启一次。判据万一算错，不能变成每 5 分钟打断一次采集。"""
        self.run_guard()
        self.write_target(STOCK, VERSION)          # 再次被覆盖
        self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), PATCHED, "第二次没还原")
        self.assertEqual(len(self.stopped.read_text("utf-8").strip().splitlines()), 1)


class DrainBeforeRestartTests(RestartAfterRestoreTests):
    """重启之前必须排空 —— 准则 29。

    2026-09-20 在网关那边查实：没排空就重启，在飞的付费请求会变成永不过期的孤儿
    （钱付了、答案没拿到、只能人工核实）。那 18 条未决只来自三次事件，每次都是
    一整批在几十秒内同时死，其中一次对得上 02:05 的裸 `docker restart`。

    worker 这一侧同理：它正在等的那次模型调用是花了钱的。
    """

    def serve_sessions(self, active: int):
        """假一个 /health，报指定数量的在飞会话。"""
        import http.server, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = f'{{"status":"ok","activeSessions":{active},"pid":1}}'.encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def log_message(self, *a): pass
        server = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.worker_health = f"http://127.0.0.1:{server.server_address[1]}/health"

    def run_guard(self, drain_timeout="5") -> str:
        import subprocess
        done = subprocess.run(
            ["/bin/sh", str(self.repo / "tools" / "claude_mem_patch_guard.sh")],
            capture_output=True, text=True,
            env={"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
                 "CLAUDE_MEM_PATCH_LOG": str(self.log),
                 "CLAUDE_MEM_PATCH_STATE": str(Path(self.tmp.name) / "state"),
                 "CLAUDE_MEM_WORKER_HEALTH": self.worker_health,
                 "CLAUDE_MEM_DRAIN_TIMEOUT": drain_timeout},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.log.read_text("utf-8") if self.log.exists() else ""

    def test_a_busy_worker_is_not_restarted(self):
        """**本文件最承重的一条。** 有在飞请求就不停 —— 停了就是制造孤儿付费记录。"""
        self.serve_sessions(3)
        log = self.run_guard()
        self.assertEqual(self.bundle.read_bytes(), PATCHED, "文件还是要还原的")
        self.assertFalse(self.stopped.exists(), "有在飞请求却把 worker 停了")
        self.assertIn("仍有在飞请求", log)

    def test_an_idle_worker_is_restarted(self):
        self.serve_sessions(0)
        log = self.run_guard()
        self.assertTrue(self.stopped.exists(), "空闲却没重启")
        self.assertIn("已排空并重启", log)

    def test_a_skipped_restart_is_retried_next_round(self):
        """排不空就**不写标记** —— 否则这一版永远等不到重启。

        守护每 300 秒一轮，下一轮 worker 空了就该补上。
        """
        self.serve_sessions(3)
        self.run_guard()
        self.assertFalse(self.stopped.exists())
        self.serve_sessions(0)                      # 下一轮：空了
        self.write_target(STOCK, VERSION)           # 再次被覆盖
        self.run_guard()
        self.assertTrue(self.stopped.exists(), "下一轮空闲了仍然没补上重启")

    def test_an_unreachable_health_endpoint_does_not_block_forever(self):
        """读不出在飞数就放行 —— 卡死在这一步会让补丁永远装不上，那是确定的损失。"""
        self.worker_health = "http://127.0.0.1:1/health"
        self.run_guard()
        self.assertTrue(self.stopped.exists())


class RunningWorkerTests(PatchGuardTests):
    """文件对不代表在跑的那个对 —— 它可能压根不在这些目标里。

    2026-09-19 实测 hook 从一个未打补丁的旧版本目录拉起过 worker。这种情况
    只出声不自动重启：正解通常是「版本变了，补丁要重做」，而不是打断在跑的那个。
    """

    def serve(self, bundle_path: str):
        """起一个假 /health，返回一个 pid，并让 ps 能查到它的命令行。"""
        import http.server, threading, os, subprocess, sys
        # 用一个真实存在的子进程当"在位 worker"，命令行里带上 bundle 路径
        proc = subprocess.Popen([sys.executable, "-c",
                                 f"import time,sys;sys.argv.append({bundle_path!r});time.sleep(30)",
                                 bundle_path])
        self.addCleanup(proc.kill)
        pid = proc.pid

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = f'{{"status":"ok","pid":{pid}}}'.encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def log_message(self, *a): pass

        server = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.worker_health = f"http://127.0.0.1:{server.server_address[1]}/health"

    def test_an_unpatched_running_worker_is_reported(self):
        stray = Path(self.tmp.name) / "stray"
        stray.mkdir()
        (stray / "worker-service.cjs").write_bytes(STOCK)
        self.serve(str(stray / "worker-service.cjs"))
        self.write_target(PATCHED, VERSION)        # 文件都对，只有在跑的那个不对
        log = self.run_guard()
        self.assertIn("在位 worker 跑的不是打过补丁的那份", log)

    def test_a_patched_running_worker_stays_silent(self):
        self.write_target(PATCHED, VERSION)
        self.serve(str(self.bundle))
        self.assertEqual(self.run_guard(), "")


class WiringTests(unittest.TestCase):
    """守护挂没挂上 300s 那条节拍 —— 以及挂上之后会不会**沉默地**失效。

    这一类不是洁癖：断路器那条链路 2026-09-10 接线、退出码全 0 跑了 2784 轮，
    而那个开关按下去什么都不会发生，八天没人看得出来。「看起来接上了」和
    「真的通了」是两回事，而前者不会自己变成后者。
    """

    def setUp(self):
        self.guard = (ROOT / "tools" / "claude_mem_guard.sh").read_text("utf-8")
        self.line = next(
            (line for line in self.guard.splitlines()
             if "claude_mem_patch_guard.sh" in line and not line.lstrip().startswith("#")),
            None,
        )

    def test_the_300s_guard_actually_invokes_the_patch_guard(self):
        self.assertIsNotNone(self.line, "补丁守护没有被 300s 那条 guard 调用")

    def test_the_invocation_does_not_swallow_stderr(self):
        """预料之外的失败（语法错、python 不在、权限）只会写 stderr。

        把它丢进 /dev/null，claude-mem-guard.err.log 就永远是空的 —— 而那是
        唯一能看出「守护自己挂了」的地方。守护内部那三态只覆盖它预料到的情形。
        """
        self.assertNotIn("2>/dev/null", self.line)

    def test_the_patch_guard_runs_before_the_early_exit(self):
        """那条 guard 在没有空转进程时 `exit 0`。调用排在它后面就等于从不执行。

        而且**不会有任何症状**：退出码照样是 0。
        """
        call = self.guard.index("claude_mem_patch_guard.sh")
        early_exit = self.guard.index('[ -z "$CANDIDATES" ] && exit 0')
        self.assertLess(call, early_exit, "补丁守护排在了提前 exit 后面，永远跑不到")

    def test_the_plist_template_points_at_the_guard_we_edited(self):
        """线上跑的必须就是仓库里这个文件，不是某份副本。"""
        template = (ROOT / "deploy" / "mac" / "agents"
                    / "com.kg-hub.claude-mem-guard.plist").read_text("utf-8")
        self.assertIn("tools/claude_mem_guard.sh", template)


if __name__ == "__main__":
    unittest.main()
