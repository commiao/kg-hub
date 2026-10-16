"""episode_exact_match 的扫描护栏：条数上限不能先于内存（总字符）上限把接口卡死。

2026-09-20 起活跃 episode 越过 10_000，接口对每次请求都返回 503
scan_limit_exceeded，WeChat 维护作业的边界重建每天 10/10 失败。当时总字符
远低于 64Mi，真正约束内存的护栏一次也没触发。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import time
import unittest

import kg_hub_server as S

# 2026-10-17 线上实测（只读统计）
LIVE_ACTIVE = 19_992
LIVE_TOTAL_CHARS = 36_320_982
LIVE_MAX_CHARS = 36_175


def fake_query(stats, rows=()):
    calls = []

    def query(cypher, timeout_ms):
        calls.append(cypher)
        if "RETURN count(n)" in cypher:
            return [stats]
        return list(rows)

    return query, calls


def stats(active, total, longest):
    return {"active_count": active, "total_content_chars": total, "max_content_chars": longest}


class ScanLimitTest(unittest.TestCase):
    def deadline(self):
        return time.monotonic() + 60

    def test_live_volume_is_scanned(self):
        query, calls = fake_query(stats(LIVE_ACTIVE, LIVE_TOTAL_CHARS, LIVE_MAX_CHARS))
        active, total, _ = S._episode_exact_match_fetch_snapshot_with_query_sync(
            query, self.deadline())
        self.assertEqual((active, total), (LIVE_ACTIVE, LIVE_TOTAL_CHARS))
        self.assertEqual(len(calls), 2, "统计通过后必须真的去取正文")

    def test_count_limit_does_not_bind_before_char_limit(self):
        # 按线上平均每条字符数，总字符上限对应的条数必须不大于条数上限，
        # 否则条数会先于内存护栏把接口卡死（09-20 的形态）。
        avg_chars = LIVE_TOTAL_CHARS / LIVE_ACTIVE
        rows_at_char_limit = S._EPISODE_EXACT_MATCH_MAX_TOTAL_CONTENT_CHARS / avg_chars
        self.assertLessEqual(rows_at_char_limit, S._EPISODE_EXACT_MATCH_MAX_ACTIVE_EPISODES)

    def test_queries_fetch_one_past_the_limit(self):
        query, calls = fake_query(stats(LIVE_ACTIVE, LIVE_TOTAL_CHARS, LIVE_MAX_CHARS))
        S._episode_exact_match_fetch_snapshot_with_query_sync(query, self.deadline())
        bound = f"LIMIT {S._EPISODE_EXACT_MATCH_MAX_ACTIVE_EPISODES + 1}"
        for cypher in calls:
            self.assertIn(bound, cypher)

    def test_each_guard_still_refuses(self):
        cap = S._EPISODE_EXACT_MATCH_MAX_ACTIVE_EPISODES
        cases = {
            "count": stats(cap + 1, 1, 1),
            "total_chars": stats(1, S._EPISODE_EXACT_MATCH_MAX_TOTAL_CONTENT_CHARS + 1, 1),
            "single_chars": stats(1, 1, S._EPISODE_EXACT_MATCH_MAX_SINGLE_CONTENT_CHARS + 1),
        }
        for name, row in cases.items():
            with self.subTest(name):
                query, calls = fake_query(row)
                with self.assertRaises(S._EpisodeExactMatchScanLimitExceeded):
                    S._episode_exact_match_fetch_snapshot_with_query_sync(query, self.deadline())
                self.assertEqual(len(calls), 1, "超限必须在取正文之前拒绝")


if __name__ == "__main__":
    unittest.main()
