"""分诊进复盘报告与飞书摘要：批次计数读取、按模型汇总、打满判断、可省 token、保留率取最新评分、只推一次、抽样。

不发真飞书：send 的 runner 一律注入假函数；状态目录、bench 目录都指临时目录。"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from tools import extract_campaign as ec
from tools import triage_notify as tn

NOW = datetime.now(timezone.utc)


def stats(drop, review, picked=50, rejected=10, dropped=0, already=0, triaged=None):
    s = {"requeued": 0, "seen": 200, "rejected_by_filter": rejected, "already_ok": already, "pending_elsewhere": 0,
         "picked": picked, "triage_drop": drop, "triage_review": review, "triage_dropped": dropped}
    if triaged is not None:
        s["triaged"] = triaged
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": str(self.root / "state"),
                                                "KG_HUB_BENCH_ROOT": str(self.root / "bench")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def campaign(self, cid, batches, quota=100, models=("m-a", "m-b")):
        """batches: [(model, n_items, status, stats|None, enabled, tokens|None, claimed_at, tiers)]"""
        d = ec.campaign_dir(cid)
        (d / "batches").mkdir(parents=True)
        c = {"tool": ec.TOOL, "id": cid, "quota": quota, "batch": 50, "prompt_version": "agent-v4", "chain": "c0",
             "models": {m: {"run_id": f"{cid}-{m}"} for m in models}, "batches": [], "cursor": 0}
        for k, (model, n, status, st, enabled, tokens, at, tiers) in enumerate(batches):
            bid = f"{model}-{k:03d}"
            ids = list(range(k * 100, k * 100 + n))
            items = [{"id": i, "episode_body": f"[DISCOVERY] title {i}\nbody"} for i in ids]
            for i, t in zip(ids, tiers or []):
                if t:
                    items[ids.index(i)]["triage_tier"] = t
            bf = {"batch_id": bid, "items": items}
            if st is not None:
                bf["stats"] = st
                bf["triage"] = {"version": "triage-v1", "enabled": enabled}
            else:
                bf["stats"] = {"seen": 200, "rejected_by_filter": 5, "picked": n}
            (d / "batches" / f"{bid}.json").write_text(json.dumps(bf))
            c["batches"].append({"id": bid, "model": model, "chain": "c0", "ids": ids, "status": status,
                                 "claimed_at": at or NOW.isoformat(), "cost": {"tokens": tokens} if tokens else {}})
        (d / "campaign.json").write_text(json.dumps(c))
        return d, c


class TriageReportTest(Base):
    def test_batch_triage_reads_counts_and_derives_triaged(self):
        d, c = self.campaign("c1", [("m-a", 50, "submitted", stats(4, 9, already=3, dropped=0), False, None, None,
                                     ["drop", "review", None, "drop"]),
                                    ("m-a", 50, "submitted", None, False, None, None, None)])
        t = ec.batch_triage(d, "m-a-000")
        self.assertEqual((t["gate_reject"], t["triaged"], t["triage_drop"], t["triage_review"]), (10, 53, 4, 9))
        self.assertEqual((t["drop_ids"], t["review_ids"]), ([0, 3], [1]))
        self.assertEqual(t["triage_version"], "triage-v1")
        self.assertIsNone(ec.batch_triage(d, "m-a-001"))                 # 分诊上线前领的批
        self.assertIsNone(ec.batch_triage(d, "nope"))
        # 新版 stats 自带 triaged 时以它为准
        d2, _ = self.campaign("c2", [("m-a", 50, "submitted", stats(1, 1, triaged=77), False, None, None, None)])
        self.assertEqual(ec.batch_triage(d2, "m-a-000")["triaged"], 77)

    def test_pcts(self):
        p = ec.triage_pcts({"gate_reject": 25, "triaged": 75, "triage_drop": 15, "triage_review": 30})
        self.assertEqual(p, {"gate_reject_pct": 25.0, "triage_drop_pct": 20.0, "triage_review_pct": 40.0})
        self.assertEqual(ec.triage_pcts({"gate_reject": 0, "triaged": 0, "triage_drop": 0, "triage_review": 0}),
                         {"gate_reject_pct": None, "triage_drop_pct": None, "triage_review_pct": None})

    def test_summary_tokens_full_and_since(self):
        old = (NOW - timedelta(days=10)).isoformat()
        d, c = self.campaign("c3", [
            ("m-a", 50, "submitted", stats(5, 10), False, 200000, None, None),      # 4000 token/条，影子：可省 5×
            ("m-a", 50, "submitted", stats(5, 0), False, 300000, old, None),        # 6000 token/条
            ("m-b", 50, "submitted", stats(4, 0, dropped=4), True, 100000, None, None),   # 启用批：判丢的都跳过了，已省 4×
            ("m-b", 50, "claimed", None, False, None, None, None)])
        s = ec.triage_summary(d, c)
        a, b = s["models"]["m-a"], s["models"]["m-b"]
        self.assertEqual((a["triage_drop"], a["tokens_per_obs"], a["tokens_saveable_est"], a["tokens_saved_est"]),
                         (10, 5000, 50000, 0))
        self.assertEqual((b["tokens_per_obs"], b["tokens_saved_est"], b["tokens_saveable_est"], b["triage_batches"]),
                         (2000, 8000, 0, 1))                                      # 启用批不再算「可省」
        self.assertEqual((s["total"]["tokens_saveable_est"], s["total"]["tokens_saved_est"]), (50000, 8000))
        self.assertFalse(s["full"])                                               # m-b 有没提交的批、也没领满
        self.assertEqual(s["enabled"], [False, True])
        week = ec.triage_summary(d, c, since=(NOW - timedelta(days=7)).isoformat())
        self.assertEqual(week["models"]["m-a"]["batches"], 1)                     # 10 天前那批不算
        self.assertEqual(week["models"]["m-a"]["tokens_saveable_est"], 20000)

    def test_campaign_full(self):
        _, c = self.campaign("c4", [("m-a", 50, "submitted", None, False, None, None, None),
                                    ("m-a", 50, "submitted", None, False, None, None, None)], models=("m-a",))
        self.assertTrue(ec.campaign_full(c))
        c["batches"][1]["status"] = "claimed"
        self.assertFalse(ec.campaign_full(c))
        c["batches"][1].update(status="submitted", released_ids=[100])              # 回收的还给配额
        self.assertFalse(ec.campaign_full(c))

    def test_report_rows_and_render(self):
        d, c = self.campaign("c5", [("m-a", 2, "submitted", stats(1, 1, picked=2, rejected=2), False, None, None, None),
                                    ("m-a", 2, "submitted", stats(1, 0, picked=2, rejected=6), False, None, None, None)])
        (d / "results").mkdir()
        rows = [ec.batch_row(d, b) for b in c["batches"]]
        self.assertEqual((rows[0]["triage_drop"], rows[0]["triage_drop_pct"], rows[0]["gate_reject_pct"]), (1, 50.0, 50.0))
        m = ec.merge_rows("合计", rows)
        # 合计比例从相加后的计数重算，不是各批比例的平均
        self.assertEqual((m["triage_drop"], m["triaged"], m["triage_drop_pct"], m["gate_reject_pct"]), (2, 4, 50.0, 66.7))
        self.assertEqual(m["triage_batches"], 2)
        txt = ec.render([m])
        self.assertIn("分诊判丢", txt)
        self.assertIn("| 66.7 |", txt)
        no = ec.merge_rows("旧", [dict(rows[0], _triage=None)])
        self.assertNotIn("triage_drop_pct", no)
        self.assertIn("| — |", ec.render([no]))

    def test_claim_writes_triage_version(self):
        with mock.patch.object(ec.ax, "_triage_cfg", return_value={"version": "triage-v9", "enabled": True}):
            self.assertEqual(ec.triage_info(), {"version": "triage-v9", "enabled": True})
        with mock.patch.object(ec.ax, "_triage_cfg", return_value={}):
            self.assertEqual(ec.triage_info(), {"version": None, "enabled": False})


class NotifyTest(Base):
    def run_record(self, version, h_kill, l_drop, mtime_shift=0):
        runs = self.root / "bench" / "obs-x" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        p = runs / f"r-{version}-{h_kill}.json"
        p.write_text(json.dumps({"bench_id": "obs-x", "rules_version": version, "at": "t",
                                 "splits": {"all": {"n": 10, "h_kill_pct": h_kill, "h_kill_pct_baseline": 8.0,
                                                    "l_drop_pct": l_drop, "l_drop_pct_baseline": 30.0}}}))
        os.utime(p, (NOW.timestamp() + mtime_shift, NOW.timestamp() + mtime_shift))

    def test_bench_retention_picks_latest_of_version(self):
        self.assertIsNone(tn.bench_retention("triage-v1"))
        self.run_record("triage-v1", 10.0, 50.0, -100)
        self.run_record("triage-v1", 4.0, 80.0, 0)
        self.run_record("triage-v0", 0.0, 0.0, 100)                                # 别的版本不算
        r = tn.bench_retention("triage-v1")
        self.assertEqual((r["h_keep_pct"], r["h_keep_pct_baseline"], r["l_keep_pct"], r["l_keep_pct_baseline"]),
                         (96.0, 92.0, 20.0, 70.0))

    def test_message_has_the_three_numbers(self):
        d, c = self.campaign("c6", [("m-a", 50, "submitted", stats(5, 10), False, 200000, None, ["drop"]),
                                    ("m-a", 50, "submitted", stats(5, 0), False, 200000, None, None)], models=("m-a",))
        s = ec.triage_summary(d, c)
        msg = tn.build_message([("c6", s), ("old", {**s, "total": {**s["total"], "triage_batches": 0}})],
                               {"rules_version": "triage-v1", "n": 10, "h_keep_pct": 96.0, "h_keep_pct_baseline": 92.0,
                                "l_keep_pct": 20.0, "l_keep_pct_baseline": 70.0}, kind="战役打满")
        self.assertIn("高价值 96% / 92%", msg)
        self.assertIn("低价值 20% / 70%", msg)
        self.assertIn("启用就能省约 4.0 万", msg)                                   # 10 条 × 4000
        self.assertIn("已打满", msg)
        self.assertIn("sample --id c6", msg)                                       # 打满 + 影子 + 有判丢 → 提示抽样复查
        self.assertIn("另有 1 条战役", msg)
        self.assertIn("保留率：当前规则版本还没在冻结样本上评过分", tn.build_message([], None, kind="周报"))

    def test_notify_once_and_retry_after_failure(self):
        self.campaign("c7", [("m-a", 50, "submitted", stats(5, 1), False, None, None, None),
                             ("m-a", 50, "submitted", stats(1, 1), False, None, None, None)], models=("m-a",))
        sent = []
        fake_send = mock.Mock(side_effect=lambda cmd, **k: sent.append(cmd) or SimpleNamespace(returncode=1))
        with mock.patch.object(tn, "FEISHU_SEND", self.root / "send.py"), mock.patch("sys.stdout"):
            (self.root / "send.py").write_text("")
            self.assertFalse(tn.notify_if_full("c7", runner=fake_send))           # 推送失败：不记，下次再推
            fake_send.side_effect = lambda cmd, **k: sent.append(cmd) or SimpleNamespace(returncode=0)
            self.assertTrue(tn.notify_if_full("c7", runner=fake_send))
            self.assertFalse(tn.notify_if_full("c7", runner=fake_send))           # 只推一次
        self.assertEqual(len(sent), 2)
        self.assertIn("--webhook", sent[-1])
        self.assertIn("kg-hub", sent[-1])
        self.assertIn("c7", json.loads(tn.state_path().read_text())["full_notified"])

    def test_not_full_or_legacy_never_pushes(self):
        self.campaign("c8", [("m-a", 50, "submitted", stats(5, 1), False, None, None, None)], models=("m-a",))
        self.campaign("c9", [("m-a", 50, "submitted", None, False, None, None, None),
                             ("m-a", 50, "submitted", None, False, None, None, None)], models=("m-a",))
        runner = mock.Mock(return_value=SimpleNamespace(returncode=0))
        with mock.patch.object(tn, "FEISHU_SEND", self.root / "send.py"), mock.patch("sys.stdout"):
            (self.root / "send.py").write_text("")
            self.assertFalse(tn.notify_if_full("c8", runner=runner))              # 没领满
            self.assertFalse(tn.notify_if_full("c9", runner=runner))              # 打满但分诊上线前领的
        runner.assert_not_called()

    def test_dry_and_missing_sender_do_not_push(self):
        runner = mock.Mock()
        with mock.patch("sys.stdout"):
            self.assertFalse(tn.send("x", dry=True, runner=runner))
            with mock.patch.object(tn, "FEISHU_SEND", self.root / "absent.py"), mock.patch("sys.stderr"):
                self.assertFalse(tn.send("x", dry=False, runner=runner))
        runner.assert_not_called()

    def test_weekly_covers_recent_and_backfills_full(self):
        old = (NOW - timedelta(days=9)).isoformat()
        self.campaign("w1", [("m-a", 50, "submitted", stats(5, 1), False, None, None, None),
                             ("m-a", 50, "submitted", stats(2, 1), False, None, None, None)], models=("m-a",))
        self.campaign("w2", [("m-a", 50, "submitted", stats(9, 9), False, None, old, None)], quota=500, models=("m-a",))
        msgs = []
        runner = lambda cmd, **k: msgs.append(cmd[2]) or SimpleNamespace(returncode=0)  # noqa: E731
        with mock.patch.object(tn, "FEISHU_SEND", self.root / "send.py"), mock.patch("sys.stdout"):
            (self.root / "send.py").write_text("")
            tn.weekly(runner=runner)
        self.assertEqual(len(msgs), 2)                                             # 周报 + w1 的打满补推
        self.assertIn("w1", msgs[0])
        self.assertNotIn("w2", msgs[0])                                            # 9 天前的批不进周报
        self.assertIn("战役打满", msgs[1])

    def test_sample_drop_tier(self):
        self.campaign("s1", [("m-a", 5, "submitted", stats(2, 1, picked=5), False, None, None,
                              ["drop", None, "review", "drop", None])], models=("m-a",))
        got = tn.sample("s1", "drop", 20)
        self.assertEqual(sorted(x["id"] for x in got), [0, 3])
        self.assertEqual(got[0]["title"][:12], "[DISCOVERY] ")
        self.assertEqual([x["id"] for x in tn.sample("s1", "review", 20)], [2])
        self.assertEqual(len(tn.sample("s1", "drop", 1)), 1)


if __name__ == "__main__":
    unittest.main()
