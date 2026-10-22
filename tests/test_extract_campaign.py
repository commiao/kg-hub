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


class CampaignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": self.tmp.name})
        self.env.start()
        self.patches = [mock.patch.object(ax, "select_batch", fake_select),
                        mock.patch.object(ax, "load_config", lambda: {})]
        for p in self.patches:
            p.start()
        ec.cmd_start("t1", ["DeepSeek-Flash", "Qwen3.8-Max"], executor="qoder", quota=60, batch=25, after_id=0)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def claim(self, model):
        return ec.cmd_claim("t1", model, url="http://x", token=None, fetch=fake_fetch, check=lambda idents: {})

    def test_start_validates_and_defaults_cursor_to_agent_extract_state(self):
        with self.assertRaises(SystemExit):
            ec.cmd_start("t1", ["X"], executor="qoder", quota=1, batch=1, after_id=0)        # 已存在
        with self.assertRaises(SystemExit):
            ec.cmd_start("t2", ["a b"], executor="qoder", quota=1, batch=1, after_id=0)      # 来源标记非法
        with self.assertRaises(SystemExit):
            ec.cmd_start("t3", ["M", "M"], executor="qoder", quota=1, batch=1, after_id=0)   # 重复
        (_Path(self.tmp.name) / "state.json").write_text(json.dumps({"cursor": 123}))
        c = ec.cmd_start("t4", ["M"], executor="codex", quota=1, batch=1, after_id=None)
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

    def test_merge_rows_weights_by_items(self):
        a = {"_n": 10, "_cost": {"credits": 10}, "items": 10, "headline_verbatim_pct": 100.0, "ok": 10}
        b = {"_n": 30, "_cost": {}, "items": 30, "headline_verbatim_pct": 50.0, "ok": 30}
        m = ec.merge_rows("x", [a, b])
        self.assertEqual((m["items"], m["ok"], m["headline_verbatim_pct"], m["credits_per_obs"]), (40, 40, 62.5, 1.0))


if __name__ == "__main__":
    unittest.main()
