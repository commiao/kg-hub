"""tools/extract_campaign：多模型并发领批不撞批、配额、约定版本、自检→dry-run→入库的门、复盘表。

取观测与提交都用假函数，不碰 NAS 与服务端。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from tools import agent_extract as ax
from tools import extract_campaign as ec

TITLES = {i: f"观测标题第{i}条" for i in range(1, 400)}


def fake_fetch(after, limit):
    rows = [{"id": i, "title": TITLES[i]} for i in range(after + 1, min(after + 1 + limit, 400))]
    return {"rows": rows, "boundary": 0, "after": after, "skipped_by_watermark": 0, "exhausted": not rows}


def fake_select(rows, cfg, n, existing):
    picked = [{"id": r["id"], "name": f"claude-mem-obs-{r['id']}", "source_description": "sd",
               "source_obs_id": f"h{r['id']}", "reference_time": "2026-10-22T00:00:00Z",
               "episode_body": f"[DISCOVERY] {TITLES[r['id']]}\nbody"} for r in rows if r["id"] % 7][:n]
    return picked, {"seen": len(rows), "picked": len(picked)}


def good_item(i):
    return {"id": i, "entities": [{"name": "A", "type": "Concept", "summary": "s"}, {"name": "B", "type": "Tool"}],
            "edges": [{"source": "A", "name": "relates_to", "target": "B", "fact": TITLES[i]},
                      {"source": "A", "name": "relates_to", "target": "B", "fact": f"Observation title number {i}"},
                      {"source": "A", "name": "depends_on", "target": "B",
                       "fact": "A depends on B because the observation says the canary path needs it to stay up"}]}


def PASS(model):
    return {"verdict": "pass", "prompt_version": ax.PROMPT_VERSION + "+bench1", "bench": "b", "decided_at": "t",
            "prompt_sha256": ec.prompt_sha(ax.PROMPT_PATH.read_text())}


class CampaignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": self.tmp.name})
        self.env.start()
        self.patches = [mock.patch.object(ax, "select_batch", fake_select),
                        mock.patch.object(ax, "load_config", lambda: {})]
        for p in self.patches:
            p.start()
        ec.cmd_start("t1", ["DeepSeek-Flash", "Qwen3.8-Max"], executor="qoder", quota=60, batch=25, after_id=0, lookup=PASS)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def claim(self, model):
        return ec.cmd_claim("t1", model, url="http://x", token=None, fetch=fake_fetch, check=lambda idents: {})

    def test_start_validates_and_defaults_cursor_to_agent_extract_state(self):
        with self.assertRaises(SystemExit):
            ec.cmd_start("t1", ["X"], executor="qoder", quota=1, batch=1, after_id=0, lookup=PASS)        # 已存在
        with self.assertRaises(SystemExit):
            ec.cmd_start("t2", ["a b"], executor="qoder", quota=1, batch=1, after_id=0, lookup=PASS)      # 来源标记非法
        with self.assertRaises(SystemExit):
            ec.cmd_start("t3", ["M", "M"], executor="qoder", quota=1, batch=1, after_id=0, lookup=PASS)   # 重复
        (_Path(self.tmp.name) / "state.json").write_text(json.dumps({"cursor": 123}))
        c = ec.cmd_start("t4", ["M"], executor="codex", quota=1, batch=1, after_id=None, lookup=PASS)
        self.assertEqual((c["cursor"], c["models"]["M"]["run_id"]), (123, "t4-M"))

    def test_concurrent_claims_never_overlap_and_respect_quota(self):
        with ThreadPoolExecutor(max_workers=6) as ex:
            outs = list(ex.map(self.claim, ["DeepSeek-Flash", "Qwen3.8-Max"] * 4))
        got = [o for o in outs if not o["done"]]
        ids = [i for o in got for i in json.loads(_Path(o["batch_file"]).read_text())["items"]]
        all_ids = [x["id"] for x in ids]
        self.assertEqual(len(all_ids), len(set(all_ids)))                 # 没有一条被两个批次领到
        st = ec.cmd_status("t1")
        self.assertEqual({m: v["claimed"] for m, v in st["models"].items()}, {"DeepSeek-Flash": 60, "Qwen3.8-Max": 60})
        self.assertTrue(any(o["done"] for o in outs))                    # 第 4 次领：配额已满
        self.assertEqual(sorted(len(b["ids"]) for b in ec.load(ec.campaign_dir("t1"))["batches"]
                                if b["model"] == "Qwen3.8-Max"), [10, 25, 25])   # 最后一批只领剩余配额

    def test_claim_writes_batch_with_lookahead_chain_and_next_commands(self):
        o = self.claim("DeepSeek-Flash")
        b = json.loads(_Path(o["batch_file"]).read_text())
        self.assertEqual((o["batch"], o["run_id"], o["chain"], len(b["items"])),
                         ("DeepSeek-Flash-001", "t1-DeepSeek-Flash", "c0", 25))
        self.assertTrue(b["lookahead"] and b["lookahead"][0]["id"] > b["items"][-1]["id"])
        self.assertIn("submit --id t1 --batch DeepSeek-Flash-001 --dry-run", " ".join(o["next"]))
        with self.assertRaises(SystemExit):
            self.claim("没登记的模型")

    def test_chain_versions_apply_to_later_claims_and_rules(self):
        d = ec.campaign_dir("t1")
        self.assertNotIn("战役补充", ec.rules_text(d, ec.load(d)))
        f = _Path(self.tmp.name) / "c.md"
        f.write_text("## 战役补充约定 v1\n- 少用 relates_to")
        self.assertEqual(ec.cmd_chain("t1", str(f))["chain"], "c1")
        text = ec.rules_text(d, ec.load(d))
        self.assertTrue(text.startswith("# kg-hub 预抽取") and text.rstrip().endswith("少用 relates_to"))
        self.assertEqual(self.claim("DeepSeek-Flash")["chain"], "c1")

    def test_lint_then_dry_run_gate_then_submit_and_report(self):
        o = self.claim("DeepSeek-Flash")
        bid, items = o["batch"], json.loads(_Path(o["batch_file"]).read_text())["items"]
        rp = _Path(o["results_file"])
        bad = [good_item(x["id"]) for x in items]
        bad[0]["edges"][0]["fact"] = "改写了标题"
        rp.write_text(json.dumps({"items": bad}, ensure_ascii=False))
        with self.assertRaises(SystemExit):                            # 自检不过不许提交（dry-run 也不行）
            ec.cmd_submit("t1", bid, dry_run=True, url="http://x", token=None, post=lambda *a: (200, {}))
        rp.write_text(json.dumps({"items": [good_item(x["id"]) for x in items]}, ensure_ascii=False))
        self.assertEqual(ec.cmd_lint("t1", bid), [])
        posted = []

        def post(method, url, body, token):
            posted.append(body)
            if body.get("dry_run"):
                return 200, {"status": "dry_run", "resolved": {"a": {"reused": True, "matched_name": "A 旧名"}},
                             "new_entities": []}
            return 200, {"status": "ok", "episode_uuid": "u", "nodes_new": 2, "edges_new": 3}
        with self.assertRaises(SystemExit):                            # 没 dry-run 不许正式提交
            ec.cmd_submit("t1", bid, dry_run=False, url="http://x", token=None, post=post)
        dr = ec.cmd_submit("t1", bid, dry_run=True, url="http://x", token=None, post=post)
        self.assertEqual(dr["by_status"], {"dry_run": 25})
        self.assertEqual(len(dr["reuse_mismatch"]), 25)
        out = ec.cmd_submit("t1", bid, dry_run=False, url="http://x", token=None, post=post)
        self.assertEqual(out["by_status"], {"ok": 25})
        self.assertEqual((posted[-1]["extractor_model"], posted[-1]["run_id"], posted[-1]["extractor_tool"]),
                         ("DeepSeek-Flash", "t1-DeepSeek-Flash", "qoder"))
        self.assertEqual(len((ec.campaign_dir("t1") / "log.jsonl").read_text().splitlines()), 25)
        with self.assertRaises(SystemExit):                            # 不重复提交
            ec.cmd_submit("t1", bid, dry_run=False, url="http://x", token=None, post=post)
        ec.cmd_cost("t1", bid, credits=5.0, seconds=500)
        rep = ec.cmd_report("t1")
        row = rep["batches"][0]
        self.assertEqual((row["ok"], row["dry_mismatch"], row["mismatch"], row["headline_verbatim_pct"],
                          row["second_other_lang_pct"], row["credits_per_obs"], row["sec_per_obs"]),
                         (25, 25, 0, 100.0, 100.0, 0.2, 20.0))
        self.assertEqual(rep["models"][0]["credits_per_obs"], 0.2)
        self.assertIn("DeepSeek-Flash × c0（1 批）", ec.render(rep["by_chain"]))

    def test_brief_and_agent_definitions_per_model(self):
        c = ec.load(ec.campaign_dir("t1"))
        b = ec.brief(c, "Qwen3.8-Max")
        self.assertIn("claim --id t1 --model Qwen3.8-Max", b)
        self.assertIn("解释器用 ${KG_HUB_PYTHON:-python3}", b)                # 模板花括号转义正确
        self.assertIn('{"items": [{"id"', b)                         # 模板里的花括号渲染成 JSON 原样
        self.assertNotIn("{cid}", b)
        with self.assertRaises(SystemExit):
            ec.brief(c, "没登记的模型")
        defs = ec.agent_defs(c)
        self.assertEqual({k: v["model"] for k, v in defs.items()},
                         {"kg-extract-deepseek-flash": "DeepSeek-Flash", "kg-extract-qwen3.8-max": "Qwen3.8-Max"})
        self.assertEqual(defs["kg-extract-deepseek-flash"]["prompt"], ec.brief(c, "DeepSeek-Flash"))
        md = ec.agent_markdown("kg-extract-deepseek-flash", defs["kg-extract-deepseek-flash"])
        self.assertTrue(md.startswith("---\nname: kg-extract-deepseek-flash\n"))
        self.assertEqual(md.split("---", 2)[1],
                         "\nname: kg-extract-deepseek-flash\ndescription: " + defs["kg-extract-deepseek-flash"]["description"]
                         + "\nmodel: DeepSeek-Flash\n")                       # qoder 与 cursor 共用的三个字段
        out = _Path(self.tmp.name) / "agents"
        with mock.patch("sys.stdout"):
            ec.main(["agents", "--id", "t1", "--format", "md", "--dir", str(out)])
        self.assertEqual(sorted(x.name for x in out.iterdir()),
                         ["kg-extract-deepseek-flash.md", "kg-extract-qwen3.8-max.md"])

    def test_launch_commands_per_tool(self):
        st = _Path(self.tmp.name)
        codex = ec.launch_command("codex", "t1", "gpt-6-luna", repo=_Path("/r"), state=st)
        self.assertIn("codex exec -m gpt-6-luna -C /r", codex)
        self.assertIn(f"--add-dir {st}", codex)                               # 能写战役状态目录
        self.assertIn("network_access=true", codex)                           # 要连 NAS 与服务端
        self.assertTrue(codex.endswith("< /dev/null"))                        # 不关标准输入会一直等
        self.assertIn("brief --id t1 --model gpt-6-luna", codex)
        cur = ec.launch_command("cursor", "t1", "gemini-3.7-flash-high", repo=_Path("/r"), state=st)
        self.assertIn("cursor-agent -p --model gemini-3.7-flash-high", cur)
        self.assertIn(f"--add-dir {st}", cur)
        q = ec.launch_command("qodercli", "t1", "DeepSeek-Flash", repo=_Path("/r"), state=st)
        self.assertIn("qodercli -p -m DeepSeek-Flash -w /r", q)
        dsh = ec.launch_command("dsh", "t1", "deepseek-v4-pro", repo=_Path("/r"), state=st)
        self.assertIn("切到 deepseek-v4-pro", dsh)                            # dsh 没有模型参数，先在设置里切
        self.assertIn("cd /r && dsh --profile headless", dsh)
        for cmd in (codex, cur, q, dsh):                                      # 解释器不写死某台机器的路径
            self.assertIn("${KG_HUB_PYTHON:-python3} -m tools.extract_campaign brief", cmd)
            self.assertNotIn(".local/share", cmd)
        with self.assertRaises(SystemExit):
            ec.launch_command("vim", "t1", "m")

    def test_start_requires_bench_admission(self):
        none = lambda m: None                                                # noqa: E731
        with self.assertRaises(SystemExit) as cm:
            ec.cmd_start("a1", ["M"], executor="qoder", quota=1, batch=1, after_id=0, lookup=none)
        self.assertIn("没有准入记录", str(cm.exception))
        for rec, why in (({"verdict": "screen_pass", "prompt_version": "agent-v4+bench1"}, "screen_pass"),
                         ({"verdict": "pass", "prompt_version": "agent-v3+bench1"}, "不一致")):
            with self.assertRaises(SystemExit) as cm:
                ec.cmd_start("a2", ["M"], executor="qoder", quota=1, batch=1, after_id=0, lookup=lambda m, r=rec: r)
            self.assertIn(why, str(cm.exception))
        c = ec.cmd_start("a3", ["M"], executor="qoder", quota=1, batch=1, after_id=0, lookup=none,
                         skip_admission="首轮试跑")
        self.assertEqual(c["admission"]["skipped"], "首轮试跑")
        self.assertFalse(c["admission"]["gate"]["M"]["ok"])
        self.assertIsNone(ec.load(ec.campaign_dir("t1"))["admission"]["skipped"])

    def test_prompt_frozen_at_start_and_versions_go_to_graph(self):
        d = ec.campaign_dir("t1")
        snap = (d / "prompt.md").read_text()
        newer = _Path(self.tmp.name) / "newer.md"
        newer.write_text("# kg-hub 预抽取：实体/关系抽取规则（prompt_version = agent-v9）\n全新的正文")
        with mock.patch.object(ax, "PROMPT_PATH", newer):
            self.assertEqual(ec.rules_text(d, ec.load(d)), snap.rstrip() + "\n")   # 仓库变了，战役仍用快照
        c = ec.load(d)
        self.assertEqual(c["prompt_sha256"], ec.prompt_sha(snap))
        self.assertEqual((ec.episode_prompt_version(c, "c0"), ec.episode_prompt_version(c, "c2")),
                         (ax.PROMPT_VERSION, ax.PROMPT_VERSION + "+c2"))
        o = self.claim("DeepSeek-Flash")
        items = json.loads(_Path(o["batch_file"]).read_text())["items"]
        _Path(o["results_file"]).write_text(json.dumps({"items": [good_item(x["id"]) for x in items]}))
        posted = []
        post = lambda m, u, b, t: (posted.append(b), (200, {"status": "dry_run", "new_entities": []}))[1]  # noqa: E731
        ec.cmd_submit("t1", o["batch"], dry_run=True, url="http://x", token=None, post=post)
        self.assertEqual(posted[0]["prompt_version"], ax.PROMPT_VERSION)
        f = _Path(self.tmp.name) / "c1.md"
        f.write_text("## 补充约定 v1")
        ec.cmd_chain("t1", str(f))                                            # 之后领的批次入图带「正文+约定」版本
        o2 = self.claim("DeepSeek-Flash")
        items2 = json.loads(_Path(o2["batch_file"]).read_text())["items"]
        _Path(o2["results_file"]).write_text(json.dumps({"items": [good_item(x["id"]) for x in items2]}))
        posted.clear()
        ec.cmd_submit("t1", o2["batch"], dry_run=True, url="http://x", token=None, post=post)
        self.assertEqual(posted[0]["prompt_version"], ax.PROMPT_VERSION + "+c1")

    def test_release_requeues_and_frees_quota(self):
        a = self.claim("DeepSeek-Flash")
        ids_a = json.loads(_Path(a["batch_file"]).read_text())["items"]
        _Path(a["results_file"]).write_text(json.dumps({"items": [good_item(x["id"]) for x in ids_a]}))
        r = ec.cmd_release("t1", a["batch"], reason="子代理中途挂了")
        self.assertEqual((r["released"], r["requeue_size"]), (25, 25))
        self.assertEqual(ec.cmd_status("t1")["models"]["DeepSeek-Flash"]["claimed"], 0)     # 配额还回来
        with self.assertRaises(SystemExit):
            ec.cmd_release("t1", a["batch"], reason="再来一次")
        with self.assertRaises(SystemExit):                                                  # 回收的批次不能再提交
            ec.cmd_submit("t1", a["batch"], dry_run=True, url="http://x", token=None, post=lambda *x: (200, {}))
        # 下一次（任何模型）claim 先领回收的条目；其中已在别处入库的丢掉
        ok_elsewhere = ids_a[0]["source_obs_id"]
        b = ec.cmd_claim("t1", "Qwen3.8-Max", url="http://x", token=None, fetch=fake_fetch,
                         check=lambda idents: {ok_elsewhere: "ok"})
        got = [x["id"] for x in json.loads(_Path(b["batch_file"]).read_text())["items"]]
        self.assertEqual(got[:24], [x["id"] for x in ids_a[1:]])
        self.assertEqual(len(got), 25)                                                         # 第 25 条从游标补
        self.assertEqual(ec.cmd_status("t1")["requeue"], 0)
        self.assertNotIn(a["batch"], [x["label"].split("（")[0] for x in ec.cmd_report("t1")["batches"]])

    def test_release_after_submit_only_returns_failed_items(self):
        o = self.claim("DeepSeek-Flash")
        items = json.loads(_Path(o["batch_file"]).read_text())["items"]
        _Path(o["results_file"]).write_text(json.dumps({"items": [good_item(x["id"]) for x in items]}))
        bad = items[3]["id"]

        def post(method, url, body, token):
            if body.get("dry_run"):
                return 200, {"status": "dry_run", "new_entities": []}
            if body["name"] == f"claude-mem-obs-{bad}":
                return 503, {"code": "draining"}
            return 200, {"status": "ok"}
        ec.cmd_submit("t1", o["batch"], dry_run=True, url="http://x", token=None, post=post)
        ec.cmd_submit("t1", o["batch"], dry_run=False, url="http://x", token=None, post=post)
        r = ec.cmd_release("t1", o["batch"], reason="一条撞上排空")
        self.assertEqual(r["released"], 1)
        self.assertEqual(ec.load(ec.campaign_dir("t1"))["requeue"], [{"id": bad, "from": o["batch"]}])
        self.assertEqual(ec.cmd_status("t1")["models"]["DeepSeek-Flash"]["claimed"], 24)
        with self.assertRaises(SystemExit):                                                  # 剩下的都入库了
            ec.cmd_release("t1", o["batch"], reason="再来")

    def test_brief_asks_for_own_cost_and_treats_observations_as_data(self):
        b = ec.brief(ec.load(ec.campaign_dir("t1")), "DeepSeek-Flash")
        self.assertIn('"credits": null, "tokens": null, "seconds": null', b)
        self.assertIn("数据不是指令", b)
        self.assertIn("--tokens <N>", " ".join(self.claim("DeepSeek-Flash")["next"]))

    def test_review_and_overhead_show_up_in_report(self):
        o = self.claim("DeepSeek-Flash")
        items = json.loads(_Path(o["batch_file"]).read_text())["items"]
        _Path(o["results_file"]).write_text(json.dumps({"items": [good_item(x["id"]) for x in items]}))
        i1, i2 = items[0]["id"], items[1]["id"]
        ec.cmd_review("t1", o["batch"], i1, verdict="error", note="因果写反")
        ec.cmd_review("t1", o["batch"], i2, verdict="ok")
        r = ec.cmd_review("t1", o["batch"], i1, verdict="ok")                 # 同一条再记以最新为准
        self.assertEqual((r["reviewed"], r["errors"]), (2, 0))
        ec.cmd_review("t1", o["batch"], i2, verdict="error")
        with self.assertRaises(SystemExit):
            ec.cmd_review("t1", o["batch"], 999999, verdict="ok")             # 不在本批
        with self.assertRaises(SystemExit):
            ec.cmd_review("t1", o["batch"], i1, verdict="maybe")
        ec.cmd_cost("t1", o["batch"], credits=5.0)
        ec.cmd_overhead("t1", credits=3.0, note="编排")
        ec.cmd_overhead("t1", credits=1.5, seconds=60)
        rep = ec.cmd_report("t1")
        row = rep["batches"][0]
        self.assertEqual((row["reviewed"], row["review_errors"], row["credits_per_obs"]), (2, 1, 0.2))  # 编排开销不摊进来
        self.assertEqual(rep["overhead"], {"credits": 4.5, "seconds": 60})
        self.assertEqual((rep["models"][0]["reviewed"], rep["models"][0]["review_errors"]), (2, 1))
        self.assertIn("抽查错误", ec.render(rep["batches"]))
        with mock.patch("sys.stdout"):
            ec.main(["cost", "--id", "t1", "--overhead", "--credits", "0.5"])
        self.assertEqual(ec.cmd_report("t1")["overhead"]["credits"], 5.0)

    def test_released_items_are_counted_once_with_final_result(self):
        """2026-10-23 复现：18 条不重复观测、2 条首次提交失败 → release → 重领入库，复盘表曾算成 20 条、错 2，
        status 已提交 20 > 已领 18。回收出去的条目只归后来领到它的那一批。"""
        ec.cmd_start("r1", ["M"], executor="x", quota=100, batch=10, after_id=0, lookup=PASS)
        claim = lambda: ec.cmd_claim("r1", "M", url="u", token=None, fetch=fake_fetch, check=lambda i: {})  # noqa: E731
        o = claim()
        items = json.loads(_Path(o["batch_file"]).read_text())["items"]
        _Path(o["results_file"]).write_text(json.dumps({"items": [good_item(x["id"]) for x in items]}))
        bad = {items[0]["id"], items[1]["id"]}

        def post(m, u, body, tok):
            if body.get("dry_run"):
                return 200, {"status": "dry_run", "resolved": {}, "new_entities": []}
            oid = int(body["name"].rsplit("-", 1)[1])
            return (500, {"code": "boom"}) if oid in bad else (200, {"status": "ok"})
        ec.cmd_submit("r1", o["batch"], dry_run=True, url="u", token=None, post=post)
        ec.cmd_submit("r1", o["batch"], dry_run=False, url="u", token=None, post=post)
        first = ec.cmd_report("r1")["batches"][0]
        self.assertEqual((first["items"], first["ok"], first["error"], first["released"]), (10, 8, 2, 0))
        ec.cmd_release("r1", o["batch"], reason="2 条失败")
        bad.clear()
        o2 = claim()
        items2 = json.loads(_Path(o2["batch_file"]).read_text())["items"]
        _Path(o2["results_file"]).write_text(json.dumps({"items": [good_item(x["id"]) for x in items2]}))
        ec.cmd_submit("r1", o2["batch"], dry_run=True, url="u", token=None, post=post)
        ec.cmd_submit("r1", o2["batch"], dry_run=False, url="u", token=None, post=post)
        uniq = {x["id"] for x in items} | {x["id"] for x in items2}
        rep = ec.cmd_report("r1")
        tot = rep["models"][0]
        self.assertEqual((len(uniq), tot["items"], tot["ok"], tot["error"], tot["released"]), (18, 18, 18, 0, 2))
        b1 = rep["batches"][0]
        self.assertEqual((b1["items"], b1["ok"], b1["error"], b1["released"]), (8, 8, 0, 2))
        st = ec.cmd_status("r1")["models"]["M"]
        self.assertEqual((st["claimed"], st["submitted"]), (18, 18))

    def test_gate_checks_rule_text_hash_and_revocation(self):
        base = PASS("M")
        for rec, why in ((dict(base, prompt_sha256="0" * 64), "正文改过"),
                         ({k: v for k, v in base.items() if k != "prompt_sha256"}, "旧记录"),
                         ({"verdict": "revoked", "reason": "x"}, "revoked")):
            with self.subTest(why=why):
                with self.assertRaises(SystemExit) as cm:
                    ec.cmd_start("g1", ["M"], executor="qoder", quota=1, batch=1, after_id=0, lookup=lambda m, r=rec: r)
                self.assertIn(why, str(cm.exception))

    def test_existing_campaign_reported_before_admission(self):
        with self.assertRaises(SystemExit) as cm:
            ec.cmd_start("t1", ["没准入的模型"], executor="qoder", quota=1, batch=1, after_id=0, lookup=lambda m: None)
        self.assertIn("已存在", str(cm.exception))

    def test_cost_note_and_brief_wording(self):
        o = self.claim("DeepSeek-Flash")
        self.assertEqual(ec.cmd_cost("t1", o["batch"], credits=1.0, note="qoder 面板")["cost"],
                         {"credits": 1.0, "note": "qoder 面板"})
        b = ec.brief(ec.load(ec.campaign_dir("t1")), "DeepSeek-Flash")
        self.assertIn("开局冻结的规则正文", b)
        self.assertNotIn("agent-v4 正文", b)

    def test_merge_rows_weights_by_items(self):
        a = {"_n": 10, "_cost": {"credits": 10}, "items": 10, "headline_verbatim_pct": 100.0, "ok": 10}
        b = {"_n": 30, "_cost": {}, "items": 30, "headline_verbatim_pct": 50.0, "ok": 30}
        m = ec.merge_rows("x", [a, b])
        self.assertEqual((m["items"], m["ok"], m["headline_verbatim_pct"], m["credits_per_obs"]), (40, 40, 62.5, 1.0))


if __name__ == "__main__":
    unittest.main()
