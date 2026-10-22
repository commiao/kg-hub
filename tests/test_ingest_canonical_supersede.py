"""tools/ingest_canonical_docs：重新入库后归档旧版，但新版抽取落空时不归档。

2026-10-22 只读核查：add_episode 每次都新建同名 Episodic，旧版从不归档，9 篇胶囊有
19 个未归档节点。最新版里 ONBOARDING / OBSERVATION-PHASE / ROADMAP 一条 RELATES_TO
都没抽出来，DESIGN 从 77 条掉到 16 条（入库时抽取断路器断开）。若那几次入库后直接
归档旧版，/api/search 会少召回 289 条事实（背书全部归档的边不召回）。
伪驱动，不连库。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import argparse
import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

try:
    from tools import ingest_canonical_docs as M
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False


class _Driver:
    def __init__(self, versions):
        self.versions = versions  # 线上形态：[{uuid, uc, last, edges}]
        self.calls = []

    async def execute_query(self, cypher, **params):
        self.calls.append((cypher, params))
        if "OPTIONAL MATCH" in cypher:
            return self.versions, None, None
        if "superseded_by" in cypher:
            return [{"c": len(self.versions)}], None, None
        return [], None, None

    def find(self, needle):
        return [p for c, p in self.calls if needle in c]


class _G:
    def __init__(self, versions):
        self.driver = _Driver(versions)


@unittest.skipUnless(HAVE_DEPS, "需要 graphiti_core（镜像 / 项目 venv 里有）")
class ShouldSupersede(unittest.TestCase):
    def test_measured_failures_are_held_back(self):
        self.assertFalse(M.should_supersede(0, 54))    # ROADMAP 10-14
        self.assertFalse(M.should_supersede(0, 49))    # ONBOARDING 10-14
        self.assertFalse(M.should_supersede(16, 77))   # DESIGN 10-16

    def test_measured_healthy_reextractions_supersede(self):
        self.assertTrue(M.should_supersede(12, 15))    # OBSERVATION-PHASE 0.8×
        self.assertTrue(M.should_supersede(67, 24))    # DESIGN 2.8×
        self.assertTrue(M.should_supersede(23, 22))

    def test_boundary_and_no_previous_edges(self):
        self.assertTrue(M.should_supersede(8, 16))
        self.assertFalse(M.should_supersede(7, 16))
        self.assertTrue(M.should_supersede(5, 0))
        self.assertTrue(M.should_supersede(5, None))
        self.assertFalse(M.should_supersede(0, None))


@unittest.skipUnless(HAVE_DEPS, "需要 graphiti_core（镜像 / 项目 venv 里有）")
class ReadMeta(unittest.TestCase):
    def test_takes_max_over_live_versions(self):
        g = _G([
            {"uuid": "d1", "uc": 549, "last": "2026-10-01", "edges": 24},
            {"uuid": "d3", "uc": 553, "last": "2026-10-20", "edges": 77},
            {"uuid": "d4", "uc": 555, "last": None, "edges": 16},
        ])
        meta = asyncio.run(M.read_capsule_meta(g, "kg-hub-canonical-DESIGN"))
        self.assertEqual(meta, {"usage_count": 555, "last_used_at": "2026-10-20",
                                "versions": 3, "best_edges": 77})
        cypher, params = g.driver.calls[0]
        self.assertIn("NOT coalesce(n.archived, false)", cypher)
        self.assertEqual(params["name"], "kg-hub-canonical-DESIGN")


@unittest.skipUnless(HAVE_DEPS, "需要 graphiti_core（镜像 / 项目 venv 里有）")
class IngestFlow(unittest.TestCase):
    """_do_main：新版抽取够了才归档旧版并记水位线；落空则保留旧版、不记水位线。"""

    def run_main(self, versions, new_edges):
        g = _G(versions)
        with tempfile.TemporaryDirectory() as d:
            wm_path = _Path(d) / "wm.json"
            args = argparse.Namespace(only="kg-hub-canonical-ROADMAP", list=False, fresh=False)
            with patch.object(M, "WATERMARK_PATH", wm_path), \
                    patch.object(M, "build_graphiti", AsyncMock(return_value=g)), \
                    patch.object(M, "ingest_one", AsyncMock(return_value=(10, new_edges, "new-uuid"))):
                rc = asyncio.run(M._do_main(args))
            wm = json.loads(wm_path.read_text()) if wm_path.exists() else {}
        return rc, g.driver, wm

    OLD = [{"uuid": "r1", "uc": 85, "last": None, "edges": 54},
           {"uuid": "r2", "uc": 80, "last": None, "edges": 0}]

    def test_healthy_reingest_archives_old_versions(self):
        rc, drv, wm = self.run_main(self.OLD, new_edges=50)
        self.assertEqual(rc, 0)
        archived = drv.find("superseded_by")
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["keep"], "new-uuid")
        self.assertEqual(archived[0]["name"], "kg-hub-canonical-ROADMAP")
        self.assertIn("kg-hub-canonical-ROADMAP", wm)

    def test_empty_extraction_keeps_old_versions_and_watermark(self):
        rc, drv, wm = self.run_main(self.OLD, new_edges=0)
        self.assertEqual(rc, 0)
        self.assertEqual(drv.find("superseded_by"), [])
        self.assertNotIn("kg-hub-canonical-ROADMAP", wm)  # 下次直接重跑就会重试

    def test_usage_restored_onto_new_node_only(self):
        _, drv, _ = self.run_main(self.OLD, new_edges=50)
        persisted = drv.find("n.usage_count = $uc")
        self.assertEqual(len(persisted), 1)
        cypher = next(c for c, _ in drv.calls if "n.usage_count = $uc" in c)
        self.assertIn("{uuid: $uuid}", cypher)  # 按 name 会把同名旧版一起改
        self.assertEqual(persisted[0]["uuid"], "new-uuid")
        self.assertNotIn("name", persisted[0])
        self.assertEqual(persisted[0]["uc"], 85)

    def test_first_ingest_archives_nothing(self):
        _, drv, wm = self.run_main([], new_edges=30)
        self.assertEqual(drv.find("superseded_by"), [])
        self.assertIn("kg-hub-canonical-ROADMAP", wm)


@unittest.skipUnless(HAVE_DEPS, "需要 graphiti_core（镜像 / 项目 venv 里有）")
class ArchiveQuery(unittest.TestCase):
    def test_keeps_new_version_and_is_reversible(self):
        g = _G([{"uuid": "a"}, {"uuid": "b"}])
        n = asyncio.run(M.archive_superseded(g, "kg-hub-canonical-DESIGN", "keep-me"))
        self.assertEqual(n, 2)
        cypher, params = g.driver.calls[0]
        self.assertIn("n.uuid <> $keep", cypher)
        self.assertIn("SET n.archived = true", cypher)
        self.assertNotIn("DELETE", cypher.upper())
        self.assertEqual(params["keep"], "keep-me")


if __name__ == "__main__":
    unittest.main()
