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


if __name__ == "__main__":
    unittest.main()
