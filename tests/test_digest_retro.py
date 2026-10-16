"""digest_retro：每 500 条复盘的门禁、指标、调参登记与最终对比必须可核验。

用户要求（2026-10-17）：消化时每 500 条自行总结经验教训并用到下一轮，最终对比要列出
实践过的优化。这里保证：门禁按账本 succeeded 计数触发；复盘记录 from→to；没实现的旋钮
不能冒充「已应用」；最终报告能把每次调参和下一轮指标变化对上。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools import digest_retro as dr


def _journal(path: Path, tasks):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE task_executions (task_id TEXT, execution_ordinal INTEGER,
        execution_id TEXT, source_description TEXT, source_obs_id TEXT, manual_command_id TEXT,
        state TEXT, reason TEXT, started_at TEXT, finished_at TEXT)""")
    db.execute("""CREATE TABLE model_attempts (idempotency_key TEXT PRIMARY KEY, business_key TEXT,
        source_description TEXT, source_obs_id TEXT, step_id TEXT, request_digest TEXT, phase TEXT,
        provider_call_started INTEGER, result_json TEXT, gateway_identity TEXT, gateway_http_status INTEGER,
        http_started_at TEXT, stage TEXT, created_at TEXT, updated_at TEXT)""")
    for i, (state, a, b, calls) in enumerate(tasks):
        sd, sid = "claude-mem obs", f"obs{i}"
        db.execute("INSERT INTO task_executions VALUES (?,1,?,?,?,NULL,?,NULL,?,?)",
                   (f"t{i}", f"e{i}", sd, sid, state, a, b))
        for c in range(calls):
            db.execute("INSERT INTO model_attempts (idempotency_key, business_key, source_description, "
                       "source_obs_id, step_id, request_digest, phase, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       (f"k{i}-{c}", "kg_hub.entity_extract", sd, sid, f"s{c}", "d", "completed", a, b))
    db.commit(); db.close()


class MetricsTest(unittest.TestCase):
    def test_graph_metrics_schema_case_insensitive_and_dirty_names(self):
        eps = [{"entities": 10, "edges": 8}, {"entities": 4, "edges": 0}]
        edges = [{"name": "belongs_to", "count": 5, "dups": 1}, {"name": "BINDS", "count": 3, "dups": 0}]
        ents = [{"name": "sd-server", "summary_len": 100, "typed": True},
                {"name": "commit 94a9779", "summary_len": 10, "typed": False}]
        m = dr.graph_metrics(eps, edges, ents)
        self.assertEqual(m["edges_per_episode"], 4.0)
        self.assertEqual(m["zero_edge_share"], 0.5)
        self.assertAlmostEqual(m["edge_in_schema"], 5 / 8)
        self.assertAlmostEqual(m["dup_triple_share"], 1 / 8)
        self.assertEqual(m["dirty_name_share"], 0.5)
        self.assertEqual(m["untyped_share"], 0.5)

    def test_task_metrics_wall_and_calls(self):
        tasks = [{"state": "succeeded", "started_at": "2026-10-17T00:00:00+00:00",
                  "finished_at": "2026-10-17T00:05:00+00:00", "calls": 6},
                 {"state": "succeeded", "started_at": "2026-10-17T00:00:00+00:00",
                  "finished_at": "2026-10-17T00:01:00+00:00", "calls": 4},
                 {"state": "uncertain", "started_at": "2026-10-17T00:00:00+00:00", "finished_at": None}]
        m = dr.task_metrics(tasks)
        self.assertEqual(m["succeeded"], 2)
        self.assertEqual(m["wall_med_s"], 180.0)
        self.assertEqual(m["calls_mean"], 5.0)
        self.assertAlmostEqual(m["uncertain_share"], 1 / 3, places=3)

    def test_findings_point_at_registered_knobs(self):
        f = dr.derive_findings({"edge_in_schema": 0.36, "dirty_name_share": 0.043, "uncertain_share": 0.7})
        knobs = {x["knob"] for x in f}
        self.assertEqual(knobs, {"normalize_edge_names", "entity_name_filter", "live_per_cycle"})
        self.assertTrue(all(x["knob"] in dr.KNOBS for x in f))
        unimpl = [x for x in f if not x["knob_implemented"]]
        self.assertEqual([x["knob"] for x in unimpl], ["entity_name_filter"])
        self.assertEqual(dr.derive_findings({"edge_in_schema": 0.9}), [])


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "retro.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_gate_due_only_after_every(self):
        st = dr.RetroState(self.state, every=500)
        self.assertFalse(st.due(499))
        self.assertTrue(st.due(500))

    def test_checkpoint_records_from_to_and_rejects_unimplemented(self):
        st = dr.RetroState(self.state, every=2)
        with self.assertRaises(ValueError):
            st.checkpoint(succeeded_since=2, metrics={}, lessons="x", adjustments={"entity_name_filter": True})
        with self.assertRaises(ValueError):
            st.checkpoint(succeeded_since=2, metrics={}, lessons="x", adjustments={"made_up": 1})
        r1 = st.checkpoint(succeeded_since=2, metrics={"edge_in_schema": 0.3}, lessons="边名太乱",
                           adjustments={"normalize_edge_names": True, "live_per_cycle": 20},
                           now="2026-10-17T01:00:00+00:00")
        self.assertEqual(r1["adjustments"]["live_per_cycle"], {"from": None, "to": 20})
        st.save()
        st2 = dr.RetroState(self.state)
        self.assertEqual(st2.tuning, {"normalize_edge_names": True, "live_per_cycle": 20})
        self.assertEqual(st2.window_since(), "2026-10-17T01:00:00+00:00")
        r2 = st2.checkpoint(succeeded_since=2, metrics={"edge_in_schema": 0.55}, lessons="好转",
                            adjustments={"live_per_cycle": 30}, applied=["normalize_edge_names 已在轮末跑"],
                            now="2026-10-17T02:00:00+00:00")
        self.assertEqual(r2["adjustments"]["live_per_cycle"], {"from": 20, "to": 30})
        self.assertEqual(r2["window"]["since"], "2026-10-17T01:00:00+00:00")

    def test_final_report_lists_practiced_with_deltas(self):
        st = dr.RetroState(self.state, every=2)
        st.checkpoint(succeeded_since=2, metrics={"edge_in_schema": 0.3, "succeeded": 2}, lessons="边名太乱，先归一",
                      adjustments={"normalize_edge_names": True}, now="2026-10-17T01:00:00+00:00")
        st.checkpoint(succeeded_since=2, metrics={"edge_in_schema": 0.55, "succeeded": 2, "dirty_name_share": 0.05},
                      lessons="合规率回升", adjustments={}, now="2026-10-17T02:00:00+00:00")
        md = dr.render_final(st)
        self.assertIn("normalize_edge_names", md)
        self.assertIn("edge_in_schema: 30.0% → 55.0%", md)
        self.assertIn("entity_name_filter", md)  # 第二轮越线但旋钮未实现 → 列入未实践
        rd = dr.render_round(st.rounds[0])
        self.assertIn("第 1 轮", rd)
        self.assertIn("`normalize_edge_names`: None → True", rd)


class JournalLoaderTest(unittest.TestCase):
    def test_counts_and_calls_from_journal(self):
        with tempfile.TemporaryDirectory() as d:
            j = Path(d) / "model-attempts.sqlite3"
            _journal(j, [("succeeded", "2026-10-17T00:00:00+00:00", "2026-10-17T00:04:00+00:00", 6),
                         ("succeeded", "2026-10-16T00:00:00+00:00", "2026-10-16T00:04:00+00:00", 5),
                         ("uncertain", "2026-10-17T00:00:00+00:00", None, 0)])
            since = "2026-10-16T12:00:00+00:00"
            self.assertEqual(dr.succeeded_since(j, since), 1)
            tasks = dr.load_tasks(j, since)
            self.assertEqual(sorted(t["state"] for t in tasks), ["succeeded", "uncertain"])
            m = dr.task_metrics(tasks)
            self.assertEqual(m["calls_mean"], 6.0)
            self.assertEqual(m["wall_med_s"], 240.0)

    def test_cli_gate_and_checkpoint_no_graph(self):
        with tempfile.TemporaryDirectory() as d:
            j = Path(d) / "model-attempts.sqlite3"
            # 账本任务必须早于「现在」：新状态的 since 是创建时刻，之前的任务不算
            _journal(j, [("succeeded", "2026-10-10T00:00:00+00:00", "2026-10-10T00:04:00+00:00", 6)] * 2)
            state = Path(d) / "retro.json"
            # 新状态的 since 是现在，账本里的旧任务不算 → 不该复盘
            self.assertEqual(dr.main(["gate", "--state", str(state), "--journal", str(j), "--every", "2"]), 0)
            # 首次 gate 必须把状态（窗口起点）落盘，否则每次都从「现在」起数
            self.assertTrue(state.exists())
            first_since = json.loads(state.read_text())["started_at"]
            dr.main(["gate", "--state", str(state), "--journal", str(j)])
            self.assertEqual(json.loads(state.read_text())["started_at"], first_since)
            # 把 started_at 拨回去，让窗口覆盖账本任务
            data = None
            st = dr.RetroState(state, every=2); st.data["started_at"] = "2026-10-01T00:00:00+00:00"; st.save()
            self.assertEqual(dr.main(["gate", "--state", str(state), "--journal", str(j)]), 2)
            with self.assertRaises(SystemExit):  # 经验教训不能为空
                dr.main(["checkpoint", "--state", str(state), "--journal", str(j), "--no-graph"])
            lessons = Path(d) / "l.md"; lessons.write_text("uncertain 为 0，可以放大每轮名额")
            rc = dr.main(["checkpoint", "--state", str(state), "--journal", str(j), "--no-graph",
                          "--lessons-file", str(lessons), "--adjust", "live_per_cycle=40",
                          "--report-dir", d])
            self.assertEqual(rc, 0)
            self.assertTrue((Path(d) / "round-001.md").exists())
            self.assertEqual(dr.RetroState(state).tuning, {"live_per_cycle": 40})
            self.assertEqual(dr.main(["gate", "--state", str(state), "--journal", str(j)]), 0)
            self.assertIsNone(data)


if __name__ == "__main__":
    unittest.main()
