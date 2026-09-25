"""The server must retain input before it acknowledges model work."""

import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "kg_hub_server.py"
TREE = ast.parse(SOURCE.read_text())


def isolated_backup(path):
    node = next(n for n in TREE.body if isinstance(n, ast.FunctionDef)
                and n.name == "_backup_episode")
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    namespace = {
        "INGEST_BACKUP_PATH": str(path), "Path": Path, "os": os,
        "datetime": datetime, "timezone": timezone, "json": json,
        "logger": __import__("logging").getLogger(__name__),
    }
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["_backup_episode"]


class BackupTests(unittest.TestCase):
    def test_input_is_durable_before_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "backup" / "ingest.jsonl"
            body = type("Body", (), {
                "source_description": "source", "source_obs_id": "item-1",
                "name": "episode", "episode_body": "original content",
            })()
            with patch("os.fsync", wraps=os.fsync) as sync:
                isolated_backup(path)(body, datetime.now(tz=timezone.utc))
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(json.loads(path.read_text())["episode_body"],
                             "original content")

    def test_backup_failure_refuses_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "directory"
            path.mkdir()
            body = type("Body", (), {
                "source_description": "source", "source_obs_id": "item-1",
                "name": "episode", "episode_body": "original content",
            })()
            with self.assertRaises(IsADirectoryError):
                isolated_backup(path)(body, datetime.now(tz=timezone.utc))

    def test_claim_occurs_after_backup(self):
        source = SOURCE.read_text()
        handler = source[source.index("async def ingest(request:"):source.index("async def ingest_status(")]
        self.assertLess(handler.index("_backup_episode(body, ref_time)"),
                        handler.index("merge_or_get_ingested_key("))


if __name__ == "__main__":
    unittest.main()
