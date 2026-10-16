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

    def test_remote_retro_forwards_args_and_rc(self):
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw))
            return type("R", (), {"returncode": 2, "stdout": "due=True\n", "stderr": ""})()
        rc = ax.remote_retro(["gate", "--every", "500"], runner=runner)
        self.assertEqual(rc, 2)
        cmd = calls[0][0]
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("python3 -m tools.digest_retro gate --every 500 --state /backup/digest-retro.json", cmd[-1])
        rc = ax.remote_retro(["checkpoint", "--adjust", "live_per_cycle=20"], stdin_text="教训", runner=runner)
        self.assertEqual(calls[1][1]["input"], "教训")
        self.assertIn("--lessons-file /tmp/retro-lessons.md", calls[1][0][-1])


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
        fake = lambda obs, c, q: SimpleNamespace(accept=obs["type"] != "change")  # noqa: E731
        with mock.patch.object(ax, "evaluate", fake):
            picked, stats = ax.select_batch(rows, cfg, 2, {"h1": "ok", "h2": "pending"})
        self.assertEqual([p["id"] for p in picked], [4, 5])
        self.assertEqual((stats["already_ok"], stats["pending_elsewhere"], stats["rejected_by_filter"],
                          stats["picked"]), (1, 1, 1, 2))
        self.assertTrue(ax.evaluate(OBS, cfg, None) is not None)  # 真闸可调用


class SubmitTest(unittest.TestCase):
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
            summary = ax.summarize_log(lines)
            self.assertEqual(summary["claude-haiku-5-5"]["ok"], 1)
            self.assertEqual(summary["claude-haiku-5-5"]["empty"], 1)
            self.assertEqual(summary["claude-haiku-5-5"]["offschema_edge_share"], 1.0)


if __name__ == "__main__":
    unittest.main()
