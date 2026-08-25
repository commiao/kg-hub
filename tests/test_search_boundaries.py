import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
import unittest

from kg_hub_server import bounded_search_episode_uuids


class SearchBoundaryTests(unittest.TestCase):
    def test_boundary_ids_are_valid_unique_sorted_and_bounded(self):
        values = [
            f"00000000-0000-4000-8000-{index:012d}"
            for index in range(10)
        ]
        result = bounded_search_episode_uuids({
            "_eps_live": [values[3], "invalid", *reversed(values), values[3], None]
        })

        self.assertEqual(result, sorted(values)[:8])

    def test_missing_boundaries_are_empty(self):
        self.assertEqual(bounded_search_episode_uuids({}), [])


if __name__ == "__main__":
    unittest.main()
