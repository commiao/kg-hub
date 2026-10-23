"""utils/obs_triage + tools/obs_bench：规则命中、加减分、三档判定、冻结样本的闸判定还原、冻结→评分端到端、选批接入。

每条规则至少一正一反；变异验证（把规则/权重/豁免改坏看用例转红）记录在 docs/OBSERVATION-TRIAGE.md。"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import json
import os
import tempfile
import unittest
from unittest import mock

from utils import obs_triage as ot
from utils.ingest_filter import load_config
from tools import obs_bench as ob
from tools import agent_extract as ax

FCFG = load_config()
TCFG = ot.load_triage_config()
LONG = ("The gateway returns 503 whenever the breaker is open; the root cause is that the readiness probe only checks the "
        "process, not the upstream. Because of that the deploy looked healthy while every call failed. " * 5)


def obs(title, narrative=LONG, type_="discovery", facts=tuple(f"fact {i}" for i in range(6)), **kw):
    return {"id": kw.pop("id", 1), "project": "p", "type": type_, "title": title, "subtitle": "", "narrative": narrative,
            "facts": json.dumps(list(facts)), "files_modified": "[]", "platform_source": "claude", "relevance_count": 0, **kw}


class RuleTest(unittest.TestCase):
    def hit(self, name, title, text=""):
        return ot.RULES_BY_NAME[name].hits(title, text or title)

    def test_status_report(self):
        for t in ("PR #203 and #204 already merged into main", "NAS task-hub deployed at 16da0ec; health=200",
                  "release/pre HEAD sits level with origin", "Fix applied, version bumped to 1.2", "线上版本核对：已部署 01e337e",
                  "Dashboard health=200 and drift verdict clean", "Task-hub service on NAS healthy with 315 tasks"):
            self.assertTrue(self.hit("status_report", t), t)
        for t in ("Canonical doc re-ingest preserves usage_count", "Legacy observation IS present despite cursor 0",
                  "Extraction evaluation framework doc added: cross-tool"):
            self.assertFalse(self.hit("status_report", t), t)

    def test_process_record(self):
        for t in ("API_FREE channel code investigation plan established", "Work paused pending online-branch fix",
                  "Codex hook trust worktrees archived and PRs opened", "Campaign batch submitted successfully"):
            self.assertTrue(self.hit("process_record", t), t)
        for t in ("kg-hub manual circuit breaker opened for entity extract", "Proposed retry semantics for the mailbox"):
            self.assertFalse(self.hit("process_record", t), t)

    def test_session_ephemeral_reads_body(self):
        r = ot.RULES_BY_NAME["session_ephemeral"]
        self.assertEqual(r.scope, "text")
        self.assertTrue(r.hits("x", "Sandbox denies ps and no fresh audit artifact exists"))
        self.assertTrue(r.hits("x", "Environment inventory: one Chrome browser, multiple monitors"))
        self.assertFalse(r.hits("x", "QQ bot delivery failures traced to expired access token"))

    def test_test_snapshot_and_routine_change(self):
        self.assertTrue(self.hit("test_snapshot", "Full test suite status after merge conflict resolution"))
        self.assertTrue(self.hit("test_snapshot", "Fixed test to simulate a batch failing mid-cycle"))
        self.assertFalse(self.hit("test_snapshot", "Mutation-resistant invariant: lease must follow the winning session"))
        self.assertTrue(self.hit("routine_code_change", "Removed stale _anchor attribute from ModelGateway.close()"))
        self.assertTrue(self.hit("routine_code_change", "Added LibtvPopupMediaTypeEnum for popup media forms"))
        self.assertTrue(self.hit("routine_code_change", "go mod tidy pruned libtv_enterprise dependency tree"))
        self.assertFalse(self.hit("routine_code_change", "Added timing provenance to bottleneck evidence"))   # 不是加个枚举那种

    def test_snapshot_diff(self):
        self.assertTrue(self.hit("snapshot_diff", "Exact undeployed delta vs release 4d6b15cb is 711 added / 21 removed"))
        self.assertTrue(self.hit("snapshot_diff", "Pending sidecar diff scopes nas-dashboard-only profile"))
        self.assertFalse(self.hit("snapshot_diff", "Gateway latency scales linearly with concurrency"))

    def test_rationale_counts_body_and_cjk(self):
        r = ot.RULES_BY_NAME["rationale"]
        self.assertGreaterEqual(r.hits("x", "root cause: the probe never checks upstream, so it must not be trusted"), 3)
        self.assertGreaterEqual(r.hits("x", "按实测修正根因：因为嵌入模型不认中文，旧结论被推翻"), 4)
        self.assertEqual(r.hits("x", "Deployed at 16da0ec; health=200; drift verdict clean"), 0)
        # 曾因太泛被拿掉的词不再算（rather than / requires / 才）
        self.assertEqual(r.hits("x", "requires a token rather than a cookie 才行"), 0)


class AdjustAndTriageTest(unittest.TestCase):
    def test_penalty_once_bonus_capped_and_type_neutralized(self):
        self.assertEqual(ot.adjustment({"status_report": 3}, TCFG), -30.0)
        self.assertEqual(ot.adjustment({"rationale": 9}, TCFG), 5.0 * TCFG["rules"]["rationale"]["cap"])
        self.assertEqual(ot.adjustment({"status_report": 1}, TCFG, type_excess_pts=65), -95.0)
        self.assertEqual(ot.adjustment({"rationale": 1}, TCFG, type_excess_pts=65), 5.0)       # 没扣分就不压类型
        # 正文讲清了为什么：标题像状态报告也不扣
        self.assertEqual(ot.adjustment({"status_report": 1, "rationale": 3}, TCFG, 65), 15.0)
        self.assertEqual(ot.type_excess({"type": "feature"}, FCFG), 65.0)
        self.assertEqual(ot.type_excess({"type": "change"}, FCFG), 0.0)

    def test_tiers(self):
        knowledge = obs("Canonical doc re-ingest preserves usage_count and last_used_at")
        self.assertEqual(ot.triage(knowledge, FCFG, TCFG).tier, "keep")
        status = obs("PR #203 and #204 already merged into main", narrative="Both pull requests are merged into main now. " * 6,
                     type_="feature")
        v = ot.triage(status, FCFG, TCFG)
        self.assertEqual((v.tier, "status_report" in v.hits), ("drop", True))
        self.assertLess(v.score, v.base_score - 90)                                 # 30 分 + feature 多出的 65 分
        # decision 受保护：扣到线下只到 review
        d = ot.triage(obs("Merge request created for enterprise-scope-v2 onto master", narrative="Created MR 683. " * 20,
                          type_="decision"), FCFG, TCFG)
        self.assertEqual(d.tier, "review")
        # bugfix 不受保护：ops_noise 的钉子只护 decision/security
        b = ot.triage(obs("Fixed test to simulate a batch failing mid-cycle", narrative="Adjusted the mock. " * 20, type_="bugfix"),
                      FCFG, TCFG)
        self.assertEqual(b.tier, "drop")

    def test_review_band_and_rescue(self):
        base_pass = mock.Mock(would_accept=True, score=66.0, threshold=60.0, layer="pass", obs_type="discovery", reasons=[])
        self.assertEqual(ot.triage(obs("x", narrative=""), FCFG, TCFG, base=base_pass).tier, "review")   # 60 ≤ 66 < 75
        base_pass.score = 76.0
        self.assertEqual(ot.triage(obs("x", narrative=""), FCFG, TCFG, base=base_pass).tier, "keep")
        rej = mock.Mock(would_accept=False, score=52.0, threshold=60.0, layer="score", obs_type="change",
                        reasons=["score 52.0 < threshold 60.0 (platform=claude)"])
        rich = obs("边向量索引卡顿说明按实测根因修正", narrative="根因是嵌入模型不认中文，因为旧结论被推翻；约定：不要再按标题查。")
        self.assertEqual(ot.triage(rich, FCFG, TCFG, base=rej).tier, "review")       # 救回只到 review
        rej.layer = "hard_gate"
        self.assertEqual(ot.triage(rich, FCFG, TCFG, base=rej).tier, "drop")         # 硬闸不救
        rej.layer, rej.score = "score", 30.0
        self.assertEqual(ot.triage(rich, FCFG, TCFG, base=rej).tier, "drop")         # 差太多不救

    def test_base_from_record(self):
        d = ot.base_from_record({"id": 7, "type": "change", "score": 51.8, "accept": False, "platform_source": "codex",
                                 "why": ["score 51.8 < threshold 60.0 (platform=codex)"]}, FCFG)
        self.assertEqual((d.would_accept, d.score, d.threshold, d.layer), (False, 51.8, 60.0, "score"))
        d = ot.base_from_record({"id": 8, "type": "discovery", "score": 0, "accept": False, "why": ["narrative too short (0 < 80)"]}, FCFG)
        self.assertEqual(d.layer, "hard_gate")
        d = ot.base_from_record({"id": 9, "type": "decision", "score": 133.2, "accept": True,
                                 "why": ["type override: decision bypasses threshold"]}, FCFG)
        self.assertEqual((d.layer, d.threshold), ("override", 60.0))
        self.assertEqual(ot.base_from_record({"id": 10, "type": "feature", "score": 84, "accept": True, "why": ["score 84 >= threshold 60.0"]},
                                             FCFG).layer, "pass")


class BenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"KG_HUB_BENCH_ROOT": str(self.root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def rows(self):
        return [
            {"population": {"total": 4}},
            dict(obs("PR #1 already merged into main", narrative="merged. " * 30, type_="feature", id=2),
                 accept=True, score=110.0, why=["score 110.0 >= threshold 60.0 (platform=claude)"]),
            dict(obs("Gateway latency scales linearly with concurrency", id=3), accept=True, score=90.0,
                 why=["score 90.0 >= threshold 60.0 (platform=claude)"]),
            dict(obs("x", narrative="", id=4), accept=False, score=52.0, why=["score 52.0 < threshold 60.0 (platform=claude)"]),
            dict(obs("Deployed at abc; health=200", narrative="ok " * 50, type_="change", id=5), accept=True, score=62.0,
                 why=["score 62.0 >= threshold 60.0 (platform=claude)"]),
            dict(obs("SLS phrase queries are tokenized loosely", narrative="", id=6), accept=False, score=0.0,
                 why=["narrative too short (0 < 80)"]),
        ]

    def labels(self):
        return [{"id": 2, "value": "L"}, {"id": 3, "value": "H"}, {"id": 4, "value": "M"}, {"id": 5, "value": "L", "topic_mismatch": True},
                {"id": 6, "value": "H"}]

    def write(self, rows, labels):
        s, l = self.root / "s.jsonl", self.root / "l.jsonl"
        s.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        l.write_text("".join(json.dumps(r) + "\n" for r in labels))
        return s, l

    def test_freeze_validates_and_score_end_to_end(self):
        s, l = self.write(self.rows(), self.labels())
        with self.assertRaises(SystemExit):                       # 标签 id 对不上
            ob.freeze(s, self.root / "missing.jsonl", "b0", self.root) if False else ob.freeze(
                s, self.write(self.rows(), self.labels()[:3])[1], "b0", self.root)
        with self.assertRaises(SystemExit):                       # 标签值非法
            ob.freeze(s, self.write(self.rows(), self.labels()[:3] + [{"id": 5, "value": "X"}])[1], "b0", self.root)
        s, l = self.write(self.rows(), self.labels())
        out = ob.freeze(s, l, "b1", self.root, "note")
        m = json.loads((out / "manifest.json").read_text())
        self.assertEqual((m["items"], m["labels"], m["topic_mismatch"]), (5, {"L": 2, "H": 2, "M": 1}, 1))
        self.assertTrue((out / "population.json").exists())
        with self.assertRaises(SystemExit):                       # 不覆盖
            ob.freeze(s, l, "b1", self.root)
        p, m, items, labels = ob.load_bench("b1")                 # 按 id 找到 KG_HUB_BENCH_ROOT 下的目录
        res = ob.score(p, m, items, labels, FCFG, TCFG, "t")
        a = res["splits"]["all"]
        self.assertEqual(a["matrix"]["L"]["drop"], 2)             # 两条状态记录都丢
        self.assertEqual(a["matrix"]["H"]["keep"], 1)
        # 硬闸拒的 H（id 6）分诊也救不了：误杀 1/2，与基线相同；低价值两条都丢而基线一条没丢
        self.assertEqual((a["h_kill_pct"], a["h_kill_pct_baseline"], a["l_drop_pct"], a["l_drop_pct_baseline"]), (50.0, 50.0, 100.0, 0.0))
        self.assertEqual((a["net_drop_pct"], a["net_drop_pct_baseline"]), (80.0, 40.0))   # 基线拒了 52 分那条和硬闸那条
        self.assertEqual([x["id"] for x in res["h_dropped"]], [6])
        self.assertEqual(res["rules"]["status_report"]["L_ids"], [2, 5])
        self.assertEqual((res["splits"]["dev"]["n"], res["splits"]["holdout"]["n"]), (3, 2))   # 偶数 2、4、6 / 奇数 3、5
        self.assertEqual((ob.split_of(2), ob.split_of(3)), ("dev", "holdout"))
        self.assertIn("status_report", ob.render(res))
        # 命令行：写运行记录
        with mock.patch("sys.stdout"):
            ob.main(["score", "--bench", "b1", "--label", "x"])
        runs = list((p / "runs").glob("*-x.json"))
        self.assertEqual(len(runs), 1)
        self.assertEqual(json.loads(runs[0].read_text())["rules_version"], ot.rules_version(TCFG))

    def test_sheet_has_no_scores_and_stratified_respects_quota(self):
        rows = self.rows()
        sheet = ob.sheet_rows(rows)
        self.assertEqual(len(sheet), 5)
        self.assertEqual(set(sheet[0]), set(ob.SHEET_FIELDS))
        many = [dict(obs("t", id=i), project="big" if i < 90 else "small", memory_session_id=f"s{i % 10}") for i in range(100)]
        picked = ob.stratified(many, 20, 2)
        self.assertLessEqual(len(picked), 20)
        self.assertTrue(any(r["project"] == "small" for r in picked))            # 小项目至少 1 条
        per = {}
        for r in picked:
            per[(r["project"], r["memory_session_id"])] = per.get((r["project"], r["memory_session_id"]), 0) + 1
        self.assertLessEqual(max(per.values()), 2)

    def test_annotate_and_population(self):
        rows = [obs("a", id=1, memory_session_id="s", created_at="2026-10-01T01:00:00Z"),
                obs("b", id=2, memory_session_id="s", created_at="2026-10-01T02:00:00Z", narrative="")]
        ob.annotate(rows, FCFG)
        self.assertEqual([r["sess_rank"] for r in rows], [1, 2])
        self.assertEqual((rows[0]["accept"], rows[1]["accept"]), (True, False))
        self.assertEqual(ob.population(rows)["session_days"], 1)


class SelectBatchTriageTest(unittest.TestCase):
    ROWS = [obs("PR #1 already merged into main", narrative="merged. " * 30, type_="feature", id=1, content_hash="h1"),
            obs("Gateway latency scales linearly with concurrency", id=2, content_hash="h2"),
            obs("x", narrative="", id=3, content_hash="h3"),
            obs("Deployed at abc; health=200", narrative="ok " * 300, type_="change", id=4, content_hash="h4")]

    def test_shadow_counts_but_keeps(self):
        shadow = dict(TCFG, enabled=False)
        picked, st = ax.select_batch(self.ROWS, FCFG, 10, {}, triage_cfg=shadow)
        self.assertEqual([p["id"] for p in picked], [1, 2, 4])
        self.assertEqual((st["rejected_by_filter"], st["triage_drop"], st["triage_dropped"]), (1, 2, 0))

    def test_enforce_skips_drop_tier(self):
        picked, st = ax.select_batch(self.ROWS, FCFG, 10, {}, triage_cfg=dict(TCFG, enabled=True))
        self.assertEqual([p["id"] for p in picked], [2])
        self.assertEqual((st["triage_drop"], st["triage_dropped"], st["picked"]), (2, 2, 1))

    def test_no_config_means_no_triage(self):
        picked, st = ax.select_batch(self.ROWS, FCFG, 10, {}, triage_cfg={})
        self.assertEqual(len(picked), 3)
        self.assertEqual(st["triage_drop"], 0)
        with mock.patch.object(ax.obs_triage, "load_triage_config", side_effect=FileNotFoundError):
            self.assertEqual(ax._triage_cfg(), {})

    def test_shipped_config_is_shadow(self):
        self.assertFalse(ot.triage_enabled(TCFG))


if __name__ == "__main__":
    unittest.main()
