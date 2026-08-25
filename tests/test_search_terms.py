import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.search_terms import all_terms_clause, bounded_terms


class SearchTermsTests(unittest.TestCase):
    def test_bounded_terms_keeps_distinct_cjk_terms_and_rejects_one_term_queries(self):
        self.assertEqual(bounded_terms("公众号 阅读量 公众号"), ("公众号", "阅读量"))
        self.assertEqual(bounded_terms("公众号"), ())

    def test_clause_uses_bound_parameters_for_each_required_term(self):
        clause, params = all_terms_clause(("公众号", "阅读量"), ("n.name", "n.content"))
        self.assertIn("toLower(n.name) CONTAINS $literal_term_0", clause)
        self.assertIn("toLower(n.content) CONTAINS $literal_term_1", clause)
        self.assertEqual(params, {"literal_term_0": "公众号", "literal_term_1": "阅读量"})


if __name__ == "__main__":
    unittest.main()
