"""导出必须么正确、么保留旧快照——绝不写出「结构合法但全空」的那一种。

2026-09-07 实测:网关每个付费请求都写见证库,`shutil.copy2` 有 ~60% 概率落在写事务
中间,所有查询抛 DatabaseError。旧实现把错误吞掉返回 [],于是写出一份全空快照,
拓扑面板据此显示「今日无 kg-hub 调用」——监控通路说了假话。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import export_gateway_usage as E  # noqa: E402


def make_witness(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE witness_meta (singleton INTEGER PRIMARY KEY, deployment_id TEXT);"
        "CREATE TABLE attempts (identity TEXT PRIMARY KEY, caller_digest TEXT,"
        " business_key TEXT, request_digest TEXT, at TEXT, day TEXT, phase TEXT);"
        "CREATE TABLE daily_counts (day TEXT, business_key TEXT, attempt_count INTEGER,"
        " PRIMARY KEY (day, business_key));"
        "CREATE TABLE cost_policy_ceiling (business_key TEXT PRIMARY KEY,"
        " daily_requests INTEGER, max_tokens INTEGER, requests_per_minute INTEGER,"
        " max_concurrency INTEGER, max_input_chars INTEGER, max_request_bytes INTEGER);"
    )
    db.execute("INSERT INTO witness_meta VALUES (1,'dep-1')")
    db.execute("INSERT INTO daily_counts VALUES ('2026-09-07','kg_hub.entity_extract',2228)")
    db.execute("INSERT INTO cost_policy_ceiling VALUES "
               "('kg_hub.entity_extract',120000,8192,60,8,400000,1048576)")
    db.commit()
    db.close()


class ExportContractTests(unittest.TestCase):
    def health_response(self, document):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(document).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        return opener

    def test_effective_limits_only_local_get_no_credentials_no_provider_call(self):
        opener = self.health_response({"status": "ok", "external_calls": 0,
            "effective_limits": {"kg_hub.entity_extract": {
                "daily_requests": 5000, "requests_per_minute": 60,
                "unexpected_secret": "must-not-export"}}})
        with mock.patch.object(E.urllib.request, "build_opener", return_value=opener):
            data = E.collect_effective_limits("http://model-gateway:39000")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://model-gateway:39000/health/ready")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.header_items(), [])
        self.assertEqual(data["limits"]["kg_hub.entity_extract"]["daily_requests"], 5000)
        self.assertNotIn("must-not-export", json.dumps(data))

    def test_invalid_or_unhealthy_response_yields_unknown_not_ceiling(self):
        for document in [
            {"status": "ok", "external_calls": 0, "ceilings": {"kg_hub.entity_extract": {"daily_requests": 120000}}},
            {"status": "error", "external_calls": 0, "effective_limits": {"x": {"daily_requests": 5000, "requests_per_minute": 60}}},
            {"status": "ok", "external_calls": 1, "effective_limits": {"x": {"daily_requests": 5000, "requests_per_minute": 60}}},
            {"status": "ok", "external_calls": 0, "effective_limits": {"x": {"daily_requests": True, "requests_per_minute": 60}}},
        ]:
            with mock.patch.object(E.urllib.request, "build_opener", return_value=self.health_response(document)):
                data = E.collect_effective_limits("http://model-gateway:39000")
            self.assertEqual(data["status"], "unknown")
            self.assertEqual(data["limits"], {})


    def test_503_valid_quota_projection_preserves_gateway_error(self):
        import io
        import urllib.error
        document = {"status": "error", "external_calls": 0,
            "effective_limits_status": "ok", "effective_limits_source": "gateway_route_registry",
            "checks": {"routes": {"status": "ok"}, "state_manifest": {"status": "ok"}},
            "effective_limits": {"x": {"daily_requests": 5000, "requests_per_minute": 60}}}
        for http_code, expected in [(503, "ok"), (500, "unknown"), (401, "unknown")]:
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(
                "http://model-gateway:39000/health/ready", http_code, "error", {},
                io.BytesIO(json.dumps(document).encode()))
            with mock.patch.object(E.urllib.request, "build_opener", return_value=opener):
                result = E.collect_effective_limits("http://model-gateway:39000")
            self.assertEqual(result["status"], expected)
            if expected == "ok":
                self.assertEqual(result["gateway_status"], "error")
                self.assertEqual(result["limits"]["x"]["daily_requests"], 5000)
        for key in ("routes", "state_manifest"):
            broken = json.loads(json.dumps(document))
            broken["checks"][key]["status"] = "error"
            with mock.patch.object(E.urllib.request, "build_opener", return_value=self.health_response(broken)):
                self.assertEqual(E.collect_effective_limits("http://model-gateway:39000")["status"], "unknown")

    def test_timeout_does_not_export_error_body_and_endpoint_is_fixed(self):
        opener = mock.Mock()
        opener.open.side_effect = TimeoutError("sensitive upstream details")
        with mock.patch.object(E.urllib.request, "build_opener", return_value=opener):
            data = E.collect_effective_limits("http://model-gateway:39000")
            self.assertEqual(E.collect_effective_limits("http://model-gateway:39000/v1/invoke")["status"], "unknown")
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(data["limits"], {})
        self.assertNotIn("sensitive", json.dumps(data))

    def test_healthy_witness_yields_daily_and_ceilings(self):
        with tempfile.TemporaryDirectory() as tmp:
            witness = Path(tmp) / "w.sqlite3"
            make_witness(witness)
            payload = E.collect(witness)
            self.assertEqual(payload["ceilings"]["kg_hub.entity_extract"]["daily_requests"],
                             120000)
            self.assertEqual(payload["effective_limits"], {})
            self.assertTrue(any(r["business_key"] == "kg_hub.entity_extract"
                                for r in payload["daily"]))

    def test_torn_copy_raises_instead_of_returning_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            witness = Path(tmp) / "w.sqlite3"
            witness.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)  # 坏库
            with self.assertRaises(E.TornSnapshot):
                E.collect(witness, )

    def test_required_rows_never_swallow_errors(self):
        db = sqlite3.connect(":memory:")
        self.assertEqual(E._rows(db, "SELECT * FROM nope"), [])          # 可选表:容忍
        with self.assertRaises(E.TornSnapshot):
            E._rows(db, "SELECT * FROM nope", required=True)             # 必需表:抛错

    def test_main_keeps_previous_snapshot_when_witness_is_torn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "usage.json"
            good = {"generated_at": "2026-09-07T03:52:49+00:00",
                    "ceilings": {"kg_hub.entity_extract": {"daily_requests": 120000}}}
            out.write_text(json.dumps(good), encoding="utf-8")
            witness = root / "w.sqlite3"
            witness.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
            rc = E.main(["--witness", str(witness), "--out", str(out)])
            self.assertEqual(rc, 1)                                   # 明确失败
            self.assertEqual(json.loads(out.read_text()), good)       # 旧快照原样保留

    def test_retry_eventually_succeeds_when_early_copies_are_torn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            witness = root / "w.sqlite3"
            make_witness(witness)
            calls = {"n": 0}
            real = E._copy_for_read

            def flaky(source, into, attempt):
                calls["n"] += 1
                target = real(source, into, attempt)
                if calls["n"] <= 2:                     # 前两次模拟撕裂
                    target.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
                return target

            E._copy_for_read = flaky
            E_delay = E._open_consistent_copy
            try:
                payload = E.collect(witness)
            finally:
                E._copy_for_read = real
            self.assertEqual(calls["n"], 3)
            self.assertIn("kg_hub.entity_extract", payload["ceilings"])
            self.assertIs(E._open_consistent_copy, E_delay)


if __name__ == "__main__":
    unittest.main()
