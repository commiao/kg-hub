"""tools/extract_campaign：多模型并发领批不撞批、配额、约定版本、自检→dry-run→入库的门、复盘表。

取观测与提交都用假函数，不碰 NAS 与服务端。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import contextlib
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
    return {"model": model, "verdict": "pass", "prompt_version": ax.PROMPT_VERSION + "+bench1", "bench": "b", "decided_at": "t",
            "prompt_sha256": ec.prompt_sha(ax.PROMPT_PATH.read_text())}


class CampaignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 测试进程不在战役专用目录里：用 CLAUDE_MEM_INTERNAL=1 过「不被 claude-mem 记录」的自检（自检本身另有用例）
        self.env = mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": self.tmp.name,
                                                "KG_HUB_CLAIMS_DIR": os.path.join(self.tmp.name, "claims"),
                                                "KG_HUB_NAS_GATE_DIR": os.path.join(self.tmp.name, "nas-gate"),
                                                "CLAUDE_MEM_INTERNAL": "1",
                                                "CLAUDE_MEM_SETTINGS_PATH": os.path.join(self.tmp.name, "cm.json")})
        self.env.start()
        legacy_prompt = _Path(self.tmp.name) / "legacy-v4.md"
        legacy_prompt.write_text(ax.PROMPT_PATH.read_text().replace("agent-v5", "agent-v4"))
        self.patches = [mock.patch.object(ax, "PROMPT_VERSION", "agent-v4"),
                        mock.patch.object(ax, "PROMPT_PATH", legacy_prompt),
                        mock.patch.object(ax, "select_batch", fake_select),
                        mock.patch.object(ax, "load_config", lambda: {}),
                        # These legacy batch/claim fixtures test frozen campaigns.
                        # New start-v2 gates are exercised without mocks in
                        # test_semantic_regression, not fabricated in this helper.
                        mock.patch("utils.semantic_regression.validate_admission", return_value={}),
                        mock.patch.object(ec, "model_readiness", return_value={})]
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

    def test_chain_versions_accumulate_in_rules_and_batch_rules(self):
        d = ec.campaign_dir("t1")
        f1, f2 = _Path(self.tmp.name) / "c1.md", _Path(self.tmp.name) / "c2.md"
        f1.write_text("## 约定 c1\n- 少用 relates_to")
        f2.write_text("## 约定 c2\n- fact 一句一事实")
        ec.cmd_chain("t1", str(f1))
        b1 = self.claim("DeepSeek-Flash")["batch"]
        self.assertEqual(ec.cmd_chain("t1", str(f2))["chain"], "c2")
        c = ec.load(d)
        text = ec.rules_text(d, c)                                     # 登记 c2 后 c1 仍在，按登记顺序
        self.assertIn("累加：c1、c2", text)
        self.assertLess(text.index("少用 relates_to"), text.index("一句一事实"))
        self.assertTrue(text.rstrip().endswith("一句一事实"))
        self.assertEqual(ec.cmd_status("t1")["chain_versions"], ["c1", "c2"])
        old = ec.batch_rules_text(d, c, ec.find_batch(c, b1))           # c1 时领的批次只带 c1
        self.assertIn("少用 relates_to", old)
        self.assertNotIn("一句一事实", old)
        legacy = {k: v for k, v in c.items() if k != "chain_mode"}      # 之前开的战役：仍只给当前一版
        self.assertEqual(ec.chain_versions(legacy), ["c2"])
        self.assertNotIn("少用 relates_to", ec.rules_text(d, legacy))

    def test_lint_flags_overlong_rest_fact_with_same_measure_as_report(self):
        o = self.claim("DeepSeek-Flash")
        bid, items = o["batch"], json.loads(_Path(o["batch_file"]).read_text())["items"]
        rp = _Path(o["results_file"])
        res = [good_item(x["id"]) for x in items]
        res[0]["edges"][2]["fact"] = "A " + "x" * 196 + "  B"           # 空白归一后 200 字符：合规
        res[1]["edges"][2]["fact"] = "A " + "x" * 197 + " B"            # 201：超
        res[2]["edges"][0]["fact"] = TITLES[items[2]["id"]] + "y" * 300  # 标题边、译文边不归这条管
        rp.write_text(json.dumps({"items": res}, ensure_ascii=False))
        probs = ec.cmd_lint("t1", bid)
        self.assertEqual(len(probs), 1, probs)
        self.assertIn(f"{items[1]['id']}: 第 3 条边 fact 201 字符", probs[0])
        with self.assertRaises(SystemExit):                            # 超长 fact 挡 dry-run
            ec.cmd_submit("t1", bid, dry_run=True, url="http://x", token=None, post=lambda *a: (200, {}))

    def test_lint_checks_summary_only_for_entities_dry_run_says_are_new(self):
        o = self.claim("DeepSeek-Flash")
        bid, items = o["batch"], json.loads(_Path(o["batch_file"]).read_text())["items"]
        rp = _Path(o["results_file"])
        rp.write_text(json.dumps({"items": [good_item(x["id"]) for x in items]}, ensure_ascii=False))
        first = items[0]["id"]

        def post(method, url, body, token):
            if body.get("dry_run"):
                # A 并到旧实体；B 第一条新建，之后的条目并到同批前面那个（服务端靠 dry_run_pending 判）
                fresh = [] if body.get("dry_run_pending") else [{"name": "b", "type": "Tool"}]
                return 200, {"status": "dry_run", "new_entities": fresh,
                             "resolved": {"a": {"reused": True, "matched_name": "a"}}}
            return 200, {"status": "ok", "episode_uuid": "u"}
        self.assertEqual(ec.cmd_lint("t1", bid), [])                   # 预览前离线分不出新旧，不查
        self.assertEqual(len(ec.lint_summary_pending("t1", bid)), len(items))
        ec.cmd_submit("t1", bid, dry_run=True, url="http://x", token=None, post=post)
        self.assertEqual(ec.lint_summary_pending("t1", bid), [])
        probs = ec.cmd_lint("t1", bid)                                 # 只有第一条的 B；复用的 A（summary "s"）不查
        self.assertEqual(probs, [f"{bid} {first}: 新建实体 summary 0 字，少于 30（规则 24）：B"])
        with self.assertRaises(SystemExit):                            # 不许正式提交
            ec.cmd_submit("t1", bid, dry_run=False, url="http://x", token=None, post=post)
        res = [good_item(x["id"]) for x in items]
        res[0]["entities"][1]["summary"] = "B 是本条观测里新出现的工具，负责金丝雀路径的健康检查与失败时的自动回滚"
        self.assertGreaterEqual(len(res[0]["entities"][1]["summary"]), 30)
        rp.write_text(json.dumps({"items": res}, ensure_ascii=False))
        self.assertEqual(ec.cmd_lint("t1", bid), [])
        ec.cmd_submit("t1", bid, dry_run=True, url="http://x", token=None, post=post)   # 改过要重新预览
        self.assertEqual(ec.cmd_submit("t1", bid, dry_run=False, url="http://x", token=None,
                                       post=post)["by_status"], {"ok": len(items)})

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
        # 旧服务端只回 resolved：复用照样逐条列出（类型未知），不判跨类型
        self.assertEqual(len(dr["reused"]), 25)
        self.assertEqual(dr["reused"][0], f"{items[0]['id']}: A（Concept）→ A 旧名（类型未知）")
        self.assertEqual(dr["reuse_cross_type"], [])
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
        self.assertIn("当前 HOME 下合格的 ~/.local/share/kg-hub/venv/bin/python", b)                # 模板花括号转义正确
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
            self.assertIn('"${KG_HUB_CODE:-$HOME/.local/share/kg-hub/current}/tools/extract" campaign brief', cmd)
            self.assertNotIn("-m tools.extract_campaign", cmd)
        with self.assertRaises(SystemExit):
            ec.launch_command("vim", "t1", "m")

    def test_launch_shell_preserves_spaces_and_metacharacters_as_literal_paths(self):
        import subprocess
        import sys
        root = _Path(self.tmp.name)
        session, state, registry = [root / name for name in ("session ;literal", "state $literal", "registry 'literal")]
        session.mkdir(); state.mkdir(); registry.mkdir()
        binary, code = root / "bin", root / "code space"
        binary.mkdir(); (code / "tools").mkdir(parents=True)
        entry = code / "tools/extract"
        entry.write_text("#!" + sys.executable + "\nimport sys,os\n"
                         "if sys.argv[1]=='campaign': print('synthetic brief',end='')\n"
                         "else: os.execv(sys.executable,[sys.executable,'-S'," +
                         repr(str(ec.REPO / "tools/extract_cli.py")) + "]+sys.argv[1:])\n")
        entry.chmod(0o755)
        worker = binary / "worker"
        worker.write_text("#!" + sys.executable + "\nimport sys,json,os\nprint(json.dumps({'cwd':os.getcwd(),'args':sys.argv[1:],'internal':os.environ.get('CLAUDE_MEM_INTERNAL')}))\n")
        worker.chmod(0o755)
        for binary_name in ("codex", "cursor-agent", "qodercli", "dsh"):
            (binary / binary_name).symlink_to(worker)
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ.get("PATH", ""), KG_HUB_CODE=str(code))
        env.pop("CLAUDE_MEM_INTERNAL", None)
        with mock.patch.object(ec.claim_registry, "claims_dir", return_value=registry):
            for tool in ec.LAUNCH_TOOLS:
                cmd = ec.launch_command(tool, "t1", "model", repo=session, state=state)
                result = json.loads(subprocess.run(["/bin/sh", "-c", cmd], env=env, text=True,
                    capture_output=True, check=True).stdout)
                self.assertEqual(_Path(result["cwd"]).resolve(), session.resolve())
                self.assertIsNone(result["internal"])
                self.assertIn("synthetic brief", result["args"])
                if tool != "dsh":
                    self.assertIn(str(session), result["args"])
                    self.assertIn(str(state), result["args"])
                    self.assertIn(str(registry), result["args"])

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

    def test_frozen_snapshot_failures_block_all_consumers_before_side_effects(self):
        d = ec.campaign_dir("t1")
        claimed = self.claim("DeepSeek-Flash")
        bid = claimed["batch"]
        items = json.loads(_Path(claimed["batch_file"]).read_text())["items"]
        _Path(claimed["results_file"]).write_text(json.dumps({"items": [good_item(x["id"]) for x in items]}))
        original = ec.load(d)
        snapshot = (d / "prompt.md").read_bytes()
        output = _Path(self.tmp.name) / "unapproved.json"
        review = _Path(self.tmp.name) / "receipt.json"
        review.write_text("{}")
        for corruption in ("missing", "wrong_sha", "wrong_bytes", "wrong_header", "missing_sha", "no_header"):
            for consumer in ("rules", "sheet", "check", "claim", "submit", "cli_claim", "mark_empty"):
                with self.subTest(corruption=corruption, consumer=consumer):
                    c = json.loads(json.dumps(original))
                    (d / "prompt.md").write_bytes(snapshot)
                    if corruption == "missing":
                        (d / "prompt.md").unlink()
                    elif corruption == "wrong_sha":
                        c["prompt_sha256"] = "0" * 64
                    elif corruption == "wrong_bytes":
                        (d / "prompt.md").write_bytes(snapshot + b"\nchanged")
                    elif corruption in ("wrong_header", "no_header"):
                        text = snapshot.decode().replace("agent-v4", "agent-v5") if corruption == "wrong_header" else "No version header\nbody"
                        (d / "prompt.md").write_text(text)
                        c["prompt_sha256"] = ec.prompt_sha(text)
                    else:
                        c.pop("prompt_sha256")
                    ec.save(d, c)
                    before = (d / "campaign.json").read_bytes()
                    with mock.patch.object(ec.claim_registry, "opened") as registry, \
                         mock.patch.object(ax, "runtime_preflight") as preflight, \
                         mock.patch.object(ax, "_http") as post:
                        with self.assertRaisesRegex(SystemExit, "frozen_prompt_"):
                            if consumer == "rules":
                                ec.rules_text(d, c)
                            elif consumer == "sheet":
                                ec.cmd_quality_sheet("t1", bid, str(output))
                            elif consumer == "check":
                                ec.cmd_quality_check("t1", bid, str(review))
                            elif consumer == "claim":
                                self.claim("DeepSeek-Flash")
                            elif consumer == "mark_empty":
                                ec.cmd_mark_empty("t1", url="http://fake", token=None, post=post)
                            elif consumer == "submit":
                                ec.cmd_submit("t1", bid, dry_run=False, url="http://fake", token=None, post=post)
                            else:
                                ec.main(["claim", "--id", "t1", "--model", "DeepSeek-Flash"])
                        registry.assert_not_called()
                        preflight.assert_not_called()
                        post.assert_not_called()
                    self.assertEqual((d / "campaign.json").read_bytes(), before)
                    self.assertFalse(output.exists())
        (d / "prompt.md").write_bytes(snapshot)
        ec.save(d, original)
        self.assertEqual(ec.frozen_prompt_text(d, original), snapshot.decode())
        self.assertEqual(original["prompt_version"], "agent-v4")
        # Verified legacy rules remain v4 and keep their original admission.
        posted = []
        ec.cmd_submit("t1", bid, dry_run=True, url="http://fake", token=None,
            post=lambda m, u, body, t: (posted.append(body), (200, {"status": "dry_run"}))[1])
        self.assertTrue(posted)
        self.assertTrue(all(body["prompt_version"] == "agent-v4" for body in posted))
        self.assertEqual(ec.load(d)["admission"], original["admission"])

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
        self.assertIn("起草时就按一句一事实写短句", b)
        self.assertIn("提交或模型调用被权限、hook 或分类器拦下时", b)
        self.assertIn("ps等可选诊断不可用不代表提交失败", b)
        self.assertIn("operation-status --id t1", b)
        self.assertIn("补充约定是累加的", b)
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



class NoSelfObservationTest(unittest.TestCase):
    """抽取会话不产生新观测（2026-10-24：跑战役的 codex 会话 2 小时产生 214 条观测，抽取工作又进了待抽队列）。"""

    def setUp(self):
        # Isolate tracking behavior from independently tested start-v2 gates.
        for patch in (mock.patch("utils.semantic_regression.validate_admission", return_value={}),
                      mock.patch.object(ec, "model_readiness", return_value={})):
            patch.start()
            self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _Path(self.tmp.name)
        self.settings = self.root / "cm" / "settings.json"
        self.ws = self.root / "kg-hub-campaign"
        self.env = mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": str(self.root / "state"),
                                                "KG_HUB_CLAIMS_DIR": str(self.root / "claims"),
                                                "CLAUDE_MEM_SETTINGS_PATH": str(self.settings),
                                                "KG_HUB_CAMPAIGN_WORKSPACE": str(self.ws)})
        self.env.start()
        for k in ("CLAUDE_MEM_INTERNAL", "CLAUDE_PROJECT_DIR"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_path_excluded_matches_claude_mem_semantics(self):
        pats = "13.*,~/a/*,/w/kg-hub-campaign,/w/kg-hub-campaign/**"
        for path, want in (("/w/kg-hub-campaign", True), ("/w/kg-hub-campaign/tools/x", True),
                           ("/w/kg-hub", False), ("/w/kg-hub-campaign2", False),
                           ("/x/13.abc", True), ("/x/130abc", False),                  # 最后一级目录名也匹配；. 是字面量
                           (os.path.expanduser("~/a/b"), True), (os.path.expanduser("~/a/b/c"), False)):  # * 不跨目录
            with self.subTest(path=path):
                self.assertEqual(ec.path_excluded(path, pats), want)
        self.assertFalse(ec.path_excluded("/anything", ""))

    def test_guard_refuses_tracked_session_and_accepts_excluded_or_internal(self):
        with self.assertRaises(SystemExit) as cm:
            ec.tracking_guard(cwd=str(self.ws))
        self.assertIn("tools/extract\" campaign workspace", str(cm.exception))
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"CLAUDE_MEM_EXCLUDED_PROJECTS": f"13.*,{self.ws},{self.ws}/**"}))
        self.assertFalse(ec.tracking_guard(cwd=str(self.ws / "tools"))["tracked"])
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": "/elsewhere"}):   # 会话目录不在专用目录：只 cd 进去不算
            with self.assertRaises(SystemExit):
                ec.tracking_guard(cwd=str(self.ws))
        with mock.patch.dict(os.environ, {"CLAUDE_MEM_INTERNAL": "1"}):
            self.assertFalse(ec.tracking_guard(cwd="/elsewhere")["tracked"])
        self.assertTrue(ec.tracking_guard("我知道", cwd="/elsewhere")["tracked"])   # 显式例外放行

    def test_start_and_claim_refuse_unless_excluded_and_record_exceptions(self):
        with self.assertRaises(SystemExit):
            ec.cmd_start("g1", ["M"], executor="codex", quota=10, batch=5, after_id=0, lookup=PASS)
        c = ec.cmd_start("g1", ["M"], executor="codex", quota=10, batch=5, after_id=0, lookup=PASS,
                         allow_tracking="测试")
        self.assertEqual(c["tracking_exceptions"][0]["reason"], "测试")
        with mock.patch.object(ax, "select_batch", fake_select), mock.patch.object(ax, "load_config", lambda: {}):
            with self.assertRaises(SystemExit):
                ec.cmd_claim("g1", "M", url="http://x", token=None, fetch=fake_fetch, check=lambda i: {})
            self.assertFalse(list((ec.campaign_dir("g1") / "batches").glob("*.json")))   # 拒绝时一批都没领
            ec.cmd_claim("g1", "M", url="http://x", token=None, fetch=fake_fetch, check=lambda i: {},
                         allow_tracking="测试领批")
        ex = ec.load(ec.campaign_dir("g1"))["tracking_exceptions"]
        self.assertEqual([e["cmd"] for e in ex], ["start", "claim"])

    def test_workspace_creates_worktree_and_appends_exclusion_with_backup(self):
        calls = []

        def runner(cmd):
            calls.append(cmd)
            if "--git-common-dir" in cmd:
                return "/main/.git"
            if cmd[3:5] == ["worktree", "add"]:
                self.ws.mkdir()
            if "status" in cmd:
                return self.dirty
            return "abc1234" if "rev-parse" in cmd else ""
        self.dirty = ""
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"CLAUDE_MEM_EXCLUDED_PROJECTS": "13.*", "OTHER": 1}))
        out = ec.cmd_workspace(runner=runner)
        self.assertEqual((out["action"], out["excluded"], out["head"]), ("created", True, "abc1234"))
        self.assertIn(["git", "-C", "/main", "worktree", "add", "--detach", str(self.ws), "origin/main"], calls)
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["CLAUDE_MEM_EXCLUDED_PROJECTS"], f"13.*,{self.ws},{self.ws}/**")   # 原有规则保留
        self.assertEqual(data["OTHER"], 1)
        self.assertEqual(json.loads(_Path(out["backup"]).read_text())["CLAUDE_MEM_EXCLUDED_PROJECTS"], "13.*")
        again = ec.cmd_workspace(runner=runner)                                      # 已存在：同步、不重复加规则
        self.assertEqual((again["action"], again["added_patterns"], again["backup"]), ("synced", [], None))
        self.dirty = " M tools/x.py"
        with self.assertRaises(SystemExit):                                         # 专用目录有改动：不动
            ec.cmd_workspace(runner=runner)

    def test_launch_runs_in_workspace_without_claude_mem(self):
        for tool in ec.LAUNCH_TOOLS:
            cmd = ec.launch_command(tool, "t1", "m")
            with self.subTest(tool=tool):
                self.assertIn(f"cd {self.ws} && ", cmd)
                self.assertNotIn("CLAUDE_MEM_INTERNAL=1", cmd)
        self.assertIn("--ignore-user-config", ec.launch_command("codex", "t1", "m"))
        self.assertIn("--setting-sources local", ec.launch_command("qodercli", "t1", "m"))

    def test_mark_empty_backfills_once_from_campaign_log(self):
        with mock.patch.dict(os.environ, {"CLAUDE_MEM_INTERNAL": "1"}):
            ec.cmd_start("g2", ["M"], executor="codex", quota=10, batch=5, after_id=0, lookup=PASS)
        d = ec.campaign_dir("g2")
        (d / "batches" / "M-001.json").write_text(json.dumps({"batch_id": "M-001", "items": [
            {"id": i, "name": f"claude-mem-obs-{i}", "source_description": "sd", "source_obs_id": f"h{i}",
             "reference_time": "t", "episode_body": "b"} for i in (1, 2, 3)]}))
        log = [{"id": 1, "status": "ok", "batch": "M-001", "model": "M", "run_id": "g2-M"},
               {"id": 2, "status": "empty", "batch": "M-001", "model": "M", "run_id": "g2-M", "executor": "codex"},
               {"id": 3, "status": "empty", "batch": "M-001", "model": "M", "run_id": "g2-M", "executor": "codex",
                "empty_recorded": True, "http": 200},                                     # 新版提交时已记过
               {"id": 9, "status": "empty", "batch": "M-404", "model": "M"}]              # 批次文件找不到
        (d / "log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in log))
        posted = []

        def post(method, url, body, token):
            posted.append(body)
            return 200, {"status": "empty_recorded"}
        out = ec.cmd_mark_empty("g2", url="http://x", token=None, post=post)
        self.assertEqual((out["candidates"], out["recorded"], out["by_status"].get("batch_missing")), (2, 1, 1))
        self.assertEqual([(b["source_obs_id"], b["empty"], b["entities"], b["extractor_model"], b["run_id"])
                          for b in posted], [("h2", True, [], "M", "g2-M")])
        again = ec.cmd_mark_empty("g2", url="http://x", token=None, post=post)          # 补过的不再补
        self.assertEqual((again["candidates"], len(posted)), (1, 1))                     # 只剩找不到批次的那条


class DeviceWideClaimsTest(unittest.TestCase):
    """全设备在途登记（tools/claim_registry）：跨战役、战役与 agent_extract next 之间不重复领。
    只复用 CampaignTest 的夹具，不继承它的用例（否则那些用例会再跑一遍）。"""
    setUp, tearDown, claim = CampaignTest.setUp, CampaignTest.tearDown, CampaignTest.claim

    def claim_in(self, cid, model):
        return ec.cmd_claim(cid, model, url="http://x", token=None, fetch=fake_fetch, check=lambda idents: {})

    def ids_of(self, out):
        return [i["id"] for i in json.loads(_Path(out["batch_file"]).read_text())["items"]]

    def test_two_campaigns_running_at_once_never_claim_the_same_observation(self):
        ec.cmd_start("t2", ["Kimi-K3"], executor="cursor", quota=60, batch=25, after_id=0, lookup=PASS)
        jobs = [("t1", "DeepSeek-Flash"), ("t2", "Kimi-K3"), ("t1", "Qwen3.8-Max")] * 2
        with ThreadPoolExecutor(max_workers=6) as ex:
            outs = list(ex.map(lambda j: self.claim_in(*j), jobs))
        ids = [i for o in outs if o.get("batch") for i in self.ids_of(o)]
        self.assertEqual(len(ids), len(set(ids)))                          # 两个战役同一起点开局，也没有重叠
        t2 = ec.load(ec.campaign_dir("t2"))
        self.assertEqual(sum(len(b["ids"]) for b in t2["batches"]), 50)

    def test_submit_releases_ingested_and_keeps_failed_reserved(self):
        from tools import claim_registry as cr
        o = self.claim("DeepSeek-Flash")
        bid, ids = o["batch"], self.ids_of(o)
        _Path(o["results_file"]).write_text(json.dumps({"items": [good_item(i) for i in ids]}, ensure_ascii=False))

        def post(method, url, body, token):
            if body.get("dry_run"):
                return 200, {"status": "dry_run", "resolved": {}, "new_entities": []}
            if body["source_obs_id"] == f"h{ids[0]}":
                return 500, {"status": "error", "code": "boom"}
            return 200, {"status": "ok", "episode_uuid": "u"}
        ec.cmd_submit("t1", bid, dry_run=True, url="http://x", token=None, post=post)
        with cr.opened() as reg:                                           # dry-run 不注销
            self.assertEqual(reg.held_by_others("campaign:other"), set(ids))
        ec.cmd_submit("t1", bid, dry_run=False, url="http://x", token=None, post=post)
        with cr.opened() as reg:                                           # 入库的注销，失败的留着等回收重领
            self.assertEqual(reg.held_by_others("campaign:other"), {ids[0]})

    def test_agent_extract_next_and_campaigns_avoid_each_other(self):
        o = self.claim("DeepSeek-Flash")
        camp = set(self.ids_of(o))
        sd = _Path(self.tmp.name) / "next-state"
        with mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": str(sd)}), \
                mock.patch.object(ax, "runtime_preflight", lambda *a, **k: {"ok": True}), \
                mock.patch.object(ax, "fetch_remote_pending", fake_fetch), \
                mock.patch.object(ax, "check_existing", lambda idents, url, token: {}), \
                mock.patch.object(ax, "api_token", lambda *a, **k: "t"), \
                mock.patch("sys.stdout"):
            ax.main(["next", "--n", "10"])
        nxt = [i["id"] for i in json.loads((sd / "batch-b0001.json").read_text())["items"]]
        self.assertEqual(len(nxt), 10)
        self.assertFalse(camp & set(nxt))                                   # next 不领战役占着的
        b = json.loads((sd / "batch-b0001.json").read_text())
        self.assertGreater(b["stats"]["held_elsewhere"], 0)
        later = set(self.ids_of(self.claim("Qwen3.8-Max")))
        self.assertFalse(later & set(nxt))                                  # 战役也不领 next 占着的
        # next 正式提交后，入库的注销
        (sd / "results.json").write_text(json.dumps({"items": [good_item(i) for i in nxt]}, ensure_ascii=False))
        calls = []

        def http(method, url, body, token, timeout=120):
            calls.append(body)
            return 200, ({"status": "dry_run", "resolved": {}, "new_entities": []} if body.get("dry_run")
                         else {"status": "ok", "episode_uuid": "u"})
        args = ["--batch", str(sd / "batch-b0001.json"), "--file", str(sd / "results.json"), "--model", "m",
                "--session", "s"]
        # 闸门换成空的：不 ssh 测温、不碰本机真实的全机锁
        with mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": str(sd)}), \
                mock.patch("tools.nas_gate.hold", lambda kind: contextlib.nullcontext()), \
                mock.patch.object(ax, "api_token", lambda *a, **k: "t"), mock.patch("sys.stdout"):
            real = ax.submit_results
            with mock.patch.object(ax, "submit_results", lambda *a, **kw: real(*a, **{**kw, "post": http})):
                ax.main(["submit", "--dry-run", *args])
                ax.main(["submit", *args])
        from tools import claim_registry as cr
        with cr.opened() as reg:
            self.assertFalse(set(nxt) & reg.held_by_others("campaign:other"))

    def test_launch_makes_registry_dir_writable(self):
        st = _Path(self.tmp.name) / "state"
        cmd = ec.launch_command("codex", "t1", "m", repo=_Path("/r"), state=st)
        self.assertIn(f"--add-dir {st} --add-dir {os.environ['KG_HUB_CLAIMS_DIR']}", cmd)
        inside = ec.launch_command("codex", "t1", "m", repo=_Path("/r"), state=_Path(self.tmp.name))
        self.assertEqual(inside.count("--add-dir"), 1)                      # 登记目录在状态目录里就不重复加

    def test_launch_makes_actual_gate_dir_writable_without_changing_the_gate(self):
        import shlex
        from tools import nas_gate
        root = _Path(self.tmp.name)
        config = root / "gate.json"
        settings = nas_gate.load_settings()
        settings["lock_dir"] = str(root / "gate ; literal")
        config.write_text(json.dumps(settings))
        env = dict(os.environ, KG_HUB_NAS_GATE_CONFIG=str(config))
        env.pop("KG_HUB_NAS_GATE_DIR", None)
        with mock.patch.dict(os.environ, env, clear=True):
            for tool in ("codex", "cursor", "qodercli"):
                cmd = ec.launch_command(tool, "t1", "m", repo=root / "session", state=root / "state")
                self.assertIn("--add-dir " + shlex.quote(settings["lock_dir"]), cmd)
            self.assertEqual(nas_gate.load_settings(), settings)
        with mock.patch.dict(os.environ, {"KG_HUB_NAS_GATE_DIR": str(root / "override gate")}):
            cmd = ec.launch_command("codex", "t1", "m", repo=root / "session", state=root / "state")
            self.assertIn("--add-dir " + shlex.quote(str(root / "override gate")), cmd)



class FillBatchTest(unittest.TestCase):
    """战役领批：可抽观测稀疏时逐段往后扫，凑满一批再交。"""
    setUp, tearDown = CampaignTest.setUp, CampaignTest.tearDown

    def test_sparse_backlog_still_yields_a_full_batch(self):
        def sparse_select(rows, cfg, n, existing):
            picked = [{"id": r["id"], "name": f"claude-mem-obs-{r['id']}", "source_description": "sd",
                       "source_obs_id": f"h{r['id']}", "reference_time": "2026-10-22T00:00:00Z",
                       "episode_body": f"[DISCOVERY] {TITLES[r['id']]}\nbody"} for r in rows if r["id"] % 13 == 0][:n]
            return picked, {"seen": len(rows), "picked": len(picked)}
        with mock.patch.object(ax, "select_batch", sparse_select):
            o = ec.cmd_claim("t1", "DeepSeek-Flash", url="http://x", token=None, fetch=fake_fetch,
                             check=lambda idents: {})
        ids = [i["id"] for i in json.loads(_Path(o["batch_file"]).read_text())["items"]]
        self.assertEqual(len(ids), 25)                                       # 一段只有 3 条可抽，照样凑满 25
        self.assertTrue(all(i % 13 == 0 for i in ids))
        self.assertGreater(json.loads(_Path(o["batch_file"]).read_text())["stats"]["rounds"], 1)
        self.assertEqual(ec.load(ec.campaign_dir("t1"))["cursor"], ids[-1])


class NameSyncSubmitTest(unittest.TestCase):
    """命名同步接进战役：namesync 子命令、正式提交前的 dry-run 分裂判据、dry-run / 提交都经 NAS 闸门。
    「图里已有」用同一状态目录下另一个已提交的战役表示；路径布局用临时目录，不读本机 ~/work-ai。"""
    setUp, tearDown, claim = CampaignTest.setUp, CampaignTest.tearDown, CampaignTest.claim

    def prepare(self):
        from tools import campaign_namesync as ns
        self.layout = ns.Layout(workai=os.path.join(self.tmp.name, "work-ai"), home=self.tmp.name)
        old = ec.campaign_dir("t1").parent / "old"
        for sub in ("batches", "results"):
            (old / sub).mkdir(parents=True)
        (old / "campaign.json").write_text(json.dumps({"tool": "extract_campaign", "id": "old", "batches": [
            {"id": "o-001", "status": "submitted", "submitted_at": "2026-10-24T00:00:00+00:00"}]}))
        (old / "batches" / "o-001.json").write_text(json.dumps({"items": [{"id": 1, "episode_body": ""}]}))
        (old / "results" / "o-001.json").write_text(json.dumps({"items": [{"id": 1, "entities": [
            {"name": "reconcile_due（QueueDispatcher）", "type": "Concept", "summary": "s"}], "edges": []}]},
            ensure_ascii=False))
        o = self.claim("DeepSeek-Flash")
        ids = [i["id"] for i in json.loads(_Path(o["batch_file"]).read_text())["items"]]
        items = [good_item(i) for i in ids]
        self.first = ids[0]
        items[0]["entities"].append({"name": "reconcile_due", "type": "Concept",
                                     "summary": "QueueDispatcher 里按到期时间对账的入口函数，每个调度周期都会调用一次。"})
        items[0]["edges"].append({"source": "reconcile_due", "name": "depends_on", "target": "B",
                                  "fact": "reconcile_due depends on B because the dispatcher checks it on every tick"})
        _Path(o["results_file"]).write_text(json.dumps({"items": items}, ensure_ascii=False))
        self.gated = []
        return o["batch"]

    def gate(self, kind):
        self.gated.append(kind)
        return contextlib.nullcontext()

    def submit(self, bid, *, dry_run, new=("reconcile_due",), **kw):
        def post(method, url, body, token):
            if body.get("dry_run"):
                names = list(new) if body["source_obs_id"] == f"h{self.first}" else []
                return 200, {"status": "dry_run", "resolved": {}, "new_entities": [{"name": n} for n in names]}
            return 200, {"status": "ok", "episode_uuid": "u"}
        return ec.cmd_submit("t1", bid, dry_run=dry_run, url="http://x", token="t", post=post, gate=self.gate,
                             layout=self.layout, **kw)

    def test_split_blocks_submit_until_namesync_apply_and_fresh_dry_run(self):
        bid = self.prepare()
        rep, code = ec.cmd_namesync("t1", bid, layout=self.layout)
        self.assertEqual((code, rep["dryrun"]), (2, "no_dryrun"))
        self.assertEqual([(a["from"], a["to"]) for a in rep["auto"]], [("reconcile_due", "reconcile_due（QueueDispatcher）")])
        self.submit(bid, dry_run=True)
        rep, code = ec.cmd_namesync("t1", bid, layout=self.layout)
        self.assertEqual((rep["dryrun"], [s["name"] for s in rep["splits"]]), ("ok", ["reconcile_due"]))
        with self.assertRaises(ec.SubmitRefused) as cm:
            self.submit(bid, dry_run=False)
        self.assertEqual(cm.exception.code, ec.EXIT_SPLIT)
        self.assertIn("reconcile_due（QueueDispatcher）", cm.exception.message)
        self.assertEqual(self.gated, ["dryrun"])                   # 被分裂判据拦下的没去排 NAS 闸门
        self.assertEqual(ec.load(ec.campaign_dir("t1"))["batches"][0]["status"], "dry_run")
        rep, code = ec.cmd_namesync("t1", bid, apply=True, layout=self.layout)
        self.assertEqual((code, rep["applied"], rep["auto"], rep["dryrun"]), (0, 1, [], "stale"))
        res = json.loads((ec.campaign_dir("t1") / "results" / f"{bid}.json").read_text())
        self.assertIn("reconcile_due（QueueDispatcher）", [e["name"] for e in res["items"][0]["entities"]])
        with self.assertRaises(SystemExit) as cm:                  # 结果改了：旧 dry-run 失效，先重新预览
            self.submit(bid, dry_run=False)
        self.assertIn("还没 dry-run", str(cm.exception))
        self.submit(bid, dry_run=True, new=())                     # 改名后服务端复用图中节点，不再新建
        out = self.submit(bid, dry_run=False)
        self.assertEqual(out["by_status"], {"ok": len(res["items"])})
        self.assertEqual(self.gated, ["dryrun", "dryrun", "submit"])

    def test_allow_split_records_reason_and_old_marker_needs_new_dry_run(self):
        bid = self.prepare()
        self.submit(bid, dry_run=True)
        marker = ax.dryrun_marker_path(ec.campaign_dir("t1"), bid)
        data = json.loads(marker.read_text())
        marker.write_text(json.dumps({k: v for k, v in data.items() if k != "new_entities"}))   # 旧版本预览
        with self.assertRaises(ec.SubmitRefused) as cm:
            self.submit(bid, dry_run=False)
        self.assertEqual(cm.exception.code, ec.EXIT_DRYRUN_STALE)
        marker.write_text(json.dumps(data))
        self.submit(bid, dry_run=False, allow_split="核对过：两者是不同函数")
        b = ec.load(ec.campaign_dir("t1"))["batches"][0]
        self.assertEqual(b["status"], "submitted")
        self.assertEqual(b["split_exceptions"][0]["reason"], "核对过：两者是不同函数")
        self.assertEqual(b["split_exceptions"][0]["rows"][0]["name"], "reconcile_due")

    def test_cli_exit_codes_and_gate_wiring(self):
        from tools import campaign_namesync as ns
        bid = self.prepare()
        self.submit(bid, dry_run=True)
        with mock.patch.object(ns.Layout, "from_disk", classmethod(lambda cls, *a, **k: self.layout)), \
                mock.patch("tools.nas_gate.hold", self.gate), mock.patch.object(ax, "api_token", lambda *a, **k: "t"), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(ec.main(["namesync", "--id", "t1", "--batch", bid]), 2)
            self.assertEqual(ec.main(["submit", "--id", "t1", "--batch", bid]), ec.EXIT_SPLIT)
            from tools import nas_gate

            def refuse(kind):
                raise nas_gate.GateRefused(nas_gate.EXIT_NO_TEMP, "读不到温度")
            with mock.patch("tools.nas_gate.hold", refuse):
                self.assertEqual(ec.main(["submit", "--id", "t1", "--batch", bid, "--dry-run"]), nas_gate.EXIT_NO_TEMP)
        self.assertEqual(self.gated, ["dryrun"])

    def test_brief_and_claim_name_the_new_steps(self):
        text = ec.brief(ec.load(ec.campaign_dir("t1")), "DeepSeek-Flash")
        for needle in ("namesync --id t1 --batch <批次> --apply --graph", "2 命名分裂", "75 读不到 NAS 温度", "76 闸门等待超时"):
            self.assertIn(needle, text)
        self.assertTrue(any(" namesync " in c for c in self.claim("DeepSeek-Flash")["next"]))


if __name__ == "__main__":
    unittest.main()
