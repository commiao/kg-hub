"""agent_extract 执行器：身份与正文必须和 refinery 一致（幂等键不能分叉）、选批跳过已入库、
提交记录纪律指标、按模型汇总。不碰网络：post/fetch 都注入。"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tools import agent_extract as ax
from utils.ingest_filter import load_config

OBS = {"id": 29995, "project": "credvault", "type": "discovery", "title": "T-0167 closed",
       "subtitle": "premise falsified", "facts": json.dumps(["a", "b"]), "narrative": "n" * 200,
       "concepts": json.dumps(["gateway"]), "files_read": None, "files_modified": None,
       "created_at": "2026-10-16T10:00:00Z", "content_hash": "abc123", "generated_by_model": "x",
       "relevance_count": 0, "platform_source": None}


class IdentityTest(unittest.TestCase):
    def test_identity_matches_refinery_contract(self):
        i = ax.to_identity(OBS)
        self.assertEqual(i["name"], "claude-mem-obs-29995")
        self.assertEqual(i["source_description"],
                         "claude-mem obs id=29995 project=credvault type=discovery platform=_default")
        self.assertEqual(i["source_obs_id"], "abc123")
        self.assertTrue(i["episode_body"].startswith("[DISCOVERY] T-0167 closed\npremise falsified\n\nNarrative:"))
        self.assertIn("Key facts:\n- a\n- b", i["episode_body"])
        self.assertTrue(i["episode_body"].endswith("Project: credvault"))
        no_hash = dict(OBS, content_hash=None, id=7)
        self.assertEqual(ax.to_identity(no_hash)["source_obs_id"], "claude-mem-id-7")

    def test_build_episode_body_mirrors_kg_refinery(self):
        try:
            import kg_refinery  # noqa: WPS433
        except Exception:  # noqa: BLE001 — 本机缺它的运行环境时跳过镜像校验
            self.skipTest("kg_refinery 不可导入")
        self.assertEqual(ax.build_episode_body(OBS), kg_refinery.build_episode_body(OBS))
        p = kg_refinery.to_payload(OBS)
        i = ax.to_identity(OBS)
        for k in ("name", "episode_body", "source_description", "source_obs_id", "reference_time"):
            self.assertEqual(p[k], i[k], k)


class TokenAndRetroTest(unittest.TestCase):
    def test_token_env_then_files(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "envfile"
            f.write_text("# c\nexport KG_HUB_API_TOKEN=\"abc\"\nOTHER=1\n")
            self.assertEqual(ax.api_token({"KG_HUB_API_TOKEN": "fromenv"}, (str(f),)), "fromenv")
            self.assertEqual(ax.api_token({}, (str(f),)), "abc")
            self.assertIsNone(ax.api_token({}, (str(Path(d) / "missing"),)))

    def test_retro_passes_unknown_options_through(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        with unittest.mock.patch.object(ax, "remote_retro",
                                        lambda args, stdin_text=None, runner=runner: (calls.append(args), 0)[1]):
            rc = ax.main(["retro", "gate", "--every", "500"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[-1], ["gate", "--every", "500"])

    def test_remote_retro_forwards_args_bundle_and_rc(self):
        import io
        import tarfile
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw))
            return type("R", (), {"returncode": 2, "stdout": b"due=True\n", "stderr": b""})()
        rc = ax.remote_retro(["gate", "--every", "500"], runner=runner)
        self.assertEqual(rc, 2)
        cmd = calls[0][0]
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("python3 /tmp/retro/digest_retro.py gate --every 500 --state /backup/digest-retro.json", cmd[-1])
        names = tarfile.open(fileobj=io.BytesIO(calls[0][1]["input"])).getnames()
        self.assertEqual(names, ["retro/digest_retro.py"])
        rc = ax.remote_retro(["checkpoint", "--adjust", "live_per_cycle=20"], stdin_text="教训", runner=runner)
        tar = tarfile.open(fileobj=io.BytesIO(calls[1][1]["input"]))
        self.assertEqual(tar.extractfile("retro/lessons.md").read().decode(), "教训")
        self.assertIn("--lessons-file /tmp/retro/lessons.md", calls[1][0][-1])


class RemotePendingTest(unittest.TestCase):
    def test_select_pending_respects_boundary_and_watermark(self):
        import sqlite3
        from tools import agent_extract_remote as rm
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "claude-mem.db"
            c = sqlite3.connect(db)
            c.execute("CREATE TABLE sdk_sessions (memory_session_id TEXT, platform_source TEXT)")
            c.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT, "
                      "type TEXT, title TEXT, subtitle TEXT, facts TEXT, narrative TEXT, concepts TEXT, files_read TEXT, "
                      "files_modified TEXT, created_at TEXT, content_hash TEXT, generated_by_model TEXT, relevance_count INTEGER)")
            for i in range(1, 11):
                c.execute("INSERT INTO observations (id, project, type, title, created_at, content_hash, relevance_count) "
                          "VALUES (?,?,?,?,?,?,0)", (i, "p", "discovery", f"t{i}", "2026-10-01T00:00:00Z", f"h{i}"))
            c.commit(); c.close()
            wm = Path(d) / "watermark.json"
            wm.write_text(json.dumps({"boundary_id": 4, "ingested": [5, 6], "rejected": [7], "held": [8]}))
            r = rm.select_pending(str(db), str(wm), 0, 5, chunk=2)
            self.assertEqual([x["id"] for x in r["rows"]], [9, 10])   # 1-4 归 backlog 线；5-8 已处理/待核验
            self.assertEqual((r["after"], r["boundary"], r["skipped_by_watermark"], r["exhausted"]), (4, 4, 4, True))
            r = rm.select_pending(str(db), str(wm), 8, 1)
            self.assertEqual([x["id"] for x in r["rows"]], [9])
            self.assertFalse(r["exhausted"])
            r = rm.select_pending(str(db), str(Path(d) / "missing.json"), 9, 5)
            self.assertEqual([x["id"] for x in r["rows"]], [10])      # 无水位文件：不排除，只按游标


class SelectTest(unittest.TestCase):
    def test_select_skips_existing_and_rejected(self):
        from unittest import mock
        from types import SimpleNamespace
        cfg = load_config()
        rows = [dict(OBS, id=1, content_hash="h1"), dict(OBS, id=2, content_hash="h2"),
                dict(OBS, id=3, content_hash="h3", type="change"),
                dict(OBS, id=4, content_hash="h4"), dict(OBS, id=5, content_hash="h5"),
                dict(OBS, id=6, content_hash="h6")]
        # 质量闸本身另有测试；这里只验证选批对「闸拒 / 已入库 / 抽取中 / 够数即停」的处理
        fake = lambda obs, c, q: SimpleNamespace(accept=obs["type"] != "change", would_accept=obs["type"] != "change",  # noqa: E731
                                                 score=90.0, threshold=60.0, layer="pass", obs_type=obs["type"], reasons=[])
        with mock.patch.object(ax, "evaluate", fake):
            picked, stats = ax.select_batch(rows, cfg, 2, {"h1": "ok", "h2": "pending"})
        self.assertEqual([p["id"] for p in picked], [4, 5])
        self.assertEqual((stats["already_ok"], stats["pending_elsewhere"], stats["rejected_by_filter"],
                          stats["picked"]), (1, 1, 1, 2))
        self.assertTrue(ax.evaluate(OBS, cfg, None) is not None)  # 真闸可调用
        # 别的执行者判为空并记录过（status=empty）：同样算已处理，不再领
        with mock.patch.object(ax, "evaluate", fake):
            picked, stats = ax.select_batch(rows, cfg, 2, {"h1": "empty", "h4": "empty"})
        self.assertEqual([p["id"] for p in picked], [2, 5])
        self.assertEqual(stats["already_ok"], 2)


class GatherBatchTest(unittest.TestCase):
    """逐段往后扫凑满一批（2026-10-23：游标后 80 条只剩 4 条可抽，领批交出 4 条就停）。"""

    @staticmethod
    def fetch_sparse(every):
        calls = []

        def fetch(after, limit):
            calls.append((after, limit))
            rows = [{"id": i, "title": f"t{i}"} for i in range(after + 1, min(after + 1 + limit, 1000))]
            return {"rows": rows, "boundary": 0, "after": after, "skipped_by_watermark": 0, "exhausted": not rows}
        return fetch, calls

    @staticmethod
    def select(every):
        def sel(rows, cfg, n, existing):
            picked = [{"id": r["id"]} for r in rows if r["id"] % every == 0][:n]
            return picked, {"seen": len(rows), "picked": len(picked)}
        return sel

    def test_keeps_scanning_until_the_batch_is_full(self):
        fetch, calls = self.fetch_sparse(30)
        with unittest.mock.patch.object(ax, "select_batch", self.select(30)), unittest.mock.patch.object(ax, "to_identity", lambda r: r):
            g = ax.gather_batch(0, 5, fetch=fetch, check=lambda idents: {}, cfg={})
        self.assertEqual([x["id"] for x in g["picked"]], [30, 60, 90, 120, 150])
        self.assertEqual(g["last"], 150)                                    # 凑满：游标停在最后挑中的那条
        self.assertGreater(g["stats"]["rounds"], 1)
        self.assertEqual(calls[1][0], calls[0][0] + calls[0][1])            # 下一段从上一段末尾接着扫
        self.assertTrue(g["lookahead"] and g["lookahead"][0]["id"] == 151)

    def test_stops_at_round_limit_or_when_exhausted(self):
        fetch, _ = self.fetch_sparse(500)
        with unittest.mock.patch.object(ax, "select_batch", self.select(500)), unittest.mock.patch.object(ax, "to_identity", lambda r: r):
            g = ax.gather_batch(0, 5, fetch=fetch, check=lambda idents: {}, cfg={}, max_rounds=3)
            self.assertEqual((len(g["picked"]), g["stats"]["rounds"], g["lookahead"]), (0, 3, []))
            self.assertEqual(g["last"], 120)                                # 没凑满：游标停在看过的最后一条
            g = ax.gather_batch(990, 5, fetch=fetch, check=lambda idents: {}, cfg={})
            self.assertEqual((g["seen"], g["picked"]), (9, []))             # 取尽就停
            g = ax.gather_batch(999, 5, fetch=fetch, check=lambda idents: {}, cfg={})
            self.assertEqual((g["seen"], g["last"]), (0, 999))

    def test_held_rows_are_skipped_and_counted(self):
        fetch, _ = self.fetch_sparse(1)
        with unittest.mock.patch.object(ax, "select_batch", self.select(1)), unittest.mock.patch.object(ax, "to_identity", lambda r: r):
            g = ax.gather_batch(0, 3, fetch=fetch, check=lambda idents: {}, cfg={}, held={1, 2})
        self.assertEqual([x["id"] for x in g["picked"]], [3, 4, 5])
        self.assertEqual(g["stats"]["held_elsewhere"], 2)


class SubmitTest(unittest.TestCase):
    def setUp(self):
        # These fixtures exercise the pre-v5 transport contract. New receipts have
        # separate end-to-end tests, without weakening the production default.
        patch = unittest.mock.patch.object(ax, "PROMPT_VERSION", "agent-v4")
        patch.start()
        self.addCleanup(patch.stop)

    def test_dry_run_accumulates_entities_new_in_this_batch(self):
        """2026-10-22 补交 b0024：dry-run 逐条把前面条目会新建的实体带给服务端，同批内误并才看得见。"""
        batch = {"batch_id": "b0001", "items": [ax.to_identity(OBS), ax.to_identity(dict(OBS, id=2, content_hash="h2"))]}
        results = {"items": [
            {"id": 29995, "entities": [{"name": "validate 阶段计数", "type": "Concept", "summary": "s"}], "edges": []},
            {"id": 2, "entities": [{"name": "validate阶段计数", "type": "Concept", "summary": "s"}], "edges": []},
        ]}
        posted = []

        def fake_post(method, url, body, token):
            posted.append(body)
            if len(posted) == 1:
                return 200, {"status": "dry_run", "resolved": {"validate 阶段计数": {"reused": False}},
                             "new_entities": [{"name": "validate 阶段计数", "type": "Concept"}]}
            return 200, {"status": "dry_run", "new_entities": [],
                         "resolved": {"validate阶段计数": {"reused": True, "matched_name": "validate 阶段计数",
                                                         "in_batch": True}}}
        recs = ax.submit_results(batch, results, model="m", session="s", url="http://x", token="t",
                                 post=fake_post, dry_run=True)
        self.assertNotIn("dry_run_pending", posted[0])
        self.assertEqual(posted[1]["dry_run_pending"], [{"name": "validate 阶段计数", "type": "Concept"}])
        self.assertEqual(recs[1]["reuse_mismatch"], ["validate阶段计数 → validate 阶段计数（同批新建）"])
        # 正式提交不带 pending
        posted.clear()
        ax.submit_results(batch, results, model="m", session="s", url="http://x", token="t",
                          post=lambda m, u, b, t: (posted.append(b), (200, {"status": "ok"}))[1])
        self.assertFalse(any("dry_run_pending" in b for b in posted))

    def test_submit_builds_payload_logs_and_summarizes(self):
        batch = {"batch_id": "b0001", "items": [ax.to_identity(OBS), ax.to_identity(dict(OBS, id=2, content_hash="h2"))]}
        results = {"items": [
            {"id": 29995, "entities": [{"name": "model-gateway", "type": "Tool", "summary": "s"}],
             "edges": [{"source": "model-gateway", "name": "BINDS", "target": "model-gateway", "fact": "f"}]},
            {"id": 2, "entities": []},
            {"id": 99, "entities": [{"name": "x", "type": "Tool"}]},
        ]}
        posted = []

        def fake_post(method, url, body, token):
            posted.append((url, body, token))
            if body.get("empty"):                                      # 空观测正式提交时发「空」记录
                return 200, {"status": "empty_recorded"}
            return 200, {"status": "ok", "episode_uuid": "u", "nodes_new": 1, "nodes_reused": 0,
                         "edges_new": 0, "edges_reused": 0,
                         "stats": {"entities_in": 1, "edges_in": 1, "offschema_edge": 1, "dirty_name": 0,
                                   "unknown_type": 0, "entities_out": 1, "edges_out": 0}}
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "log.jsonl"
            recs = ax.submit_results(batch, results, model="claude-haiku-5-5", session="s", url="http://x",
                                     token="tok", post=fake_post, log_path=log)
            self.assertEqual([r["status"] for r in recs], ["ok", "empty", "unknown_id"])
            url, body, token = posted[0]
            self.assertEqual(url, "http://x/api/ingest_extracted")
            self.assertEqual(token, "tok")
            self.assertEqual((body["extractor_model"], body["extractor_session"], body["prompt_version"], body["sync"]),
                             ("claude-haiku-5-5", "s", ax.PROMPT_VERSION, True))
            self.assertEqual(body["source_obs_id"], "abc123")
            self.assertEqual(body["edges"][0]["name"], "BINDS")
            lines = [json.loads(l) for l in log.read_text().splitlines()]
            self.assertEqual(len(lines), 2)
            # dry-run：带 dry_run 标记、不记 log、不记名字
            posted.clear()
            names = Path(d) / "names.tsv"
            recs2 = ax.submit_results(batch, results, model="claude-haiku-5-5", session="s", url="http://x",
                                      token="tok", post=fake_post, log_path=log, dry_run=True, names_path=names)
            self.assertTrue(posted[0][1]["dry_run"])
            self.assertEqual(len(log.read_text().splitlines()), 2)
            self.assertFalse(names.exists())
            ax.submit_results(batch, results, model="claude-haiku-5-5", session="s", url="http://x",
                              token="tok", post=fake_post, log_path=log, names_path=names)
            self.assertEqual(names.read_text(), "Tool\tmodel-gateway\n")
            summary = ax.summarize_log(lines)
            self.assertEqual(summary["claude-haiku-5-5"]["ok"], 1)
            self.assertEqual(summary["claude-haiku-5-5"]["empty"], 1)
            self.assertEqual(summary["claude-haiku-5-5"]["offschema_edge_share"], 1.0)



class ProvenanceAndRunTest(unittest.TestCase):
    """框架 §8-1/§8-2：executor / run_id 随 payload 入图并记进 log；运行清单 run start/end。"""
    def setUp(self):
        patch = unittest.mock.patch.object(ax, "PROMPT_VERSION", "agent-v4")
        patch.start()
        self.addCleanup(patch.stop)

    def test_submit_carries_executor_and_run_id_and_status_groups_by_them(self):
        batch = {"batch_id": "b0001", "items": [ax.to_identity(OBS)]}
        results = {"items": [{"id": 29995, "entities": [{"name": "model-gateway", "type": "Tool"}], "edges": []}]}
        posted = []

        def fake_post(method, url, body, token):
            posted.append(body)
            return 200, {"status": "ok", "episode_uuid": "u", "stats": {"entities_in": 1, "edges_in": 0},
                         "resolved": {"model-gateway": {"reused": True, "matched_name": "model gateway"}}}
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "log.jsonl"
            recs = ax.submit_results(batch, results, model="m", session="s", url="http://x", token="t",
                                     post=fake_post, log_path=log, executor="codex", run_id="r1")
            self.assertEqual((posted[0]["extractor_tool"], posted[0]["run_id"]), ("codex", "r1"))
            self.assertEqual((recs[0]["executor"], recs[0]["run_id"], recs[0]["reuse_mismatch"]),
                             ("codex", "r1", ["model-gateway → model gateway"]))
            line = json.loads(log.read_text().splitlines()[0])
            self.assertEqual((line["executor"], line["run_id"]), ("codex", "r1"))
            # 不给就不打：服务端把 None 当未打标
            ax.submit_results(batch, results, model="m", session="s", url="http://x", token="t",
                              post=fake_post, log_path=log)
            self.assertIsNone(posted[1]["extractor_tool"])
            self.assertIsNone(posted[1]["run_id"])
            lines = [json.loads(l) for l in log.read_text().splitlines()]
        by_run = ax.summarize_log(lines, "run_id")
        self.assertEqual(sorted(by_run), ["?", "r1"])
        self.assertEqual((by_run["r1"]["ok"], by_run["r1"]["reuse_mismatch_per_obs"]), (1, 1.0))
        by_exec = ax.summarize_log(lines, "executor")
        self.assertEqual(by_exec["codex"]["n"], 1)
        with self.assertRaises(ValueError):
            ax.summarize_log(lines, "batch")

    def test_cli_defaults_executor_and_run_id_from_env(self):
        seen = {}

        def fake_submit(batch, results, **kw):
            seen.update(kw)
            return []
        with tempfile.TemporaryDirectory() as d:
            b, r = Path(d) / "b.json", Path(d) / "r.json"
            b.write_text(json.dumps({"batch_id": "b1", "items": []}))
            r.write_text(json.dumps({"items": []}))
            env = {"KG_HUB_EXECUTOR": "qoder", "KG_HUB_RUN_ID": "run-env", "KG_HUB_AGENT_EXTRACT_DIR": d,
                   "KG_HUB_CLAIMS_DIR": d + "/claims"}
            with unittest.mock.patch.dict(_os.environ, env), \
                    unittest.mock.patch.object(ax, "submit_results", fake_submit):
                ax.main(["submit", "--batch", str(b), "--file", str(r), "--model", "m"])
                self.assertEqual((seen["executor"], seen["run_id"]), ("qoder", "run-env"))
                ax.main(["submit", "--batch", str(b), "--file", str(r), "--model", "m",
                         "--executor", "codex", "--run-id", "run-cli"])
                self.assertEqual((seen["executor"], seen["run_id"]), ("codex", "run-cli"))

    def test_run_start_end_manifest(self):
        known = {"type_guard_on_resolution", "resolution_strictness", "submit_dry_run", "gateway.post_normalize",
                 "gateway.custom_extraction", "gateway.max_tokens", "gateway.edge_empty_retry", "batch_size"}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "runs" / "r1.json"
            m = ax.run_start("r1", executor="claude-code", model="claude-fable-5-1", sample="ab-sample-20261017",
                             server_commit="30049c9", executor_version="2.1.x", knobs={"batch_size": 20},
                             now="2026-10-19T01:00:00+00:00", path=p, known_knobs=known)
            self.assertEqual(set(m), {"run_id", "executor", "executor_version", "model", "prompt_version",
                                      "server_commit", "knobs", "sample", "started_at", "ended_at", "self_reported"})
            self.assertEqual(m["prompt_version"], ax.PROMPT_VERSION)
            self.assertEqual(m["knobs"]["gateway.max_tokens"], 8192)
            self.assertEqual(m["knobs"]["batch_size"], 20)
            self.assertIsNone(m["ended_at"])
            self.assertEqual(json.loads(p.read_text())["self_reported"], {"input_tokens": None, "output_tokens": None, "wall_s": None})
            with self.assertRaises(FileExistsError):
                ax.run_start("r1", executor="x", model="y", path=p, known_knobs=known)
            with self.assertRaises(ValueError):   # 没登记的旋钮：与 checkpoint 同一道闸
                ax.run_start("r2", executor="x", model="y", knobs={"made_up": 1}, path=Path(d) / "r2.json", known_knobs=known)
            with self.assertRaises(ValueError):
                ax.run_start("bad run!", executor="x", model="y", path=Path(d) / "r3.json", known_knobs=known)
            e = ax.run_end("r1", input_tokens=1000, output_tokens=200, now="2026-10-19T01:10:00+00:00", path=p)
            self.assertEqual(e["self_reported"], {"input_tokens": 1000, "output_tokens": 200, "wall_s": 600.0})
            self.assertEqual(e["ended_at"], "2026-10-19T01:10:00+00:00")
            e = ax.run_end("r1", wall_s=42.5, path=p)
            self.assertEqual(e["self_reported"]["wall_s"], 42.5)
            self.assertEqual(e["self_reported"]["input_tokens"], 1000)   # 不给的字段不动
            with self.assertRaises(FileNotFoundError):
                ax.run_end("nope", path=Path(d) / "nope.json")
            self.assertIsNone(ax.load_run("nope", Path(d) / "nope.json"))

    def test_cli_run_start_end_uses_state_dir_and_real_knobs(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"KG_HUB_AGENT_EXTRACT_DIR": d, "KG_HUB_EXECUTOR": "codex", "KG_HUB_CLAIMS_DIR": d + "/claims"}
            with unittest.mock.patch.dict(_os.environ, env):
                rc = ax.main(["run", "start", "--run-id", "r-cli", "--model", "gpt-5-codex",
                              "--knob", "gateway.max_tokens=4096"])
                self.assertEqual(rc, 0)
                m = json.loads((Path(d) / "runs" / "r-cli.json").read_text())
                self.assertEqual((m["executor"], m["knobs"]["gateway.max_tokens"]), ("codex", 4096))
                with self.assertRaises(SystemExit):   # 未登记旋钮
                    ax.main(["run", "start", "--run-id", "r-cli2", "--model", "m", "--knob", "nope=1"])
                rc = ax.main(["run", "end", "--run-id", "r-cli", "--input-tokens", "5", "--output-tokens", "6"])
                self.assertEqual(rc, 0)
                m = json.loads((Path(d) / "runs" / "r-cli.json").read_text())
                self.assertIsNotNone(m["ended_at"])
                self.assertEqual(m["self_reported"]["input_tokens"], 5)
                with self.assertRaises(SystemExit):
                    ax.main(["run", "bogus", "--run-id", "x"])



class DryRunGateTest(unittest.TestCase):
    """四条红线之 2「每批先 dry-run」由工具自己执行：--dry-run 记标记，正式提交逐 id 核对，没预览过的拒绝。"""
    def setUp(self):
        patch = unittest.mock.patch.object(ax, "PROMPT_VERSION", "agent-v4")
        patch.start()
        self.addCleanup(patch.stop)

    def _files(self, d):
        b, r = Path(d) / "b.json", Path(d) / "r.json"
        b.write_text(json.dumps({"batch_id": "b0009", "items": [ax.to_identity(OBS),
                                                                  ax.to_identity(dict(OBS, id=2, content_hash="h2"))]}))
        r.write_text(json.dumps({"items": [
            {"id": 29995, "entities": [{"name": "x", "type": "Tool"}], "edges": []},
            {"id": 2, "entities": []}]}))
        return b, r

    def test_marker_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            sd = Path(d)
            results = {"items": [{"id": 1, "entities": [{"name": "a", "type": "Tool"}]},
                                 {"id": 2, "entities": []},
                                 {"id": 3, "entities": [{"name": "c", "type": "Tool"}]}]}
            self.assertEqual(ax.dryrun_missing(sd, "b1", results), [1, 3])   # 空 entities 的 2 不要求
            ax.record_dryrun(sd, "b1", [{"id": 1, "status": "dry_run"}, {"id": 9, "status": "http_500"}],
                             now="2026-10-19T00:00:00+00:00")
            self.assertEqual(ax.dryrun_missing(sd, "b1", results), [3])      # 失败的 9 不算预览过
            ax.record_dryrun(sd, "b1", [{"id": 3, "status": "dry_run"}])        # 分两次 dry-run 取并集
            self.assertEqual(ax.dryrun_missing(sd, "b1", results), [])
            self.assertEqual(json.loads(ax.dryrun_marker_path(sd, "b1").read_text())["ids"], [1, 3])
            self.assertEqual(ax.dryrun_missing(sd, "b2", results), [1, 3])   # 别的批不沾光

    def test_submit_refuses_until_dry_run_then_allows(self):
        posted = []

        def fake_post(method, url, body, token):
            posted.append("empty" if body.get("empty") else body.get("dry_run", False))
            if body.get("empty"):
                return 200, {"status": "empty_recorded"}
            return 200, {"status": "dry_run" if body.get("dry_run") else "ok", "episode_uuid": "u",
                         "stats": {"entities_in": 1, "edges_in": 0}, "resolved": {}}
        with tempfile.TemporaryDirectory() as d:
            b, r = self._files(d)
            env = {"KG_HUB_AGENT_EXTRACT_DIR": d, "KG_HUB_CLAIMS_DIR": d + "/claims"}
            real = ax.submit_results
            # submit_results 的 post 参数在定义时就绑定了 _http，patch 模块属性不生效；包一层把假 post 塞进去，
            # 测试绝不能真的打到 NAS
            with unittest.mock.patch.dict(_os.environ, env), \
                    unittest.mock.patch.object(ax, "submit_results", lambda *a, **kw: real(*a, **{**kw, "post": fake_post})):
                with self.assertRaises(SystemExit) as cm:              # 没 dry-run 过：拒绝，且一条都没发
                    ax.main(["submit", "--batch", str(b), "--file", str(r), "--model", "m"])
                self.assertIn("还没 dry-run", str(cm.exception))
                self.assertEqual(posted, [])
                self.assertEqual(ax.main(["dryrun-check", "--batch", str(b), "--file", str(r)]), 1)
                self.assertEqual(ax.main(["submit", "--batch", str(b), "--file", str(r), "--model", "m", "--dry-run"]), 0)
                self.assertEqual(posted, [True])
                self.assertTrue((Path(d) / "dryrun-b0009.json").exists())
                self.assertEqual(ax.main(["dryrun-check", "--batch", str(b), "--file", str(r)]), 0)
                self.assertEqual(ax.main(["submit", "--batch", str(b), "--file", str(r), "--model", "m"]), 0)
                self.assertEqual(posted, [True, False, "empty"])   # 预览不发空记录；正式提交时空观测也记一条
                # 明确跳过：另一批没 dry-run 也放行
                b2 = Path(d) / "b2.json"
                b2.write_text(json.dumps({"batch_id": "b0010", "items": [ax.to_identity(OBS)]}))
                self.assertEqual(ax.main(["submit", "--batch", str(b2), "--file", str(r), "--model", "m",
                                          "--skip-dryrun-check"]), 0)


if __name__ == "__main__":
    unittest.main()
