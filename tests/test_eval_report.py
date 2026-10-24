"""eval_report：三面（图 / 提交 / 成本）按 run 并排成一张表，列顺序固定 纪律 → 丰富度 → 消解 → 成本 → 检索，
缺面留空不顶替，自报成本与账本成本分列。全部用假数据驱动，不碰图库、不碰网络。"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import json
import os
import tempfile
import unittest
from unittest import mock

from tools import eval_report as er
from tools import agent_extract as ax

GRAPH = {
    "runA": {"episodes": 50, "entities_per_episode": 5.6, "edges_per_episode": 4.3, "zero_entity_share": 0.0,
             "zero_edge_share": 0.02, "edge_in_schema": 1.0, "edge_distinct_names": 12, "dup_triple_share": 0.0,
             "untyped_share": 0.0, "dirty_name_share": 0.01, "summary_len_avg": 166.0},
    "runB": {"episodes": 48, "entities_per_episode": 12.4, "edges_per_episode": 12.7, "zero_entity_share": 0.0,
             "zero_edge_share": 0.2, "edge_in_schema": 0.36, "edge_distinct_names": 40, "dup_triple_share": 0.04,
             "untyped_share": 0.16, "dirty_name_share": 0.05, "summary_len_avg": 398.0},
    "unknown": {"episodes": 1000},
}


def _log_records():
    base = {"status": "ok", "model": "claude-fable-5-1", "latency_s": 2.0, "stats": {"entities_in": 5, "edges_in": 4},
            "executor": "claude-code"}
    return ([dict(base, id=i, run_id="runA", reuse_mismatch=["a → b"] if i % 2 else []) for i in range(10)]
            + [dict(base, id=100, run_id="runA", status="empty", http=200, empty_recorded=True)]
            + [dict(base, id=200, run_id="runB", executor="script", model="deepseek", reuse_mismatch=[])]
            + [dict(base, id=300, status="ok")])   # 旧记录：没有 run_id


class AssembleTest(unittest.TestCase):
    def test_columns_in_fixed_order_and_sources_do_not_bleed(self):
        heads = [h for _, h, _ in er.COLUMNS]
        groups = [h.split("·")[0] for h in heads if "·" in h]
        # 列顺序：纪律 → 丰富度 → 消解 → 成本 → 检索，且每组连续
        order = []
        for g in groups:
            if not order or order[-1] != g:
                order.append(g)
        self.assertEqual(order, ["纪律", "丰富度", "消解", "成本", "检索"])
        submit = ax.summarize_log(_log_records(), "run_id")
        runs = {"runA": {"run_id": "runA", "executor": "claude-code", "model": "claude-fable-5-1",
                         "prompt_version": "agent-v3", "server_commit": "abc", "sample": "s50",
                         "self_reported": {"input_tokens": 100000, "output_tokens": 20000, "wall_s": 600}},
                "runB": None}
        journal = {"runB": {"calls_mean": 7.07, "wall_med_s": 270.0, "stage_share": {"edge": 0.18}}}
        t = er.assemble(["runA", "runB"], graph=GRAPH, submit=submit, runs=runs, journal=journal,
                        recall={"runA": 0.82})
        a, b = t["runA"], t["runB"]
        self.assertEqual((a["episodes"], a["edge_in_schema"], a["summary_len_avg"]), (50, 1.0, 166.0))
        self.assertEqual((a["ok"], a["empty"], a["error"], a["reuse_mismatch_per_obs"]), (10, 1, 0, 0.5))
        # 自报成本摊到每条 ok：10 万 / 10 条
        self.assertEqual((a["input_tokens_per_obs"], a["output_tokens_per_obs"], a["wall_s_per_obs"]),
                         (10000.0, 2000.0, 60.0))
        self.assertIsNone(a["calls_mean"])          # runA 没有账本口径
        self.assertEqual(a["recall"], 0.82)
        self.assertEqual(a["_manifest"]["server_commit"], "abc")
        # runB：没有清单 → 自报留空；账本口径有；没给召回
        self.assertIsNone(b["input_tokens_per_obs"])
        self.assertEqual((b["calls_mean"], b["wall_med_s"]), (7.07, 270.0))
        self.assertIsNone(b["recall"])
        self.assertEqual(b["ok"], 1)
        self.assertIsNone(b["_manifest"])

    def test_render_header_and_blanks(self):
        t = er.assemble(["runA", "runC"], graph=GRAPH, submit={}, runs={}, journal={}, recall={})
        md = er.render(t, "run_id")
        lines = md.splitlines()
        self.assertTrue(lines[2].startswith("| run | 条（图） | 纪律·边名合规 |"))
        self.assertIn("| 成本·输入 token/条（自报） | 成本·输出 token/条（自报） | 成本·wall s/条（自报） | "
                      "成本·调用/条（账本） | 成本·wall 中位 s（账本） | 检索·kg_eval 召回 |", lines[2])
        row_a = next(l for l in lines if l.startswith("| runA |"))
        self.assertIn("| 100.0% | 12 | 0.0% | 1.0% | 0.0% | 2.0% | 5.6 | 4.3 | 166.0 |", row_a)
        row_c = next(l for l in lines if l.startswith("| runC |"))
        self.assertEqual(row_c.count("—"), len(er.COLUMNS))   # 没有任何面的数据：全空
        self.assertNotIn("## 运行清单", md)
        md2 = er.render(er.assemble(["runA"], graph=GRAPH, runs={"runA": {"executor": "codex", "model": "m"}}), "run_id")
        self.assertIn("- **runA**：executor codex · model m", md2)
        self.assertTrue(er.render(t, "model").splitlines()[2].startswith("| 模型 |"))


class LoadersAndCliTest(unittest.TestCase):
    def test_cli_with_graph_json_and_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            sd = _Path(d) / "state"
            (sd / "runs").mkdir(parents=True)
            (sd / "log.jsonl").write_text("\n".join(json.dumps(r) for r in _log_records()) + "\n")
            (sd / "runs" / "runA.json").write_text(json.dumps(
                {"run_id": "runA", "executor": "claude-code", "model": "claude-fable-5-1",
                 "self_reported": {"input_tokens": 1000, "output_tokens": 100, "wall_s": 50}}))
            gj = _Path(d) / "compare.json"
            gj.write_text(json.dumps(GRAPH))
            out_md, out_json = _Path(d) / "r.md", _Path(d) / "r.json"
            with mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": str(sd)}):
                rc = er.main(["--run-id", "runA", "--run-id", "runB", "--graph-json", str(gj),
                              "--recall", "runB=0.7", "--md", str(out_md), "--json", str(out_json)])
            self.assertEqual(rc, 0)
            t = json.loads(out_json.read_text())
            self.assertEqual(t["runA"]["input_tokens_per_obs"], 100.0)
            self.assertEqual(t["runB"]["recall"], 0.7)
            self.assertIn("| runB |", out_md.read_text())
            # 三个分组键不能混用
            with self.assertRaises(SystemExit):
                er.main(["--run-id", "a", "--model", "m", "--graph-json", str(gj)])
            # 按模型分组：提交口径按 model 汇总，没有运行清单
            with mock.patch.dict(os.environ, {"KG_HUB_AGENT_EXTRACT_DIR": str(sd)}):
                er.main(["--model", "claude-fable-5-1", "--graph-json", str(gj), "--json", str(out_json)])
            t = json.loads(out_json.read_text())
            self.assertEqual(t["claude-fable-5-1"]["ok"], 11)
            self.assertIsNone(t["claude-fable-5-1"]["input_tokens_per_obs"])

    def test_journal_loader_uses_ab_score_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            sample = _Path(d) / "sample.json"
            sample.write_text(json.dumps({"items": [{"source_description": "sd", "source_obs_id": "1"}]}))
            calls = []

            def fake(journal, key, items):
                calls.append((journal, key, len(items)))
                return {"calls_mean": 3.0, "wall_med_s": 9.0}
            with mock.patch.object(er, "journal_route_metrics", fake):
                j = er.load_journal(["A", "B"], "j.sqlite3", str(sample), {"B": "kg_hub.entity_extract"})
            self.assertEqual(list(j), ["B"])
            self.assertEqual(calls, [("j.sqlite3", "kg_hub.entity_extract", 1)])


if __name__ == "__main__":
    unittest.main()
