import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.retrieval_aliases import query_aliases


class RetrievalAliasesTests(unittest.TestCase):
    def write_config(self, rules):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"version": 1, "rules": rules}, handle, ensure_ascii=False)
        return Path(handle.name)

    def test_matches_only_when_all_required_fragments_are_present(self):
        path = self.write_config([{
            "id": "mcp-memory-to-muxcp",
            "match_all": ["mcp", "内存"],
            "expand_terms": ["muxcp"],
            "source_episode": "claude-mem-obs-4218",
        }])
        self.assertEqual(query_aliases("为什么 MCP 进程把内存占满了", path)[0].expand_terms, ("muxcp",))
        self.assertEqual(query_aliases("MCP 的接口规范", path), ())

    def test_invalid_rule_is_ignored_instead_of_widening_search(self):
        path = self.write_config([{
            "id": "bad",
            "match_all": ["公众号"],
            "expand_terms": [],
            "source_episode": "source",
        }])
        self.assertEqual(query_aliases("公众号 流量", path), ())


if __name__ == "__main__":
    unittest.main()
