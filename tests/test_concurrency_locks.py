"""多进程并发：两个以上工具同时跑时不冲突（2026-10-24 实测出的两处）。

1. 同一状态目录同时 `agent_extract next`：曾领到完全相同的一批、批次文件互相覆盖、批次号只记 1。
2. 同时对同一模型 `admit` 写准入记录：读改写无锁会丢记录、非原子写会让读者读到半截文件。

用真实子进程（不是线程）跑，取观测与查重用假函数，并故意放慢临界区里的操作把竞争窗口拉大——
去掉锁时这些用例必然转红，而不是碰运气。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = _Path(__file__).resolve().parent.parent

NEXT_WORKER = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, {repo!r})
    from tools import agent_extract as ax

    def fetch(after, limit):
        time.sleep(0.4)        # 放慢：没有状态目录锁时，几个进程会读到同一个游标
        rows = [{{"id": i, "title": "t%d" % i}} for i in range(after + 1, after + 1 + limit)]
        return {{"rows": rows, "boundary": 0, "after": after, "skipped_by_watermark": 0, "exhausted": False}}

    def select(rows, cfg, n, existing):
        picked = [{{"id": r["id"], "name": "obs-%d" % r["id"], "source_description": "sd", "source_obs_id": "h%d" % r["id"],
                   "reference_time": "t", "episode_body": "[DISCOVERY] t%d" % r["id"]}} for r in rows][:n]
        return picked, {{"seen": len(rows), "picked": len(picked)}}

    ax.fetch_remote_pending = fetch
    ax.select_batch = select
    ax.load_config = lambda: {{}}
    ax.check_existing = lambda idents, url, token: {{}}
    ax.api_token = lambda *a, **k: "t"
    # This harness exercises process locks, not NAS capabilities/tracking.
    # Production preflight remains mandatory; its real gate has separate tests.
    ax.runtime_preflight = lambda *a, **k: {{"ok": True, "fixture": "isolated-lock-worker"}}
    def no_http(*a, **k):
        raise AssertionError("lock fixture must never reach network")
    ax._http = no_http
    sys.exit(ax.main(["next", "--n", "5"] + sys.argv[1:]))
''')

ADMIT_WORKER = textwrap.dedent('''
    import sys, time, pathlib
    sys.path.insert(0, {repo!r})
    from tools import extract_bench as xb
    real = pathlib.Path.write_text

    def slow(self, *a, **k):   # 放慢写入：没有锁时读改写会互相覆盖
        time.sleep(0.05)
        return real(self, *a, **k)
    pathlib.Path.write_text = slow
    for k in range(int(sys.argv[2])):
        xb.append_admission("m-x", {{"verdict": "pass", "who": sys.argv[1], "k": k}})
''')

READER = textwrap.dedent('''
    import sys, json, time, pathlib
    f = pathlib.Path(sys.argv[1])
    bad = 0
    end = time.time() + float(sys.argv[2])
    while time.time() < end:
        if f.exists():
            try:
                json.loads(f.read_text())
            except ValueError:
                bad += 1
    print(bad)
''')


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _Path(self.tmp.name)
        self.env = {**os.environ, "KG_HUB_AGENT_EXTRACT_DIR": str(self.root / "state"),
                    "KG_HUB_CLAIMS_DIR": str(self.root / "claims"), "KG_HUB_BENCH_ROOT": str(self.root / "bench"),
                    "HOME": str(self.root / "home")}

    def tearDown(self):
        self.tmp.cleanup()

    def script(self, name, body):
        p = self.root / name
        p.write_text(body.format(repo=str(REPO)))
        return p

    def run_parallel(self, cmds):
        procs = [subprocess.Popen(c, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for c in cmds]
        outs = [p.communicate(timeout=120) for p in procs]
        for p, (o, e) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, e[-2000:])
        return [o for o, _ in outs]

    def batches(self):
        sd = self.root / "state"
        files = sorted(sd.glob("batch-*.json"))
        return files, [[i["id"] for i in json.loads(f.read_text())["items"]] for f in files], \
            json.loads((sd / "state.json").read_text())

    def test_four_processes_next_in_one_state_dir_get_distinct_batches(self):
        w = self.script("next_worker.py", NEXT_WORKER)
        self.run_parallel([[sys.executable, str(w)] for _ in range(4)])
        files, ids, st = self.batches()
        self.assertEqual([f.name for f in files], ["batch-b0001.json", "batch-b0002.json", "batch-b0003.json",
                                                   "batch-b0004.json"])
        flat = [i for b in ids for i in b]
        self.assertEqual((len(flat), len(set(flat))), (20, 20))          # 4 批 × 5 条，没有一条重复
        self.assertEqual((st["batches"], st["cursor"]), (4, 20))

    def test_same_explicit_after_id_still_does_not_overlap(self):
        """两个工具都显式给了同一个 --after-id：锁让它们排队，按批登记让后一批避开前一批没交的条目。"""
        w = self.script("next_worker.py", NEXT_WORKER)
        self.run_parallel([[sys.executable, str(w), "--after-id", "100"] for _ in range(3)])
        files, ids, st = self.batches()
        flat = [i for b in ids for i in b]
        self.assertEqual(len(files), 3)
        self.assertEqual((len(flat), len(set(flat))), (15, 15))
        self.assertTrue(all(i > 100 for i in flat))

    def test_concurrent_admit_keeps_every_record_and_readers_never_see_partial_json(self):
        w = self.script("admit_worker.py", ADMIT_WORKER)
        r = self.script("reader.py", READER)
        f = self.root / "bench" / "admissions" / "m-x.json"
        cmds = [[sys.executable, str(w), f"p{k}", "5"] for k in range(4)] + [[sys.executable, str(r), str(f), "4"]]
        outs = self.run_parallel(cmds)
        hist = json.loads(f.read_text())["history"]
        self.assertEqual(len(hist), 20)                                   # 4 进程 × 5 条，一条不丢
        self.assertEqual(sorted((h["who"], h["k"]) for h in hist), sorted((f"p{p}", k) for p in range(4) for k in range(5)))
        self.assertEqual(outs[-1].strip(), "0")                           # 读者从没读到半截 JSON
        self.assertEqual(list(f.parent.glob("*.tmp*")), [])                # 临时文件都换掉了


if __name__ == "__main__":
    unittest.main()
