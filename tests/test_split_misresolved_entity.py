"""tools/split_misresolved_entity 的纯函数：从结果文件认边、在图里挑要搬的边、端点换算。

2026-10-22 补交 b0024：`prevalidate 阶段相似度计数` 被并进同批先建的 `validate 阶段相似度计数`。
读写图的部分在 NAS 上用临时图演练（见 PR 说明），这里只测判断逻辑。
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import unittest

from tools import split_misresolved_entity as sp

W, P = "validate 阶段相似度计数", "prevalidate 阶段相似度计数"
BATCH = {"items": [{"id": 1, "name": "obs-1"}, {"id": 2, "name": "obs-2"}, {"id": 3, "name": "obs-3"}]}
RESULTS = {"items": [
    {"id": 1, "entities": [{"name": W, "type": "Concept", "summary": "v"}, {"name": "金丝雀", "type": "Event"}],
     "edges": [{"source": "金丝雀", "target": W, "name": "relates_to", "fact": "canary  validate"}]},
    {"id": 2, "entities": [{"name": P, "type": "Concept", "summary": "pre 阶段"}, {"name": "金丝雀", "type": "Event"}],
     "edges": [{"source": P, "target": "金丝雀", "name": "relates_to", "fact": "pre feeds canary"},
               {"source": "金丝雀", "target": P, "name": "relates_to", "fact": "canary sees pre"}]},
    {"id": 3, "entities": [{"name": W, "type": "Concept"}, {"name": P.upper(), "type": "Concept"}],
     "edges": [{"source": W, "target": P.upper(), "name": "relates_to", "fact": "validate vs pre"}]},
]}


class PlanTest(unittest.TestCase):
    def test_plan_lists_episodes_and_edge_ends_of_the_misresolved_entity(self):
        spec = sp.plan_from_results(RESULTS, BATCH, W, P)
        self.assertEqual(spec["entity"], {"name": P, "type": "Concept", "summary": "pre 阶段"})
        # 观测 3 里两个实体都在：误并节点的提及要留着
        self.assertEqual(spec["episodes"], [{"episode": "obs-2", "keep_wrong_mention": False},
                                            {"episode": "obs-3", "keep_wrong_mention": True}])
        self.assertEqual(spec["edges"], [
            {"episode": "obs-2", "fact": "pre feeds canary", "end": "source"},
            {"episode": "obs-2", "fact": "canary sees pre", "end": "target"},
            {"episode": "obs-3", "fact": "validate vs pre", "end": "target"}])
        with self.assertRaises(ValueError):
            sp.plan_from_results(RESULTS, BATCH, W, "不存在的实体")


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.spec = sp.plan_from_results(RESULTS, BATCH, W, P)
        self.names = {"ep1": "obs-1", "ep2": "obs-2", "ep3": "obs-3"}

    def test_picks_edges_by_fact_episode_and_wrong_end(self):
        edges = [("e-v", "u-canary", "u-w", "canary validate", ["ep1"]),       # validate 自己的边：不动
                 ("e-a", "u-w", "u-canary", "pre feeds canary", ["ep2"]),
                 ("e-b", "u-canary", "u-w", "canary sees pre", ["ep2"]),
                 ("e-self", "u-w", "u-w", "validate vs pre", ["ep3"])]          # 同条观测两端都并成了自环
        sel = sp.select_moves(self.spec, "u-w", edges, self.names)
        self.assertEqual([(m["uuid"], m["end"]) for m in sel["moves"]],
                         [("e-a", "source"), ("e-b", "target"), ("e-self", "target")])
        self.assertEqual((sel["unmatched"], sel["shared"]), ([], []))

    def test_wrong_end_other_episode_or_shared_edge_is_not_moved(self):
        edges = [("e-a", "u-canary", "u-w", "pre feeds canary", ["ep2"]),      # 误并节点在另一端：不是它
                 ("e-b", "u-canary", "u-w", "canary sees pre", ["ep1"]),       # 别的观测写的同一句：不是它
                 ("e-self", "u-w", "u-w", "validate vs pre", ["ep3", "ep1"])]  # 被别的观测复用过：不动，报出来
        sel = sp.select_moves(self.spec, "u-w", edges, self.names)
        self.assertEqual(sel["moves"], [])
        self.assertEqual([u["fact"] for u in sel["unmatched"]], ["pre feeds canary", "canary sees pre"])
        self.assertEqual(sel["shared"], ["e-self"])


class EndpointTest(unittest.TestCase):
    def test_apply_and_restore_are_inverse(self):
        for mv, applied in (({"end": "source", "src": "w", "dst": "x"}, ("n", "x")),
                            ({"end": "target", "src": "x", "dst": "w"}, ("x", "n")),
                            ({"end": "target", "src": "w", "dst": "w"}, ("w", "n"))):
            with self.subTest(mv=mv):
                cs, cd, ts, td = sp.moved_endpoints(mv, "w", "n")
                self.assertEqual(((cs, cd), (ts, td)), ((mv["src"], mv["dst"]), applied))
                cs, cd, ts, td = sp.moved_endpoints(mv, "w", "n", back=True)
                self.assertEqual(((cs, cd), (ts, td)), (applied, (mv["src"], mv["dst"])))


class ProvenanceTest(unittest.TestCase):
    """2026-10-24 opus/fable 战役：fable 抽出的 036063e2 被并进 opus 建的 d2091eac，拆分时新节点的来源
    抄了误并节点的（opus），差点把这个实体记到 opus 名下，只能手改清单纠正。"""
    FABLE = {"extractor_model": "m-fable", "extraction_mode": "agent", "extractor_tool": "claude-code",
             "run_id": "camp-m-fable"}
    # (uuid, name, labels, group_id, extractor_model, extraction_mode, extractor_tool, run_id)
    WRONG = ("u-w", W, ["Entity", "Concept"], "g", "m-opus", "agent", "claude-code", "camp-m-opus")
    ERR2 = {"id": 2, "status": "error", "model": "m-fable", "executor": "codex", "run_id": "stale-run"}
    LOG = [{"id": 1, "status": "ok", "model": "m-opus", "executor": "claude-code", "run_id": "camp-m-opus"},
           {"id": 2, "status": "ok", "model": "m-fable", "executor": "claude-code", "run_id": "camp-m-fable"},
           ERR2,   # 成功之后又失败的一次重提：不算
           {"id": 3, "status": "ok", "model": "m-fable", "executor": "claude-code", "run_id": "camp-m-fable-later"}]

    def test_plan_takes_provenance_from_the_first_ok_log_entry_of_the_entity(self):
        spec = sp.plan_from_results(RESULTS, {**BATCH, "model": "m-fable"}, W, P, log=self.LOG)
        # 实体最早出现在观测 2（观测 3 的 run 不同）；同一观测失败的那次提交不算
        self.assertEqual(spec["entity"]["provenance"], self.FABLE)
        self.assertEqual(spec["batch_model"], "m-fable")

    def test_plan_without_log_or_ok_entry_leaves_provenance_out(self):
        self.assertNotIn("provenance", sp.plan_from_results(RESULTS, BATCH, W, P)["entity"])
        self.assertNotIn("provenance", sp.plan_from_results(RESULTS, BATCH, W, P, log=[self.LOG[0], self.ERR2])["entity"])

    def test_dry_run_prefers_spec_provenance_over_the_wrong_node(self):
        spec = {"entity": {"name": P, "provenance": self.FABLE}, "batch_model": "m-fable"}
        self.assertEqual(sp.entity_provenance(spec, self.WRONG), (self.FABLE, "spec"))

    def test_without_spec_provenance_copies_wrong_node_only_when_models_agree(self):
        same = sp.entity_provenance({"entity": {"name": P}, "batch_model": "m-opus"}, self.WRONG)
        self.assertEqual(same, ({"extractor_model": "m-opus", "extraction_mode": "agent",
                                 "extractor_tool": "claude-code", "run_id": "camp-m-opus"}, "wrong_node"))
        self.assertEqual(sp.entity_provenance({"entity": {"name": P}}, self.WRONG)[1], "wrong_node")
        with self.assertRaises(SystemExit):   # 跨模型误并：抄过来就记错了，要求 --log
            sp.entity_provenance({"entity": {"name": P}, "batch_model": "m-fable"}, self.WRONG)

    def test_dry_run_manifest_carries_the_chosen_provenance(self):
        class FakeGraph:   # 按查询语句返回固定结果，只认 dry_run 用到的只读查询
            def __init__(self, rows):
                self.rows = rows

            def ro_query(self, q, p, timeout=None):
                if q == sp.FIND_NODE:
                    rows = [ProvenanceTest.WRONG] if p["lname"] == sp._key(W) else []
                else:
                    rows = self.rows.get(q, [])
                return type("R", (), {"result_set": [list(r) for r in rows]})()
        g = FakeGraph({sp.NODE_EDGES: [("e1", "u-w", "u-c", "pre feeds canary", ["ep-2"])],
                       sp.EPISODES: [("ep-2", "obs-2")], sp.EPISODES_BY_NAME: [("ep-2", "obs-2")],
                       sp.MENTIONS: [("ep-2", "m-1", "g", "t0")]})
        spec = sp.plan_from_results(RESULTS, {**BATCH, "model": "m-fable"}, W, P, log=self.LOG)
        spec["episodes"], spec["edges"] = spec["episodes"][:1], spec["edges"][:1]
        m = sp.dry_run(g, spec)
        self.assertEqual((m["entity"]["provenance"], m["entity"]["provenance_source"]), (self.FABLE, "spec"))
        self.assertEqual([x["uuid"] for x in m["edges"]], ["e1"])
        spec["entity"].pop("provenance")
        with self.assertRaises(SystemExit):
            sp.dry_run(g, spec)

    def test_cli_plan_reads_the_log_file(self):
        import contextlib, io, json, tempfile
        with tempfile.TemporaryDirectory() as d:
            files = {"r.json": json.dumps(RESULTS), "b.json": json.dumps({**BATCH, "model": "m-fable"}),
                     "log.jsonl": "\n".join(json.dumps(r) for r in self.LOG) + "\n\n"}
            for name, text in files.items():
                _Path(d, name).write_text(text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = sp.main(["--plan", f"{d}/r.json", "--batch", f"{d}/b.json", "--wrong", W, "--entity", P,
                              "--log", f"{d}/log.jsonl"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["entity"]["provenance"], self.FABLE)


if __name__ == "__main__":
    unittest.main()
