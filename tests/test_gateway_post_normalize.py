"""网关路径入图后归一：边名归 schema、无类型兜底、脏命名降级、来源标记——确定性，不调模型。"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from utils import gateway_post_normalize as gp

EDGE = {"caused_by", "fixed_by", "references", "belongs_to", "relates_to", "depends_on"}
ENT = {"Person", "Project", "File", "Tool", "Concept", "Issue", "Fix", "Config", "Lesson"}
NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


class PlanTest(unittest.TestCase):
    def test_edge_renames(self):
        r = gp.plan_edge_renames([{"uuid": "a", "name": "BELONGS_TO"}, {"uuid": "b", "name": "IS_ANCESTOR_OF"},
                                  {"uuid": "c", "name": "references"}], EDGE)
        self.assertEqual([(x["uuid"], x["name"], x["raw"], x["in_schema"]) for x in r],
                         [("a", "belongs_to", "BELONGS_TO", True), ("b", "relates_to", "IS_ANCESTOR_OF", False)])

    def test_fallback_type_and_dirty(self):
        self.assertEqual(gp.fallback_type("deploy/nas/release.sh"), "File")
        self.assertEqual(gp.fallback_type("KG_HUB_MODEL_GATEWAY_TOKEN"), "Config")
        self.assertEqual(gp.fallback_type("docker-compose.yml"), "Config")
        self.assertEqual(gp.fallback_type("断路器"), "Concept")
        self.assertTrue(gp.is_dirty_name("commit 94a9779"))
        self.assertTrue(gp.is_dirty_name("/Users/mac/work-ai/kg-hub"))
        self.assertTrue(gp.is_dirty_name("T-0046"))
        self.assertFalse(gp.is_dirty_name("熔断器改造（T-0086）"))

    def test_plan_node_fixes_splits_new_and_existing(self):
        nodes = [{"uuid": "n1", "name": "kg-hub", "labels": ["Entity", "Project"], "created_at": NOW},
                 {"uuid": "n2", "name": "deploy/x.py", "labels": ["Entity"], "created_at": NOW},
                 {"uuid": "n3", "name": "commit 94a9779", "labels": ["Entity", "Tool"], "created_at": NOW + timedelta(seconds=5)},
                 {"uuid": "n4", "name": "T-0046", "labels": ["Entity", "Issue"], "created_at": (NOW - timedelta(days=30)).isoformat()}]
        p = gp.plan_node_fixes(nodes, ENT, since=NOW)
        self.assertEqual(p.type_fallback, {"File": ["n2"]})
        self.assertEqual((p.dirty_new, p.dirty_existing), (["n3"], ["n4"]))


class FakeDriver:
    def __init__(self, others=0):
        self.calls = []; self.others = others

    async def execute_query(self, cypher, **params):
        self.calls.append((cypher, params))
        if cypher.startswith("MATCH (n:Entity {uuid: $u}) OPTIONAL MATCH"):
            return [{"others": self.others}], None, None
        if cypher.startswith("MATCH (n:Entity {uuid: $u})-[r:RELATES_TO]"):
            return [{"uuid": "e-dirty"}], None, None
        return [{"c": 1}], None, None


class ApplyTest(unittest.TestCase):
    def _run(self, others):
        d = FakeDriver(others)
        edges = [{"uuid": "e1", "name": "BINDS"}, {"uuid": "e2", "name": "references"}]
        nodes = [{"uuid": "n2", "name": "deploy/x.py", "labels": ["Entity"], "created_at": NOW},
                 {"uuid": "n3", "name": "commit 94a9779", "labels": ["Entity", "Tool"], "created_at": NOW}]
        st = asyncio.run(gp.post_ingest_normalize(d, episode_uuid="ep", edges=edges, nodes=nodes, edge_types=EDGE,
                                                  entity_types=ENT, since=NOW, extractor_label="gateway:deepseek"))
        return d, st

    def test_removes_new_dirty_node_only_when_no_other_mentions(self):
        d, st = self._run(others=0)
        self.assertEqual((st["edges_renamed"], st["edges_offschema"], st["nodes_typed"]), (1, 1, 1))
        self.assertEqual((st["dirty_removed"], st["dirty_marked"], st["edges_unlinked"]), (1, 0, 1))
        cyphers = [c for c, _ in d.calls]
        self.assertTrue(any("SET n:File" in c for c in cyphers))
        self.assertTrue(any(c.startswith("MATCH (n:Entity {uuid: $u}) DETACH DELETE") for c in cyphers))
        unlink = [p for c, p in d.calls if c.startswith("MATCH (e:Episodic {uuid: $ep})")][0]
        self.assertEqual(unlink["gone"], ["e-dirty"])
        tag = [p for c, p in d.calls if "extraction_mode = 'gateway'" in c][0]
        self.assertEqual(tag["label"], "gateway:deepseek")
        rename = [p for c, p in d.calls if c.startswith("UNWIND $items")][0]
        self.assertEqual(rename["items"], [{"uuid": "e1", "name": "relates_to", "raw": "BINDS"}])

    def test_marks_instead_of_removing_when_shared(self):
        d, st = self._run(others=2)
        self.assertEqual((st["dirty_removed"], st["dirty_marked"]), (0, 1))
        self.assertFalse(any(c.startswith("MATCH (n:Entity {uuid: $u}) DETACH DELETE") for c, _ in d.calls))


if __name__ == "__main__":
    unittest.main()
