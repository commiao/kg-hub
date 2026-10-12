"""T-0244: FalkorDB keeps AOF (everysec) and writes at most one RDB a day."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def falkordb_args() -> list[str]:
    text = COMPOSE.read_text("utf-8")
    block = text.split("\n  falkordb:\n", 1)[1].split("\n\n", 1)[0]
    line = re.search(r"- FALKORDB_ARGS=(.*)", block).group(1)
    return line.split()          # run.sh word-splits ${FALKORDB_ARGS}


class FalkorDBPersistenceTests(unittest.TestCase):
    def option(self, name: str) -> list[str]:
        args = falkordb_args()
        found = [i for i, a in enumerate(args) if a == name]
        self.assertEqual(len(found), 1, f"{name} must appear exactly once")
        values = []
        for arg in args[found[0] + 1:]:
            if arg.startswith("--"):
                break
            values.append(arg)
        return values

    def test_aof_every_second_stays_the_durability(self):
        self.assertEqual(self.option("--appendonly"), ["yes"])
        self.assertEqual(self.option("--appendfsync"), ["everysec"])

    def test_at_most_one_rdb_snapshot_a_day(self):
        self.assertEqual(self.option("--save"), ["86400", "1"])


if __name__ == "__main__":
    unittest.main()
