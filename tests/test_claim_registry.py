"""tools/claim_registry：全设备在途领取登记——互斥、只放自己的、超时失效、并发不重叠。"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock

from tools import claim_registry as cr

REPO = _Path(__file__).resolve().parent.parent

# 以当前时间为基准：release / 命令行用的是真实时钟，写死日期过几天这些断言就会因为登记「已过期」而失效
T0 = datetime.now(timezone.utc)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"KG_HUB_CLAIMS_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_reserve_skips_others_and_renews_own(self):
        with cr.opened(T0) as reg:
            self.assertEqual(reg.reserve([1, 2, 3], "campaign:a", "a-001"), [1, 2, 3])
        with cr.opened(T0) as reg:
            self.assertEqual(reg.held_by_others("campaign:b"), {1, 2, 3})
            self.assertEqual(reg.held_by_others("campaign:a"), set())            # 自己占着的不挡自己
            self.assertEqual(reg.reserve([2, 3, 4], "campaign:b", "b-001"), [4])
            self.assertEqual(reg.reserve([1], "campaign:a", "a-002"), [1])       # 自己的续期
        data = json.loads((_Path(self.tmp.name) / "inflight.json").read_text())["claims"]
        self.assertEqual({k: v["owner"] for k, v in data.items()},
                         {"1": "campaign:a", "2": "campaign:a", "3": "campaign:a", "4": "campaign:b"})
        self.assertEqual(data["1"]["info"], "a-002")

    def test_release_only_own(self):
        with cr.opened(T0) as reg:
            reg.reserve([1, 2], "next:/x")
        self.assertEqual(cr.release([1, 2], "campaign:a"), 0)                    # 别人的放不掉
        self.assertEqual(cr.release([1], "next:/x"), 1)
        with cr.opened(T0) as reg:
            self.assertEqual(reg.held_by_others("campaign:a"), {2})
            self.assertEqual(reg.release_owner("next:/x"), 1)
            self.assertEqual(reg.entries, {})

    def test_expired_claims_drop_out(self):
        with cr.opened(T0) as reg:
            reg.reserve([1], "campaign:a")
        with cr.opened(T0 + timedelta(hours=11)) as reg:
            self.assertEqual(reg.held_by_others("campaign:b"), {1})
        with cr.opened(T0 + timedelta(hours=13)) as reg:                       # 默认 12 小时
            self.assertEqual(reg.held_by_others("campaign:b"), set())
            self.assertEqual(reg.reserve([1], "campaign:b"), [1])
        with mock.patch.dict(os.environ, {"KG_HUB_CLAIM_TTL_HOURS": "1"}):
            with cr.opened(T0) as reg:
                reg.reserve([9], "campaign:c")
            with cr.opened(T0 + timedelta(minutes=61)) as reg:
                self.assertNotIn(9, reg.held_by_others("campaign:x"))

    def test_concurrent_reservations_never_share_an_id(self):
        def grab(k):
            with cr.opened() as reg:
                return reg.reserve(range(k * 5, k * 5 + 20), f"campaign:{k}")
        with ThreadPoolExecutor(max_workers=8) as ex:
            got = list(ex.map(grab, range(8)))
        flat = [i for g in got for i in g]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(set(flat), set(range(0, 55)))

    def test_cli_list_release_purge(self):
        with cr.opened(T0) as reg:
            reg.reserve([1, 2], "campaign:a")
        with mock.patch("sys.stdout") as out:
            cr.main(["release", "--owner", "campaign:a", "--ids", "1"])
        with cr.opened(T0) as reg:
            self.assertEqual(set(reg.entries), {"2"})


    # ---------- 操作日志（2026-10-24：38252 被两个战役领到，事后查不到登记何时、被谁注销） ----------

    def journal(self):
        j = _Path(self.tmp.name) / "journal.jsonl"
        return [json.loads(x) for x in j.read_text().splitlines()] if j.exists() else []

    def test_journal_records_every_operation_with_context(self):
        with cr.opened(T0) as reg:
            reg.reserve([1, 2, 3], "campaign:a", "a-001")
        with cr.opened(T0) as reg:
            reg.reserve([3, 4], "campaign:b", "b-001")              # 3 被 a 占着
            reg.release([1, 9], "campaign:b")                         # 1 不是 b 的，9 不存在
            reg.release([2], "campaign:a")
        with cr.opened(T0 + timedelta(hours=13)) as reg:              # 过期清理
            pass
        ev = self.journal()
        self.assertEqual([e["op"] for e in ev], ["reserve", "reserve", "release", "release", "expire", "expire"])
        self.assertEqual((ev[1]["ids"], ev[1]["skipped_held"]), ([4], [3]))
        self.assertEqual((ev[2]["ids"], ev[2]["not_mine"], ev[2]["absent"]), ([], {"campaign:a": [1]}, [9]))
        self.assertEqual({(e["owner"], tuple(e["ids"])) for e in ev[4:]}, {("campaign:a", (1, 3)), ("campaign:b", (4,))})
        for e in ev:                                                  # 每行都带是谁动的
            self.assertEqual((e["pid"], e["cwd"]), (os.getpid(), os.getcwd()))
            self.assertIn("ppid", e)
            self.assertIn("argv", e)
        self.assertEqual([e["rev"] for e in ev], [1, 2, 2, 2, 3, 3])
        self.assertEqual(json.loads((_Path(self.tmp.name) / "inflight.json").read_text())["rev"], 3)

    def test_read_only_access_writes_nothing(self):
        with cr.opened(T0) as reg:
            reg.reserve([1], "campaign:a")
        with cr.opened(T0) as reg:
            reg.held_by_others("campaign:b")
        self.assertEqual(len(self.journal()), 1)

    def test_external_change_is_detected(self):
        with cr.opened(T0) as reg:
            reg.reserve([1, 2], "campaign:a")
        f = _Path(self.tmp.name) / "inflight.json"
        d = json.loads(f.read_text())
        del d["claims"]["1"]                                          # 有人绕过锁直接改文件、rev 没动
        d["rev"] = 7
        f.write_text(json.dumps(d))
        with cr.opened(T0) as reg:
            self.assertEqual(reg.held_by_others("campaign:b"), {2})
        ev = self.journal()
        self.assertEqual(ev[-1]["op"], "external_change")
        self.assertEqual((ev[-1]["file_rev"], ev[-1]["journal_rev"]), (7, 1))
        with cr.opened(T0) as reg:                                    # 记过一次之后对上了，不再重复报
            reg.reserve([5], "campaign:a")
        self.assertEqual([e["op"] for e in self.journal()][-1], "reserve")

    def test_history_spans_rotated_journal(self):
        with cr.opened(T0) as reg:
            reg.reserve([38252, 1], "campaign:codex", "gpt-6.1-sol-006")
        with cr.opened(T0) as reg:
            reg.reserve([38252], "campaign:fable", "fable-002")
        with mock.patch.object(cr, "JOURNAL_MAX_BYTES", 1):             # 第三次写入前滚动：前两行进旧档
            with cr.opened(T0) as reg:
                reg.release([38252], "campaign:fable")
        self.assertTrue((_Path(self.tmp.name) / "journal.jsonl.1").exists())
        h = cr.history(38252)
        self.assertEqual([(e["op"], e["owner"]) for e in h],
                         [("reserve", "campaign:codex"), ("reserve", "campaign:fable"), ("release", "campaign:fable")])
        self.assertEqual(h[1]["skipped_held"], [38252])               # 被挡住的那次也查得到
        self.assertEqual(h[2]["not_mine"], {"campaign:codex": [38252]})
        with mock.patch("sys.stdout") as out:
            cr.main(["history", "--id", "38252"])
        self.assertEqual(sum(c.args[0].count("\n") for c in out.write.call_args_list if c.args), 3)

    def test_concurrent_processes_keep_journal_lines_whole_and_revs_increasing(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, %r)\nfrom tools import claim_registry as cr\n"
                "k = int(sys.argv[1])\nfor j in range(10):\n"
                "    with cr.opened() as reg: reg.reserve([k * 100 + j], 'campaign:%%d' %% k)\n") % str(REPO)
        procs = [subprocess.Popen([sys.executable, "-c", code, str(k)], env={**os.environ}) for k in range(6)]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        ev = self.journal()
        self.assertEqual(len(ev), 60)
        self.assertEqual([e["rev"] for e in ev], list(range(1, 61)))   # 每次写入一行、版本严格递增
        self.assertEqual(len({e["pid"] for e in ev}), 6)


if __name__ == "__main__":
    unittest.main()
