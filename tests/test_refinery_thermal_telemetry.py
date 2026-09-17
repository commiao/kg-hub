"""温控遥测:先量清楚,再谈要不要改判定。

2026-09-17 查"每天恰好 208 条"的来历时发现,真因是温度门控:阈值 59,而盘温
稳态正好 59,判定是 `>=`,于是每 90 秒歇工一次、连着几小时。当天配额只用掉
215/5000 —— 瓶颈根本不在配额。

更要紧的是闸门**拦错了盘**:它取 max(全部盘),当时 sata1=57 sata2=58(都属
/volume1),而 kg-hub 的数据根 /volume2/4T 落在 sata3=47。管线被两块自己根本
不写的盘停掉,白白浪费 11 度余量。

但不能就此把判定换成只看承载盘:同机箱有热耦合,是不是 kg-hub 自己干活把
sata1/2 带热了,这是实证问题;而 DSM 强制关机线只有 ~61°C,判断错的代价是整台
NAS 停机(2026-08 已发生过一次,持续两周)。

所以这一步**只加记录,判定一个字不改**。本测试就钉这两件事:
  1. 记录确实记下来了(每块盘分列、当日歇工累计、承载盘单列);
  2. 判定依据仍然是 max(全部盘) —— 谁要是顺手把它改成只看承载盘,这里必须红。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kg_refinery as R  # noqa: E402


class DiskTempsTests(unittest.TestCase):

    def setUp(self):
        self._dir = R.DISKTEMP_DIR

    def tearDown(self):
        R.DISKTEMP_DIR = self._dir

    def _fake_disks(self, mapping):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        for name, t in mapping.items():
            d = tmp / name
            d.mkdir()
            (d / "temperature").write_text(str(t))
        R.DISKTEMP_DIR = tmp
        return tmp

    def test_each_disk_is_reported_separately(self):
        """原来只回一个最大值,于是"是哪块盘热"在状态里根本没答案 ——
        而它恰恰是判断闸门有没有拦错盘的唯一依据。"""
        self._fake_disks({"sata1": 57, "sata2": 58, "sata3": 47})
        self.assertEqual(R.disk_temps(), {"sata1": 57, "sata2": 58, "sata3": 47})

    def test_gate_still_uses_the_max_across_all_disks(self):
        """判定依据没变。谁把它改成只看承载盘,这条必须红 —— 那是要用几天实测
        数据来支撑的决定,不能顺手改掉。"""
        self._fake_disks({"sata1": 57, "sata2": 58, "sata3": 47})
        self.assertEqual(R.max_disk_temp(), 58, "闸门仍须取全盘最大值")

    def test_unreadable_disks_are_skipped_not_faked(self):
        tmp = self._fake_disks({"sata1": 50})
        bad = tmp / "sata9"
        bad.mkdir()
        (bad / "temperature").write_text("这不是数字")
        self.assertEqual(R.disk_temps(), {"sata1": 50})

    def test_no_disks_means_do_not_block(self):
        """读不到盘温(非群晖/未挂载)必须是"不拦",不能变成"一律歇工"。"""
        self._fake_disks({})
        self.assertEqual(R.disk_temps(), {})
        self.assertIsNone(R.max_disk_temp())


class ThermalAccountingTests(unittest.TestCase):

    def setUp(self):
        R._thermal_today.update(day="", holds=0, seconds=0)

    def test_holds_accumulate_into_minutes(self):
        temps = {"sata1": 60, "sata3": 47}
        for _ in range(4):
            out = R.note_thermal(True, temps)
        self.assertEqual(out["holds"], 4)
        self.assertEqual(out["minutes"], round(4 * R.INTERVAL / 60, 1))

    def test_working_cycles_do_not_inflate_the_hold_counter(self):
        R.note_thermal(True, {"sata1": 60})
        out = R.note_thermal(False, {"sata1": 50})
        self.assertEqual(out["holds"], 1, "没歇工的轮次不该计入")

    def test_counter_resets_on_utc_day_rollover(self):
        R.note_thermal(True, {"sata1": 60})
        R._thermal_today["day"] = "1999-01-01"      # 假装跨天
        out = R.note_thermal(False, {"sata1": 50})
        self.assertEqual(out["holds"], 0)
        self.assertEqual(out["minutes"], 0.0)

    def test_data_disk_is_singled_out_when_configured(self):
        """承载盘单列一栏 = "本来只看自己那块会是多少"。没有这一栏,
        "闸门拦错盘"这个判断就只能靠人去 ssh 上翻。"""
        R.DATA_DISK = "sata3"
        try:
            out = R.note_thermal(True, {"sata1": 57, "sata2": 58, "sata3": 47})
            self.assertEqual(out["data_disk"], "sata3")
            self.assertEqual(out["data_disk_temp"], 47)
            self.assertEqual(out["disks"], {"sata1": 57, "sata2": 58, "sata3": 47})
            self.assertEqual(out["threshold"], R.MAX_DISK_TEMP)
        finally:
            R.DATA_DISK = ""

    def test_unconfigured_data_disk_adds_no_field(self):
        R.DATA_DISK = ""
        out = R.note_thermal(False, {"sata1": 50})
        self.assertNotIn("data_disk", out)

    def test_missing_data_disk_reports_none_not_a_guess(self):
        """配了名字但那块盘读不到时,要如实给 None,不能拿别的盘顶上。"""
        R.DATA_DISK = "sata9"
        try:
            out = R.note_thermal(False, {"sata1": 50})
            self.assertIsNone(out["data_disk_temp"])
        finally:
            R.DATA_DISK = ""


if __name__ == "__main__":
    unittest.main()
