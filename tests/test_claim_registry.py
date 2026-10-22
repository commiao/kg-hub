"""tools/claim_registry：全设备在途领取登记——互斥、只放自己的、超时失效、并发不重叠。"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock

from tools import claim_registry as cr

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


if __name__ == "__main__":
    unittest.main()
