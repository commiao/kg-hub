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

    def test_torn_copies_are_discarded_before_the_next_attempt(self):
        # /tmp 是 tmpfs（2026-10-10）：撕裂副本留到本轮结束，就是 6 倍见证库的内存。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            witness = root / "w.sqlite3"
            make_witness(witness)
            staging = root / "staging"
            staging.mkdir()
            real = E._copy_for_read
            seen = []

            def flaky(source, into, attempt):
                seen.append(sorted(p.name for p in into.iterdir()))
                target = real(source, into, attempt)
                if attempt < 2:
                    target.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
                return target

            with mock.patch.object(E, "_copy_for_read", flaky), \
                 mock.patch.object(E.time, "sleep"):
                database, _, _ = E._open_consistent_copy(witness, staging)
            database.close()
            self.assertEqual(seen, [[], [], []])
            self.assertEqual(sorted(p.name for p in staging.iterdir()), ["witness-2.sqlite3"])

    def test_watchdog_stages_witness_copies_in_memory(self):
        # 2026-10-10 实测：副本落盘时 watchdog 容器 4.5 小时写了 22.8GB。
        compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text("utf-8")
        block = compose.split("\n  watchdog:\n", 1)[1].split("\n  # ", 1)[0]
        self.assertRegex(block, r"\n    tmpfs:\n      - /tmp:size=\d+m")


def make_wal_witness(path: Path) -> sqlite3.Connection:
    """像网关那样：WAL + 一条常驻连接（锚），之后的写都留在 -wal 里未 checkpoint。"""
    make_witness(path)
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO daily_counts VALUES ('2026-09-08','kg_hub.entity_extract',7)")
    writer.commit()
    return writer


class DirectWalReadTests(unittest.TestCase):
    """T-0234 阶段 3：见证库为 WAL 时直接只读打开，不再每轮复制整库到 tmpfs。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "witness"
        self.root.mkdir()
        self.witness = self.root / "rollback-witness.sqlite3"

    def read_only_mount(self, directory=None):
        # watchdog 以 :ro 挂载见证目录：既不能建 -shm，也不能写任何文件。
        directory = directory or self.root
        directory.chmod(0o555)
        self.addCleanup(directory.chmod, 0o755)
        try:                                   # root 无视权限位，模拟不出只读挂载
            (directory / ".probe").touch()
        except OSError:
            return
        (directory / ".probe").unlink()
        self.skipTest("chmod 不能让目录只读（以 root 运行？）")

    def no_copy(self):
        return mock.patch.object(E, "_copy_for_read",
                                 side_effect=AssertionError("WAL 见证库不该被复制"))

    def counts(self, payload):
        return {r["day"]: r["count"] for r in payload["daily"]}

    def test_running_gateway_is_read_in_place_including_uncheckpointed_commits(self):
        writer = make_wal_witness(self.witness)
        self.addCleanup(writer.close)
        self.assertTrue(Path(str(self.witness) + "-wal").stat().st_size > 0)
        self.read_only_mount()
        with self.no_copy(), mock.patch.object(E, "DAILY_DAYS", 100000):
            payload = E.collect(self.witness)
        self.assertEqual(self.counts(payload)["2026-09-08"], 7)

    def test_reader_does_not_block_the_gateway_writing_its_witness(self):
        writer = make_wal_witness(self.witness)
        self.addCleanup(writer.close)
        database, _, _ = E._open_direct(self.witness)
        try:
            database.execute("BEGIN")
            database.execute("SELECT count(*) FROM daily_counts").fetchone()
            writer.execute("INSERT INTO daily_counts VALUES ('2026-09-09','k',1)")
            writer.commit()                                   # DELETE 模式下这里会 busy
        finally:
            database.close()

    def test_stopped_gateway_is_read_immutably_without_sidecars(self):
        writer = make_wal_witness(self.witness)
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.close()
        for suffix in ("-wal", "-shm"):                      # Linux 最后关闭时会删掉
            Path(str(self.witness) + suffix).unlink(missing_ok=True)
        self.read_only_mount()
        with self.no_copy(), mock.patch.object(E, "DAILY_DAYS", 100000):
            payload = E.collect(self.witness)
        self.assertEqual(self.counts(payload)["2026-09-08"], 7)

    def test_delete_mode_witness_is_still_copied(self):
        make_witness(self.witness)
        self.assertFalse(E._is_wal(self.witness))
        real = E._copy_for_read
        with mock.patch.object(E, "_copy_for_read", side_effect=real) as copy:
            E.collect(self.witness)
        self.assertEqual(copy.call_count, 1)

    def test_wal_witness_that_cannot_be_opened_directly_falls_back_to_copy(self):
        writer = make_wal_witness(self.witness)
        self.addCleanup(writer.close)
        real = E._copy_for_read
        with mock.patch.object(E, "_open_direct", return_value=None), \
             mock.patch.object(E, "_copy_for_read", side_effect=real) as copy:
            payload = E.collect(self.witness)
        self.assertEqual(copy.call_count, 1)
        self.assertIn("kg_hub.entity_extract", payload["ceilings"])

    def test_leftover_wal_is_never_read_immutably(self):
        # immutable=1 会无视 -wal：-wal 里还有提交时那样读到的是旧数据，宁可复制。
        writer = make_wal_witness(self.witness)
        self.addCleanup(writer.close)
        crashed = self.root / "crashed"
        crashed.mkdir()
        target = crashed / self.witness.name
        for suffix in ("", "-wal"):                          # 有 -wal、无 -shm
            target.with_name(target.name + suffix).write_bytes(
                Path(str(self.witness) + suffix).read_bytes())
        self.read_only_mount(crashed)
        self.assertIsNone(E._open_direct(target))

    def test_direct_read_that_fails_self_check_falls_back_to_copy(self):
        writer = make_wal_witness(self.witness)
        self.addCleanup(writer.close)
        real_check, real_copy = E._self_check, E._copy_for_read
        verdicts = iter([E.TornSnapshot("direct"), None])

        def check_once_torn(database):
            verdict = next(verdicts, None)
            if verdict:
                raise verdict
            return real_check(database)

        with mock.patch.object(E, "_self_check", side_effect=check_once_torn), \
             mock.patch.object(E, "_copy_for_read", side_effect=real_copy) as copy:
            payload = E.collect(self.witness)
        self.assertGreaterEqual(copy.call_count, 1)
        self.assertIn("kg_hub.entity_extract", payload["ceilings"])

    def test_wal_detection_reads_the_header_not_the_name(self):
        writer = make_wal_witness(self.witness)
        self.addCleanup(writer.close)
        self.assertTrue(E._is_wal(self.witness))
        bogus = self.root / "torn.sqlite3"
        bogus.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
        self.assertFalse(E._is_wal(bogus))
        self.assertFalse(E._is_wal(self.root / "missing.sqlite3"))


if __name__ == "__main__":
    unittest.main()
