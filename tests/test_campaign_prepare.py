"""campaign prepare：一条命令串起 lint → 命名同步 → 质量表 + 引文候选 → quality-check → 至多一次整批 dry-run。

取观测、dry-run、正式提交都用假函数，不碰 NAS 与服务端；规则用仓库里的 agent-v5 正文（要质量表）。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import contextlib
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from tools import agent_extract as ax
from tools import campaign_namesync as ns
from tools import campaign_prepare as cp
from tools import extract_campaign as ec
from utils import extraction_quality as quality
from utils import quality_candidates as qc


def title(i):
    return f"Worker {i} retries only before provider dispatch"


def body(i):
    return (f"[DISCOVERY] {title(i)}\n"
            f"The worker_{i} module limits pre-provider retries to 3 attempts and then stops.\n"
            f"Dispatch failures after the provider accepted the request require reconciliation through reconcile_queue_{i}.\n"
            "This worker lives in the kg-hub repository.\n"
            "Project: some-recording-project")


def fake_select(rows, cfg, n, existing):
    picked = [{"id": r["id"], "name": f"claude-mem-obs-{r['id']}", "source_description": "sd",
               "source_obs_id": f"h{r['id']}", "reference_time": "2026-10-22T00:00:00Z",
               "episode_body": body(r["id"])} for r in rows][:n]
    return picked, {"seen": len(rows), "picked": len(picked)}


def fake_fetch(after, limit):
    rows = [{"id": i, "title": title(i)} for i in range(after + 1, min(after + 1 + limit, 60))]
    return {"rows": rows, "boundary": 0, "after": after, "skipped_by_watermark": 0, "exhausted": not rows}


def result_item(i, empty=False):
    if empty:
        return {"id": i, "entities": [], "edges": []}
    w = f"worker_{i}"
    return {"id": i,
            "entities": [{"name": w, "type": "Tool", "summary": f"The {w} module limits pre-provider retries to 3 attempts."},
                         {"name": "kg-hub", "type": "Project", "summary": "kg-hub repository that hosts the extraction worker."},
                         {"name": f"reconcile_queue_{i}", "type": "Tool",
                          "summary": "Queue used to reconcile dispatch failures after provider acceptance."}],
            "edges": [{"source": w, "name": "relates_to", "target": f"reconcile_queue_{i}", "fact": title(i)},
                      {"source": w, "name": "relates_to", "target": f"reconcile_queue_{i}",
                       "fact": f"工作线程 {i} 只在调用供应商之前重试"},
                      {"source": w, "name": "depends_on", "target": f"reconcile_queue_{i}",
                       "fact": f"{w} needs reconcile_queue_{i} to reconcile dispatch failures after the provider accepted the request"},
                      {"source": w, "name": "belongs_to", "target": "kg-hub",
                       "fact": f"{w} lives in the kg-hub repository"}]}


def PASS(model):
    return {"model": model, "verdict": "pass", "prompt_version": ax.PROMPT_VERSION + "+bench1", "bench": "b",
            "decided_at": "t", "prompt_sha256": ec.prompt_sha(ax.PROMPT_PATH.read_text())}


def confirm(sheet, batch, reviewer="independent-test-reviewer"):
    """测试里的审阅者：逐条下结论、确认候选（删 candidate 键），空引文用本条正文补。"""
    sheet = copy.deepcopy(sheet)
    sheet["reviewer"] = reviewer
    bodies = {i["id"]: quality.body_evidence(i) for i in batch["items"]}
    for row in sheet["items"]:
        row.update(verdict="ok", translation_complete=True, facts_complete=True, core_covered=True,
                   ownership_checked=True)
        for ev in row["edges"] + row["summaries"] + ([row["empty"]] if row["empty"] else []):
            ev.pop("candidate", None)
            if not ev.get("quote"):
                ev["quote"] = bodies[row["id"]]
        if row["empty"]:
            row["empty"].update(reason="no_stable_knowledge", no_new_information=True)
    return sheet


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": self.tmp.name,
                                                "KG_HUB_CLAIMS_DIR": os.path.join(self.tmp.name, "claims"),
                                                "KG_HUB_NAS_GATE_DIR": os.path.join(self.tmp.name, "nas-gate"),
                                                "CLAUDE_MEM_INTERNAL": "1",
                                                "CLAUDE_MEM_SETTINGS_PATH": os.path.join(self.tmp.name, "cm.json")})
        self.env.start()
        self.patches = [mock.patch.object(ax, "select_batch", fake_select),
                        mock.patch.object(ax, "load_config", lambda: {}),
                        mock.patch("utils.semantic_regression.validate_admission", return_value={}),
                        mock.patch.object(ec, "model_readiness", return_value={})]
        for p in self.patches:
            p.start()
        self.assertTrue(quality.requires_quality(ax.PROMPT_VERSION))      # 用仓库里的 v5 正文：要质量表
        ec.cmd_start("p1", ["M"], executor="claude-code", quota=20, batch=4, after_id=0, lookup=PASS)
        o = ec.cmd_claim("p1", "M", url="http://x", token="t", fetch=fake_fetch, check=lambda idents: {})
        self.bid, self.d = o["batch"], ec.campaign_dir("p1")
        self.batch = json.loads(_Path(o["batch_file"]).read_text())
        ids = [i["id"] for i in self.batch["items"]]
        self.ids = ids
        self.results_file = _Path(o["results_file"])
        self.write_results([result_item(i, empty=(n == len(ids) - 1)) for n, i in enumerate(ids)])
        self.layout = ns.Layout(workai=os.path.join(self.tmp.name, "work-ai"), home=self.tmp.name)
        self.gated, self.posts = [], []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def write_results(self, items):
        self.results_file.write_text(json.dumps({"items": items}, ensure_ascii=False))

    def gate(self, kind):
        self.gated.append(kind)
        return contextlib.nullcontext()

    def post(self, method, url, body, token):
        self.posts.append(body)
        if body.get("dry_run"):
            i = int(body["source_obs_id"][1:])
            ents = [{"name": e["name"], "type": e["type"], "reused": e["name"] == "kg-hub",
                     **({"matched_name": "kg-hub-legacy", "matched_type": "Project", "match": "lexical",
                         "matched_summary": "old node"} if e["name"] == "kg-hub" else {})}
                    for e in body["entities"]]
            return 200, {"status": "dry_run", "entities": ents,
                         "resolved": {"kg-hub": {"reused": True, "matched_name": "kg-hub-legacy"}},
                         "new_entities": [{"name": n} for n in (f"worker_{i}", f"reconcile_queue_{i}")]}
        return 200, {"status": "empty_recorded"} if body.get("empty") else {"status": "ok", "episode_uuid": "u"}

    def test_http502_without_error_code_cannot_report_ready(self):
        self.prepare();self.review_sheet()
        original=self.post
        def failed(method,url,body,token):
            if body.get('dry_run') and body['source_obs_id']==f'h{self.ids[0]}':
                return 502,{'detail':'temporary gateway failure'}
            return original(method,url,body,token)
        result=cp.cmd_prepare('p1',self.bid,url='http://x',token='t',post=failed,gate=self.gate,layout=self.layout)
        report,code=result
        self.assertEqual(report['stage'],'dryrun');self.assertFalse(report['ready'])
        self.assertEqual(report['dryrun']['by_status']['http_502'],1)
        self.assertNotEqual(code,0)

    def prepare(self, **kw):
        return cp.cmd_prepare("p1", self.bid, url="http://x", token="t", post=self.post, gate=self.gate,
                              layout=self.layout, **kw)

    def sheet(self):
        return json.loads(cp.sheet_path(self.d, self.bid).read_text())

    def review_sheet(self, **kw):
        p = cp.sheet_path(self.d, self.bid)
        p.write_text(json.dumps(confirm(self.sheet(), self.batch, **kw), ensure_ascii=False))

    def dry_posts(self):
        return [b for b in self.posts if b.get("dry_run")]

    def test_full_flow_unfinished_sheet_then_reviewed_sheet_runs_one_dryrun(self):
        rep, code = self.prepare()
        self.assertEqual((code, rep["stage"], rep["lint"]), (cp.EXIT_ACTION, "quality_sheet", []))
        st = rep["quality_sheet"]["status"]
        self.assertTrue(rep["quality_sheet"]["written"])
        self.assertEqual(st["pending_rows"], self.ids)                       # 没有一行被预填成通过
        self.assertGreater(st["unconfirmed_candidates"], 0)
        self.assertFalse(st["complete"])
        self.assertEqual((self.gated, self.posts), ([], []))                 # 表没填完不 dry-run
        sheet = self.sheet()
        for row in sheet["items"]:
            self.assertEqual(row["verdict"], "pending")
            for flag in ("translation_complete", "facts_complete", "core_covered", "ownership_checked"):
                self.assertIs(row[flag], False)
        self.assertIn("审阅质量表", rep["next"][0])
        # 第二次跑：表没动就不覆盖，也不 dry-run
        rep, code = self.prepare()
        self.assertEqual((code, rep["quality_sheet"]["written"]), (cp.EXIT_ACTION, False))
        # 审阅者确认后：quality-check → 恰好一次整批 dry-run（经闸门）→ 报告误并候选
        self.review_sheet()
        rep, code = self.prepare()
        self.assertEqual((code, rep["stage"]), (cp.EXIT_READY, "ready"), rep.get("next"))
        self.assertTrue(rep["quality_check"]["ok"])
        self.assertEqual(rep["dryrun"]["cached"], False)
        self.assertEqual(self.gated, ["dryrun"])
        nonempty = len(self.ids) - 1
        self.assertEqual(len(self.dry_posts()), nonempty)                   # 每条非空结果 POST 一次，整批只跑一遍
        self.assertEqual(rep["dryrun"]["by_status"], {"dry_run": nonempty, "empty": 1})
        self.assertTrue(any("kg-hub-legacy" in m for m in rep["merge_candidates"]["reuse_mismatch"]))
        self.assertTrue(any("kg-hub-legacy" in m for m in rep["merge_candidates"]["reused"]))
        self.assertEqual((rep["post_lint"], rep["splits"]), ([], []))
        self.assertIn(f"submit --id p1 --batch {self.bid}", rep["next"][-1])
        # 输入没变：复用预览，不再排闸门、不再 POST
        rep, code = self.prepare()
        self.assertEqual((code, rep["dryrun"]["cached"]), (cp.EXIT_READY, True))
        self.assertEqual((self.gated, len(self.dry_posts())), (["dryrun"], nonempty))
        # 正式提交沿用同一预览（门禁不变：dryrun_missing / split_guard 照查）
        out = ec.cmd_submit("p1", self.bid, dry_run=False, url="http://x", token="t", post=self.post,
                            gate=self.gate, layout=self.layout)
        self.assertEqual(out["by_status"], {"ok": nonempty, "empty": 1})
        self.assertEqual(out["preview"]["reused"], True)
        self.assertEqual(self.gated, ["dryrun", "submit"])
        with self.assertRaises(SystemExit):
            self.prepare()                                                   # 已提交的批次不再准备

    def test_submit_reports_no_reuse_without_matching_preview(self):
        self.prepare()
        self.review_sheet()
        self.assertEqual(self.prepare()[1], cp.EXIT_READY)
        ec.preview_path(self.d, self.bid).unlink()
        out = ec.cmd_submit("p1", self.bid, dry_run=False, url="http://x", token="t", post=self.post,
                            gate=self.gate, layout=self.layout)
        self.assertEqual(out["preview"], {"reused": False, "at": None})

    def test_dryrun_errors_are_reported_and_never_cached(self):
        self.prepare()
        self.review_sheet()
        broken = f"h{self.ids[1]}"

        def post(method, url, body, token):
            if body.get("dry_run") and body["source_obs_id"] == broken:
                self.posts.append(body)
                return 500, {"code": "boom"}
            return self.post(method, url, body, token)
        rep, code = cp.cmd_prepare("p1", self.bid, url="http://x", token="t", post=post, gate=self.gate,
                                   layout=self.layout)
        self.assertEqual((code, rep["stage"]), (cp.EXIT_ACTION, "dryrun"))
        self.assertEqual([e["id"] for e in rep["dryrun"]["errors"]], [self.ids[1]])
        self.assertFalse(ec.preview_path(self.d, self.bid).exists())
        rep, code = self.prepare()                                           # 修好后重跑：重新 dry-run，不吃坏缓存
        self.assertEqual((code, rep["dryrun"]["cached"]), (cp.EXIT_READY, False))
        self.assertEqual(self.gated, ["dryrun", "dryrun"])

    def test_cache_invalidated_when_results_review_or_marker_change(self):
        self.prepare()
        self.review_sheet()
        self.assertEqual(self.prepare()[1], cp.EXIT_READY)
        self.assertEqual(self.gated, ["dryrun"])
        # 1. 质量表换了审阅人（审过的字节变了）：摘要变 → 重新 dry-run
        self.review_sheet(reviewer="second-reviewer")
        rep, code = self.prepare()
        self.assertEqual((code, rep["dryrun"]["cached"]), (cp.EXIT_READY, False))
        self.assertEqual(self.gated, ["dryrun", "dryrun"])
        # 2. 摘要没变但 dry-run 标记被删（绑定条件不满足）：不复用，重新 dry-run
        ax.dryrun_marker_path(self.d, self.bid).unlink()
        rep, code = self.prepare()
        self.assertEqual((code, rep["dryrun"]["cached"]), (cp.EXIT_READY, False))
        self.assertEqual(self.gated, ["dryrun"] * 3)
        # 3. 结果改了一条：质量表过期，重新生成；沿用旧表审阅者确认过的引文作候选；不 dry-run
        old_sheet = self.sheet()
        items = json.loads(self.results_file.read_text())["items"]
        items[0]["edges"][2]["fact"] = f"worker_{self.ids[0]} stops after 3 attempts and then stops retrying"
        self.write_results(items)
        rep, code = self.prepare()
        self.assertEqual((code, rep["stage"]), (cp.EXIT_ACTION, "quality_sheet"))
        self.assertTrue(rep["quality_sheet"]["written"])
        self.assertTrue(_Path(rep["quality_sheet"]["previous"]).exists())
        self.assertGreater(rep["quality_sheet"]["candidates"]["from_previous"], 0)
        new_sheet = self.sheet()
        self.assertEqual(new_sheet["reviewer"], "")
        carried = [ev for row in new_sheet["items"] for ev in row["edges"] if ev.get("candidate") == qc.PREVIOUS]
        old_quotes = {ev["quote"] for row in old_sheet["items"] for ev in row["edges"]}
        self.assertTrue(carried and all(ev["quote"] in old_quotes for ev in carried))
        self.assertTrue(all(r["verdict"] == "pending" for r in new_sheet["items"]))
        self.assertEqual(self.gated, ["dryrun"] * 3)
        # 旧表留下的 quality-check 副本也挡不住：正式提交要求当前预览
        with self.assertRaises(SystemExit):
            ec.cmd_submit("p1", self.bid, dry_run=False, url="http://x", token="t", post=self.post,
                          gate=self.gate, layout=self.layout)
        self.review_sheet()
        rep, code = self.prepare()
        self.assertEqual((code, rep["dryrun"]["cached"]), (cp.EXIT_READY, False))
        self.assertEqual(self.gated, ["dryrun"] * 4)

    def test_lint_failure_stops_before_sheet_and_dryrun(self):
        items = json.loads(self.results_file.read_text())["items"]
        items[0]["edges"][0]["fact"] = "not the verbatim title"
        self.write_results(items)
        rep, code = self.prepare()
        self.assertEqual((code, rep["stage"]), (cp.EXIT_ACTION, "lint"))
        self.assertTrue(rep["lint"])
        self.assertNotIn("quality_sheet", rep)
        self.assertFalse(cp.sheet_path(self.d, self.bid).exists())
        self.assertEqual((self.gated, self.posts), ([], []))

    def test_namesync_applies_only_deterministic_renames_then_relints(self):
        old = self.d.parent / "old"
        for sub in ("batches", "results"):
            (old / sub).mkdir(parents=True)
        (old / "campaign.json").write_text(json.dumps({"tool": "extract_campaign", "id": "old", "batches": [
            {"id": "o-001", "status": "submitted", "submitted_at": "2026-10-24T00:00:00+00:00"}]}))
        (old / "batches" / "o-001.json").write_text(json.dumps({"items": [{"id": 1, "episode_body": ""}]}))
        (old / "results" / "o-001.json").write_text(json.dumps({"items": [{"id": 1, "entities": [
            {"name": "reconcile_due（QueueDispatcher）", "type": "Concept", "summary": "s"}], "edges": []}]},
            ensure_ascii=False))
        items = json.loads(self.results_file.read_text())["items"]
        items[0]["entities"].append({"name": "reconcile_due", "type": "Concept",
                                     "summary": "QueueDispatcher 里按到期时间对账的入口函数，每个调度周期都会调用一次。"})
        items[0]["edges"].append({"source": "reconcile_due", "name": "depends_on", "target": "kg-hub",
                                  "fact": "reconcile_due depends on kg-hub because the dispatcher checks it on every tick"})
        self.write_results(items)
        rep, code = self.prepare()
        self.assertEqual([(a["from"], a["to"]) for a in rep["namesync"]["applied"]],
                         [("reconcile_due", "reconcile_due（QueueDispatcher）")])
        self.assertEqual(rep["namesync"]["residual"], [])
        names = [e["name"] for e in json.loads(self.results_file.read_text())["items"][0]["entities"]]
        self.assertIn("reconcile_due（QueueDispatcher）", names)
        self.assertEqual(rep["stage"], "quality_sheet")                      # 改名后的结果才进质量表
        sheet_names = [s["name"] for s in self.sheet()["items"][0]["summaries"]]
        self.assertIn("reconcile_due（QueueDispatcher）", sheet_names)

    def test_error_verdict_and_leftover_candidates_block_quality_check(self):
        self.prepare()
        sheet = confirm(self.sheet(), self.batch)
        sheet["items"][0]["edges"][0]["candidate"] = True                    # 审阅者漏确认一处候选
        cp.sheet_path(self.d, self.bid).write_text(json.dumps(sheet, ensure_ascii=False))
        with mock.patch.object(ec, "cmd_quality_check") as check:
            rep, code = self.prepare()
            check.assert_not_called()
        self.assertEqual((code, rep["stage"], rep["quality_sheet"]["status"]["unconfirmed_candidates"]),
                         (cp.EXIT_ACTION, "quality_sheet", 1))
        sheet["items"][0]["edges"][0].pop("candidate")
        sheet["items"][1]["verdict"] = "error"
        cp.sheet_path(self.d, self.bid).write_text(json.dumps(sheet, ensure_ascii=False))
        rep, code = self.prepare()
        self.assertEqual((code, rep["quality_sheet"]["status"]["error_rows"]), (cp.EXIT_ACTION, [self.ids[1]]))
        self.assertEqual(self.gated, [])

    def test_quality_check_failure_is_reported_without_dryrun(self):
        self.prepare()
        sheet = confirm(self.sheet(), self.batch)
        sheet["items"][0]["edges"][2]["quote"] = "A fabricated quote that never appeared in the source."
        cp.sheet_path(self.d, self.bid).write_text(json.dumps(sheet, ensure_ascii=False))
        rep, code = self.prepare()
        self.assertEqual((code, rep["stage"], rep["quality_check"]["ok"]), (cp.EXIT_ACTION, "quality_check", False))
        self.assertTrue(any("source_quote_required" in e for e in rep["quality_check"]["errors"]))
        self.assertEqual(self.gated, [])

    def test_cli_wires_gate_and_returns_gate_refusal_with_partial_report(self):
        from tools import nas_gate
        self.prepare()
        self.review_sheet()

        def refuse(kind):
            raise nas_gate.GateRefused(nas_gate.EXIT_NO_TEMP, "读不到温度")
        printed = []
        with mock.patch.object(ns.Layout, "from_disk", classmethod(lambda cls, *a, **k: self.layout)), \
                mock.patch.object(ax, "api_token", lambda *a, **k: "t"), \
                mock.patch.object(ax, "_http", self.post), \
                mock.patch("builtins.print", lambda *a, **k: printed.append(a[0] if a else "")):
            with mock.patch("tools.nas_gate.hold", refuse):
                self.assertEqual(ec.main(["prepare", "--id", "p1", "--batch", self.bid, "--no-graph"]),
                                 nas_gate.EXIT_NO_TEMP)
            rep = json.loads(printed[-1])
            self.assertEqual((rep["stage"], rep["dryrun"]["refused"]), ("dryrun", nas_gate.EXIT_NO_TEMP))
            self.assertTrue(rep["quality_check"]["ok"])
            with mock.patch("tools.nas_gate.hold", self.gate):
                self.assertEqual(ec.main(["prepare", "--id", "p1", "--batch", self.bid, "--no-graph"]), 0)
                self.assertEqual(ec.main(["prepare", "--id", "p1", "--batch", self.bid, "--no-graph"]), 0)
                self.assertEqual(ec.main(["prepare", "--id", "p1", "--batch", self.bid, "--no-graph",
                                          "--fresh-dryrun"]), 0)
        self.assertEqual(self.gated, ["dryrun", "dryrun"])                    # 第二次复用，--fresh-dryrun 才重跑


class CandidateTest(unittest.TestCase):
    """引文候选：都是冻结输入里的真实子串；从不预填通过；不改变 validate_review 的判定。"""

    def fixture(self):
        batch = {"batch_id": "b", "prompt_version": "agent-v5",
                 "items": [{"id": 1, "episode_body": body(1)},
                           {"id": 2, "episode_body": "[DISCOVERY] Second note about queues\n"
                                                     "The shared_scheduler_x9 drains every queue each 30 seconds."}],
                 "lookahead": [{"id": 9, "title": "Later: provider_gateway_v2 replaced the dispatcher entirely"}]}
        r1 = result_item(1)
        r1["edges"] += [{"source": "worker_1", "name": "relates_to", "target": "kg-hub",
                         "fact": "shared_scheduler_x9 drains every queue each 30 seconds"},
                        {"source": "worker_1", "name": "relates_to", "target": "kg-hub",
                         "fact": "provider_gateway_v2 replaced the dispatcher entirely"},
                        {"source": "worker_1", "name": "relates_to", "target": "kg-hub",
                         "fact": "Completely unrelated claim with zero overlap whatsoever"},
                        {"source": "worker_1", "name": "belongs_to", "target": "credvault",
                         "fact": "worker_1 module limits retries and belongs to credvault"}]
        results = {"items": [r1, result_item(2, empty=True)]}
        return batch, results

    def test_candidates_are_real_substrings_and_never_approve(self):
        batch, results = self.fixture()
        identity = quality.extraction_identity("m", "claude-code", "r")
        sheet = quality.review_sheet(batch, results, "agent-v5 rules", identity)
        template = copy.deepcopy(sheet)
        stats = qc.add_candidates(sheet, batch, results)
        sources = {str(i["id"]): i for i in batch["items"]}
        future = quality.lookahead_evidence(batch)
        row, empty_row = sheet["items"]
        evs = {ev["index"]: ev for ev in row["edges"]}
        checks = [(ev, quality.evidence_text(ev, batch["items"][0], sources, future)) for ev in row["edges"]]
        checks += [(ev, quality.evidence_text(ev, batch["items"][0], sources, future) if "source" in ev
                    else quality.body_evidence(batch["items"][0]) + "\n" + future) for ev in row["summaries"]]
        checks.append((empty_row["empty"], quality.body_evidence(batch["items"][1])))
        for ev, text in checks:
            if "candidate" in ev:
                self.assertIn(ev["quote"], text)
                self.assertTrue(quality._quote_ok(ev["quote"], text))
        self.assertEqual(empty_row["empty"]["quote"], "Second note about queues")
        self.assertEqual(evs[0]["quote"], title(1))                               # 照抄标题 → 标题行
        self.assertEqual(evs[1]["quote"], title(1))                               # 标题译文 → 标题行
        self.assertIn("kg-hub", evs[3]["quote"])                                  # 归属：本条正文、含目标名
        self.assertEqual(evs[3]["source"], "body")
        self.assertEqual((evs[4]["source"], evs[4].get("source_id")), ("batch_item", 2))
        self.assertEqual(evs[5]["source"], "lookahead")
        self.assertNotIn("candidate", evs[6])                                     # 无重合：不硬凑
        self.assertEqual(evs[6]["quote"], "")
        self.assertNotIn("candidate", evs[7])                                     # 归属目标不在正文：不给
        self.assertEqual(stats["edges"], 8)
        self.assertEqual(stats["edge_candidates"], 6)
        self.assertEqual(stats["summary_candidates"], 3)
        # 结论与核对标志、判空理由与模板一字不差
        for new, old in zip(sheet["items"], template["items"]):
            for k in ("verdict", "translation_complete", "facts_complete", "core_covered", "ownership_checked",
                      "context_digest", "id"):
                self.assertEqual(new[k], old[k])
        self.assertEqual((empty_row["empty"]["reason"], empty_row["empty"]["no_new_information"]), ("", False))
        self.assertEqual(sheet["reviewer"], "")
        # 候选不改变 validate_review 的判定：带 candidate 键与删掉后错误清单一致
        stripped = copy.deepcopy(sheet)
        for r in stripped["items"]:
            for ev in r["edges"] + r["summaries"] + ([r["empty"]] if r["empty"] else []):
                ev.pop("candidate", None)
        prompt = "agent-v5 rules"
        self.assertEqual(quality.validate_review(batch, results, sheet, prompt),
                         quality.validate_review(batch, results, stripped, prompt))
        self.assertTrue(quality.validate_review(batch, results, sheet, prompt))
        self.assertFalse(qc.sheet_status(sheet)["complete"])

    def test_previous_quotes_are_reused_only_when_still_real(self):
        batch, results = self.fixture()
        identity = quality.extraction_identity("m", "claude-code", "r")
        prev = quality.review_sheet(batch, results, "rules", identity)
        prev["items"][0]["edges"][2].update(quote="The worker_1 module limits pre-provider retries")
        prev["items"][0]["edges"][6].update(quote="This sentence was fabricated by a careless reviewer.")
        prev["items"][0]["edges"][4].update(quote="Another fabricated sentence that the source never said.")
        sheet = quality.review_sheet(batch, results, "rules", identity)
        qc.add_candidates(sheet, batch, results, previous=prev)
        evs = sheet["items"][0]["edges"]
        self.assertEqual((evs[2]["quote"], evs[2]["candidate"]),
                         ("The worker_1 module limits pre-provider retries", qc.PREVIOUS))
        self.assertNotIn("fabricated", evs[6]["quote"])
        self.assertEqual((evs[4]["candidate"], evs[4]["source"], evs[4]["source_id"]), (qc.MATCH, "batch_item", 2))
        self.assertNotIn("candidate", evs[6])

    def test_short_title_is_not_offered_as_quote(self):
        batch = {"items": [{"id": 1, "episode_body": "[DISCOVERY] Fix A\nFix A patches the retry loop."}], "lookahead": []}
        results = {"items": [{"id": 1, "entities": [{"name": "Fix A", "type": "Fix"}],
                              "edges": [{"source": "Fix A", "name": "relates_to", "target": "Fix A", "fact": "Fix A"}]}]}
        sheet = quality.review_sheet(batch, results, "rules", quality.extraction_identity("m", "t", "r"))
        qc.add_candidates(sheet, batch, results)
        self.assertNotIn("candidate", sheet["items"][0]["edges"][0])         # 不足 8 字、又不是整段正文：_quote_ok 不认

    def test_segments_are_substrings(self):
        text = "第一句。第二句！Third one. Fourth? fifth;\nline two"
        for seg in qc.segments(text):
            self.assertIn(seg, text)


if __name__ == "__main__":
    unittest.main()
