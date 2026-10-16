"""extractor_compare：按 extractor_model / legacy 月份分组的并排指标，口径与报告第 3 节一致。"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import unittest

from tools import extractor_compare as xc


class AggregateTest(unittest.TestCase):
    def test_groups_and_metrics(self):
        eps = [{"src": "legacy:2026-10", "eps": 2, "mentions": 20, "edges": 18, "zero_entities": 0},
               {"src": "claude-haiku-5-5", "eps": 4, "mentions": 24, "edges": 16, "zero_entities": 1}]
        edges = [{"src": "legacy:2026-10", "name": "binds", "count": 10, "dups": 2},
                 {"src": "legacy:2026-10", "name": "belongs_to", "count": 8, "dups": 0},
                 {"src": "claude-haiku-5-5", "name": "relates_to", "count": 16, "dups": 0}]
        ents = [{"src": "legacy:2026-10", "name": "commit 94a9779", "summary_len": 10, "l": ["Entity", "Tool"]},
                {"src": "legacy:2026-10", "name": "kg-hub", "summary_len": 90, "l": ["Entity"]},
                {"src": "claude-haiku-5-5", "name": "FalkorDB", "summary_len": 60, "l": ["Entity", "Tool"]}]
        t = xc.aggregate(eps, edges, ents)
        lg, hk = t["legacy:2026-10"], t["claude-haiku-5-5"]
        self.assertEqual((lg["entities_per_episode"], lg["edges_per_episode"]), (10.0, 9.0))
        self.assertAlmostEqual(lg["edge_in_schema"], 8 / 18, places=3)
        self.assertAlmostEqual(lg["dup_triple_share"], 2 / 18, places=3)
        self.assertEqual((lg["untyped_share"], lg["dirty_name_share"], lg["summary_len_avg"]), (0.5, 0.5, 50.0))
        self.assertEqual((hk["edge_in_schema"], hk["zero_entity_share"], hk["edge_distinct_names"]), (1.0, 0.25, 1))
        md = xc.render(t)
        self.assertTrue(md.splitlines()[2].startswith("| legacy:2026-10 |"))
        self.assertIn("| claude-haiku-5-5 | 4 | 6.0 | 4.0 | 25.0% | 100.0% | 1 |", md)


if __name__ == "__main__":
    unittest.main()
