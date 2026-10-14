"""T-0285: which expired edges carry the contradiction fingerprint, and how they are restored."""
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import unexpire_graphiti_contradictions as tool  # noqa: E402

T = "2026-09-0{}T10:00:00+00:00".format


def row(uuid, valid_at=None, invalid_at=None, expired_at=None, created_at=T(1), episode=None):
    return (uuid, valid_at, invalid_at, expired_at, created_at, episode or f"ep-{uuid}")


class Result:
    def __init__(self, result_set):
        self.result_set = result_set


class FakeGraph:
    """Records queries; reads return ``rows``/``facts``, writes report every row changed."""

    def __init__(self, rows=(), changed=None):
        self.rows = list(rows)
        self.changed = changed
        self.reads, self.writes = [], []

    def ro_query(self, query, params=None, timeout=None):
        self.reads.append((query, params))
        if query == tool.LOAD:
            return Result([list(r) for r in self.rows])
        return Result([[u, "fact " + u, "v", "i"] for u in params["u"]])

    def query(self, query, params=None, timeout=None):
        self.writes.append((query, params))
        n = len(params["rows"]) if self.changed is None else self.changed(params["rows"])
        return Result([[n]])


@contextmanager
def no_lock(owner="?", timeout_seconds=0.0):
    no_lock.calls.append(owner)
    yield


no_lock.calls = []


class ClassifyTests(unittest.TestCase):
    def categories(self, rows, since=None):
        return {r[0]: cat for r, cat in tool.classify(rows, since)}

    def test_old_edge_expired_by_a_newer_edge_from_another_episode_is_restored(self):
        rows = [row("old", T(1), invalid_at=T(3), expired_at=T(4)),
                row("new", T(3), created_at=T(3))]
        self.assertEqual(self.categories(rows), {"old": tool.RESTORE})

    def test_new_edge_expired_at_birth_by_a_later_dated_edge_is_restored(self):
        rows = [row("later", "2026-09-09T10:00:00+00:00", created_at=T(1)),
                row("new", T(2), invalid_at="2026-09-09T10:00:00Z", expired_at=T(2),
                    created_at=T(2))]
        self.assertEqual(self.categories(rows), {"new": tool.RESTORE})

    def test_text_end_without_a_matching_edge_is_kept(self):
        rows = [row("e", T(1), invalid_at="2026-09-01T10:05:00+00:00", expired_at=T(1))]
        self.assertEqual(self.categories(rows), {"e": tool.KEEP_TEXT_END})

    def test_matching_edge_from_the_same_episode_is_not_a_fingerprint(self):
        rows = [row("a", T(1), invalid_at=T(3), expired_at=T(4), episode="ep"),
                row("b", T(3), created_at=T(3), episode="ep")]
        self.assertEqual(self.categories(rows), {"a": tool.KEEP_TEXT_END})

    def test_matching_edge_created_after_the_expiry_is_not_a_fingerprint(self):
        rows = [row("a", T(1), invalid_at=T(3), expired_at=T(4)),
                row("b", T(3), created_at=T(5))]
        self.assertEqual(self.categories(rows), {"a": tool.KEEP_TEXT_END})

    def test_the_edge_itself_is_not_its_own_fingerprint(self):
        rows = [row("a", T(3), invalid_at=T(3), expired_at=T(4), created_at=T(1))]
        self.assertEqual(self.categories(rows), {"a": tool.KEEP_TEXT_END})

    def test_midnight_values_are_kept_even_when_they_match(self):
        rows = [row("a", T(1), invalid_at="2026-09-05T00:00:00+00:00", expired_at=T(6)),
                row("b", "2026-09-05T00:00:00+00:00", created_at=T(2))]
        self.assertEqual(self.categories(rows), {"a": tool.KEEP_MIDNIGHT})

    def test_expired_without_invalid_at_is_kept_and_live_edges_are_ignored(self):
        rows = [row("a", T(1), expired_at=T(2)), row("live", T(1))]
        self.assertEqual(self.categories(rows), {"a": tool.KEEP_NO_INVALID_AT})

    def test_since_limits_to_later_expiries(self):
        rows = [row("early", T(1), invalid_at=T(3), expired_at=T(4)),
                row("late", T(1), invalid_at=T(3), expired_at=T(6)),
                row("new", T(3), created_at=T(3))]
        self.assertEqual(self.categories(rows, since=T(5)), {"late": tool.RESTORE})


class ManifestAndWriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "m.jsonl")
        no_lock.calls.clear()

    def tearDown(self):
        self.dir.cleanup()

    def test_dry_run_reads_only_and_freezes_just_the_restore_rows(self):
        rows = [row("old", T(1), invalid_at=T(3), expired_at=T(4)),
                row("text", T(1), invalid_at="2026-09-01T10:05:00+00:00", expired_at=T(1)),
                row("new", T(3), created_at=T(3))]
        graph = FakeGraph(rows)
        report = tool.dry_run(graph, manifest=self.path)
        self.assertEqual(graph.writes, [])
        self.assertEqual(report["counts"], {tool.RESTORE: 1, tool.KEEP_TEXT_END: 1})
        header, frozen = tool.read_manifest(self.path)
        self.assertEqual(header["rows"], 1)
        self.assertEqual(frozen, [{"uuid": "old", "invalid_at": T(3), "expired_at": T(4)}])

    def test_apply_guards_on_frozen_values_batches_and_holds_the_writer_lock(self):
        rows = [{"uuid": str(i), "invalid_at": T(3), "expired_at": T(4)} for i in range(7)]
        graph = FakeGraph(changed=lambda chunk: len(chunk) - 1)
        with mock.patch("utils.writer_lock.writer_lock", no_lock):
            result = tool.apply(graph, rows, batch=3)
        self.assertEqual([len(p["rows"]) for _, p in graph.writes], [3, 3, 1])
        query = graph.writes[0][0]
        self.assertIn("e.invalid_at = r.invalid_at AND e.expired_at = r.expired_at", query)
        self.assertIn("SET e.invalid_at = NULL, e.expired_at = NULL", query)
        self.assertEqual(result, {"rows": 7, "changed": 4, "skipped": 3})
        self.assertEqual(no_lock.calls, [tool.TOOL])

    def test_restore_writes_values_back_only_where_both_are_empty(self):
        rows = [{"uuid": "a", "invalid_at": T(3), "expired_at": T(4)}]
        graph = FakeGraph()
        with mock.patch("utils.writer_lock.writer_lock", no_lock):
            tool.restore(graph, rows)
        query, params = graph.writes[0]
        self.assertIn("WHERE e.invalid_at IS NULL AND e.expired_at IS NULL", query)
        self.assertIn("SET e.invalid_at = r.invalid_at, e.expired_at = r.expired_at", query)
        self.assertEqual(params["rows"], rows)
        self.assertEqual(no_lock.calls, [tool.TOOL])

    def test_foreign_or_truncated_manifests_are_refused(self):
        Path(self.path).write_text(json.dumps({"tool": "other", "rows": 0}) + "\n")
        with self.assertRaises(ValueError):
            tool.read_manifest(self.path)
        tool.write_manifest(self.path, [row("a", T(1), T(3), T(4))], since=None, counts={})
        lines = Path(self.path).read_text().splitlines()
        Path(self.path).write_text(lines[0] + "\n")
        with self.assertRaises(ValueError):
            tool.read_manifest(self.path)

    def test_main_apply_runs_the_manifest_rows(self):
        tool.write_manifest(self.path, [row("a", T(1), T(3), T(4))], since=None, counts={})
        graph = FakeGraph()
        with mock.patch.object(tool, "_graph", return_value=graph), \
                mock.patch("utils.writer_lock.writer_lock", no_lock), \
                mock.patch("sys.stdout"):
            self.assertEqual(tool.main(["--apply", self.path]), 0)
        self.assertEqual(graph.writes[0][1]["rows"],
                         [{"uuid": "a", "invalid_at": T(3), "expired_at": T(4)}])


if __name__ == "__main__":
    unittest.main()
