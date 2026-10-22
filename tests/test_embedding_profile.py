"""向量模型档案与换模型工具（T-0339）：查询向量与库向量必须同源，靠代码拦，不靠人记得。

不碰网络与图：图驱动用替身，向量计算不跑。"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime

from utils import embedding_profile as ep
from tools import reembed as rb

MINILM = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class FakeDriver:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def execute_query(self, cypher, **params):
        self.calls.append(cypher)
        return self.rows, None, None


class ProfileTest(unittest.TestCase):
    def test_default_is_current_model_and_unknown_fails_closed(self):
        self.assertEqual(ep.active_model({}), "BAAI/bge-small-en-v1.5")
        self.assertEqual(ep.active_model({ep.ENV_VAR: "  "}), "BAAI/bge-small-en-v1.5")
        self.assertEqual(ep.active_model({ep.ENV_VAR: MINILM}), MINILM)
        with self.assertRaises(ep.EmbeddingProfileError):
            ep.active_model({ep.ENV_VAR: "BAAI/bge-small-zh-v1.5"})   # 评估过但未登记 → 不许用

    def test_every_profile_is_complete(self):
        for name, p in ep.PROFILES.items():
            self.assertEqual(p.model, name)
            self.assertGreater(p.dim, 0)
            self.assertTrue(0.5 < p.node_match_min < 1.0, name)
        # 现状门槛不许被顺手改动：它是 10-17 三轮复盘校准出来的
        self.assertEqual(ep.PROFILES["BAAI/bge-small-en-v1.5"].node_match_min, 0.95)
        # MiniLM 的门槛来自 2026-10-20 全图校准（复用成功率与现状持平），不是沿用 0.95：
        # 沿用的话复用率从 42.8% 掉到 16.7%，同一事物会被拆成多个节点。
        self.assertEqual(ep.PROFILES[MINILM].node_match_min, 0.89)

    def test_dim_must_match_model(self):
        p = ep.PROFILES["BAAI/bge-small-en-v1.5"]
        ep.check_dim(p, {"EMBEDDING_DIM": "384"})
        ep.check_dim(p, {})                                       # 没给就由档案补
        with self.assertRaises(ep.EmbeddingProfileError):
            ep.check_dim(p, {"EMBEDDING_DIM": "512"})

    def test_graph_marker_absent_means_default_model(self):
        self.assertEqual(ep.graph_model_from_rows([]), ep.DEFAULT_MODEL)
        self.assertEqual(ep.graph_model_from_rows([{"model": MINILM}]), MINILM)

    def test_mismatch_refuses_both_directions(self):
        # 配置换了、数据没重算
        d = FakeDriver([])
        with self.assertRaises(ep.EmbeddingProfileError) as cm:
            asyncio.run(ep.assert_graph_profile(d, {ep.ENV_VAR: MINILM}))
        self.assertIn("reembed", str(cm.exception))
        # 数据重算了、配置没换（或回退时只回退了数据）
        d = FakeDriver([{"model": MINILM}])
        with self.assertRaises(ep.EmbeddingProfileError):
            asyncio.run(ep.assert_graph_profile(d, {}))
        # 一致才放行，且确实查过标记
        d = FakeDriver([{"model": MINILM}])
        self.assertEqual(asyncio.run(ep.assert_graph_profile(d, {ep.ENV_VAR: MINILM})).model, MINILM)
        self.assertIn("EmbeddingProfile", d.calls[0])
        self.assertEqual(asyncio.run(ep.assert_graph_profile(FakeDriver([]), {})).model, ep.DEFAULT_MODEL)

    def test_node_match_min_comes_from_profile(self):
        from utils import extracted_ingest as xi
        self.assertEqual(xi.NODE_MATCH_MIN, ep.active_profile().node_match_min)

    def test_build_graphiti_checks_profile_before_building(self):
        src = (_Path(__file__).resolve().parent.parent / "graphiti_client.py").read_text()
        body = src[src.index("async def build_graphiti("):]
        self.assertLess(body.index("assert_graph_profile(driver)"), body.index("g = Graphiti("))
        self.assertIn('kwargs = {"model_name": _EMBED_PROFILE.model}', src)
        self.assertNotIn('"BAAI/bge-small-en-v1.5"}', src)      # 模型名不许再写死


class ReembedPureTest(unittest.TestCase):
    def test_write_window_crosses_midnight_and_avoids_0200(self):
        at = lambda h, m: datetime(2026, 10, 20, h, m)
        for ok in [(23, 0), (23, 59), (0, 30), (1, 44)]:
            self.assertTrue(rb.in_window(at(*ok)), ok)
        for no in [(22, 59), (1, 45), (2, 0), (12, 0)]:
            self.assertFalse(rb.in_window(at(*no)), no)

    def test_plan_updates_recomputes_new_and_changed_only(self):
        cur = {"a": "h1", "b": "h2", "c": "h3"}
        ck = {"a": "h1", "b": "OLD", "gone": "h9"}
        redo, ready = rb.plan_updates(cur, ck)
        self.assertEqual(redo, ["b", "c"])     # 文本改了的、检查点之后新增的
        self.assertEqual(ready, ["a"])         # 线上已删除的 gone 不出现

    def test_batches_and_hash(self):
        self.assertEqual(list(rb.batches(list(range(5)), 2)), [[0, 1], [2, 3], [4]])
        self.assertEqual(rb.text_hash("x"), rb.text_hash("x"))
        self.assertNotEqual(rb.text_hash("x"), rb.text_hash("y"))
        self.assertEqual(rb.text_hash(None), rb.text_hash(""))
        self.assertEqual(rb.model_slug(MINILM), "sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2")

    def test_falkordb_settings_reads_only_its_own_keys(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("KG_HUB_FALKORDB_PASSWORD=pw\nexport KG_HUB_FALKORDB_PORT=6380\n"
                    "KG_HUB_MODEL_GATEWAY_TOKEN=gw\nOTHER_KEY=nope\n")
        s = rb.falkordb_settings(env={"KG_HUB_FALKORDB_HOST": "h"}, files=(f.name,))
        self.assertEqual(s, {"KG_HUB_FALKORDB_HOST": "h", "KG_HUB_FALKORDB_PASSWORD": "pw",
                             "KG_HUB_FALKORDB_PORT": "6380"})
        self.assertEqual(set(s) - set(rb.FALKOR_KEYS), set())       # 别的键一个都不拿
        self.assertEqual(rb.falkordb_settings(env={"KG_HUB_FALKORDB_PASSWORD": "env"}, files=(f.name,))
                         ["KG_HUB_FALKORDB_PASSWORD"], "env")         # 环境变量优先

    def test_write_cypher_uses_indexed_uuid_and_vecf32(self):
        for q in (rb.WRITE_NODES, rb.WRITE_EDGES):
            self.assertIn("{uuid: r.u}", q)
            self.assertIn("vecf32(r.v)", q)
        self.assertIn("name_embedding", rb.WRITE_NODES)
        self.assertIn("fact_embedding", rb.WRITE_EDGES)

    def test_write_requires_yes_window_and_expected_source(self):
        class G:
            def __init__(self, model): self.model = model
            def ro_query(self, q, *a):
                class R: pass
                r = R(); r.result_set = [[self.model]] if self.model else []
                return r
        args = lambda **k: type("A", (), {"allow_outside_window": True, "yes": True, **k})()
        rb._guard_write(args(), G(None), ep.DEFAULT_MODEL)                       # 通过
        with self.assertRaises(SystemExit):
            rb._guard_write(args(yes=False), G(None), ep.DEFAULT_MODEL)          # 没 --yes
        with self.assertRaises(SystemExit):
            rb._guard_write(args(), G(MINILM), ep.DEFAULT_MODEL)                 # 图里已是别的模型
        night = rb.in_window(datetime.now(rb.TZ))
        if not night:
            with self.assertRaises(SystemExit):
                rb._guard_write(args(allow_outside_window=False), G(None), ep.DEFAULT_MODEL)


class ReembedRestoreGuardTest(unittest.TestCase):
    """restore 只在「备份后图里没有新增带向量的条目」时可用：硬拦，不靠文档。
    备份之后入图的条目只有切换后模型的向量，restore 改不到它们，标记却会改回旧模型。"""

    BACKUP = {"nodes": ["n1", "n2", "n3"], "edges": ["e1", "e2"]}

    class FakeGraph:
        def __init__(self, live, model=MINILM):
            self.live, self.model, self.writes = {k: list(v) for k, v in live.items()}, model, []

        def ro_query(self, q, params=None):
            class R: pass
            r = R()
            if q == ep.MARKER_READ:
                r.result_set = [[self.model]]
                return r
            kind = {rb.READ_NODE_VEC_UUIDS: "nodes", rb.READ_EDGE_VEC_UUIDS: "edges"}[q]
            r.result_set = [[u] for u in self.live[kind][params["s"]:params["s"] + params["l"]]]
            return r

        def query(self, q, params=None):
            self.writes.append(q)

    def setUp(self):
        import tempfile
        import numpy as np
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = _Path(self.tmp.name)
        for kind, uu in self.BACKUP.items():
            rb.save_ckpt(d / f"backup-{kind}.npz", uu, [""] * len(uu), np.ones((len(uu), 4), np.float32))
        (d / "backup-model.txt").write_text(ep.DEFAULT_MODEL)
        self.args = type("A", (), {"dir": str(d), "url": "http://nas", "batch": 2, "sleep": 0, "yes": True,
                                   "allow_outside_window": True, "expect_from": MINILM})()

    def run_restore(self, g, on_quiesce=None):
        """跑 cmd_restore，返回 (退出信息或 None, 写过向量的批次种类, 排空调用的秒数序列)。"""
        from unittest import mock
        written, drains = [], []

        def fake_write(g_, kind, uuids, vecs, *a):
            written.append((kind, list(map(str, uuids))))
            return len(uuids)

        def fake_wait_idle(*a):
            if on_quiesce:
                on_quiesce()

        with mock.patch.object(rb, "graph", return_value=g), \
                mock.patch("tools.agent_extract.api_token", return_value="t"), \
                mock.patch.object(rb, "_drain", side_effect=lambda b, t, sec: drains.append(sec) or {}), \
                mock.patch.object(rb, "_wait_idle", side_effect=fake_wait_idle), \
                mock.patch.object(rb, "_write_vectors", side_effect=fake_write):
            try:
                with redirect_stdout(io.StringIO()):
                    rc = rb.cmd_restore(self.args)
                return rc, written, drains
            except SystemExit as exc:
                return exc.code, written, drains

    def test_outside_backup_is_computed_from_live_vs_backup(self):
        self.assertEqual(rb.vectors_outside_backup(["a", "b", "c"], ["a", "b"]), ["c"])
        self.assertEqual(rb.vectors_outside_backup(["a"], ["a", "gone"]), [])   # 线上已删的无妨
        self.assertEqual(rb.vectors_outside_backup([], []), [])

    def test_restore_runs_when_nothing_new_since_backup(self):
        # 备份里的 n3 / e2 之后被删了：不影响
        g = self.FakeGraph({"nodes": ["n1", "n2"], "edges": ["e1"]})
        rc, written, drains = self.run_restore(g)
        self.assertEqual(rc, 0)
        self.assertEqual([k for k, _ in written], ["nodes", "edges"])
        self.assertEqual(drains[-1], 0)                 # 排空已解除
        self.assertTrue(any("EmbeddingProfile" in q or "DELETE" in q for q in g.writes))   # 标记改回

    def test_restore_refuses_new_entity_before_touching_anything(self):
        g = self.FakeGraph({"nodes": ["n1", "n2", "n3", "n_new"], "edges": ["e1", "e2"]})
        rc, written, drains = self.run_restore(g)
        self.assertIsInstance(rc, str)
        self.assertIn("nodes 1 条", rc)
        self.assertIn("n_new", rc)
        self.assertIn("compute --model " + ep.DEFAULT_MODEL, rc)
        self.assertIn("write --model " + ep.DEFAULT_MODEL, rc)
        self.assertEqual(written, [])
        self.assertEqual(drains, [])                    # 连线上都没去排空
        self.assertEqual(g.writes, [])                  # 标记也没动

    def test_restore_refuses_new_edge(self):
        g = self.FakeGraph({"nodes": ["n1"], "edges": ["e1", "e_new_1", "e_new_2"]})
        rc, written, _ = self.run_restore(g)
        self.assertIn("edges 2 条", rc)
        self.assertNotIn("nodes", rc)
        self.assertEqual(written, [])

    def test_restore_rechecks_after_drain_quiesces(self):
        # 第一遍查时干净，排空等静前最后一刻又入了图：写任何一条之前都要再拦一次，并解除排空
        g = self.FakeGraph({"nodes": ["n1"], "edges": ["e1"]})
        rc, written, drains = self.run_restore(g, on_quiesce=lambda: g.live["edges"].append("e_late"))
        self.assertIn("edges 1 条", rc)
        self.assertEqual(written, [])
        self.assertEqual(drains[-1], 0)
        self.assertEqual(g.writes, [])

    def test_restore_still_needs_expected_source(self):
        g = self.FakeGraph({"nodes": ["n1"], "edges": ["e1"]}, model=ep.DEFAULT_MODEL)
        rc, written, _ = self.run_restore(g)            # 图里已是备份模型，expect_from 对不上
        self.assertIn("不符", rc)
        self.assertEqual(written, [])


if __name__ == "__main__":
    unittest.main()
