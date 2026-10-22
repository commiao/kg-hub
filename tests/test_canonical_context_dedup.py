"""/api/canonical_context：同名胶囊只注入最新一版，取用只记在被注入的那个节点上。

2026-10-22 只读核查：9 篇 canonical 胶囊有 19 个未归档节点（重新入库时 add_episode
新建节点、旧版不归档；DESIGN 有 4 版）。后果：
- 同一篇胶囊在一次注入里占两个名额（日志 names=[DESIGN,DESIGN,…]），还可能是旧版内容；
- 记取用按 name 匹配，一次注入 DESIGN 给 4 个节点各 +1，canonical 的 usage_count 成倍虚高。
纯伪驱动，不连库。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request

import kg_hub_server as S


def _request(query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/canonical_context",
                    "headers": [], "query_string": query.encode()})


def _canon(name, uuid, created_at, content, scope="global", uc=10):
    return {"name": name, "uuid": uuid, "content": content, "source": "kg-hub-canonical",
            "uc": uc, "scope": scope, "created_at": created_at}


class _Driver:
    """按 cypher 形态分派：胶囊全集 / 全文填位 / 取用计数。"""

    def __init__(self, canon_rows, fulltext_rows=()):
        self.canon_rows = canon_rows
        self.fulltext_rows = list(fulltext_rows)
        self.bump_params = None

    async def execute_query(self, cypher, **params):
        if "STARTS WITH 'kg-hub-canonical'" in cypher:
            return self.canon_rows, None, None
        if "fulltext.queryNodes" in cypher:
            return self.fulltext_rows, None, None
        if "usage_count" in cypher and "SET" in cypher:
            self.bump_params = params
            return [{"c": len(params.get("uuids") or params.get("names") or [])}], None, None
        return [], None, None


def _call(driver, query):
    with tempfile.TemporaryDirectory() as d, \
            patch.dict(_os.environ, {"KG_HUB_DELIVERY_LOG": str(_Path(d) / "hits.jsonl")}), \
            patch.object(S, "get_status_driver", return_value=driver):
        resp = asyncio.run(S.canonical_context(_request(query)))
    return json.loads(resp.body)


class LatestPerName(unittest.TestCase):
    def test_newest_created_at_wins(self):
        rows = [
            _canon("kg-hub-canonical-DESIGN", "d1", "2026-06-11T15:39:11", "v1"),
            _canon("kg-hub-canonical-DESIGN", "d4", "2026-10-16T18:57:21", "v4"),
            _canon("kg-hub-canonical-DESIGN", "d2", "2026-09-16T14:41:30", "v2"),
            _canon("kg-hub-canonical-NOTIFICATION", "n1", None, "only"),
        ]
        got = {r["name"]: r["uuid"] for r in S._latest_per_name(rows)}
        self.assertEqual(got, {"kg-hub-canonical-DESIGN": "d4", "kg-hub-canonical-NOTIFICATION": "n1"})


class CanonicalContextDedup(unittest.TestCase):
    def setUp(self):
        # 线上形态：三篇 global 胶囊，其中两篇各有新旧两版
        self.driver = _Driver([
            _canon("kg-hub-canonical-INVESTIGATION-RULES", "ir-old", "2026-08-20T15:49:24", "old rules"),
            _canon("kg-hub-canonical-INVESTIGATION-RULES", "ir-new", "2026-09-07T07:56:07", "new rules"),
            _canon("kg-hub-canonical-ONBOARDING", "ob-old", "2026-06-11T16:21:12", "old onboarding"),
            _canon("kg-hub-canonical-ONBOARDING", "ob-new", "2026-10-14T14:09:10", "new onboarding"),
            _canon("kg-hub-canonical-INTEGRATION-GUIDE", "ig", "2026-06-12T17:13:29", "guide"),
        ])

    def test_each_capsule_injected_once_with_newest_content(self):
        data = _call(self.driver, "kw=sd-server&top_n=3&bump=0")
        names = [p["name"] for p in data["picked"]]
        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3, names)
        by_name = {p["name"]: p for p in data["picked"]}
        self.assertEqual(by_name["kg-hub-canonical-INVESTIGATION-RULES"]["uuid"], "ir-new")
        self.assertEqual(by_name["kg-hub-canonical-INVESTIGATION-RULES"]["content"], "new rules")
        self.assertEqual(by_name["kg-hub-canonical-ONBOARDING"]["content"], "new onboarding")

    def test_bump_counts_only_the_injected_nodes(self):
        data = _call(self.driver, "kw=sd-server&top_n=3&bump=1")
        params = self.driver.bump_params
        self.assertIsNotNone(params)
        self.assertNotIn("names", params)
        self.assertEqual(sorted(params["uuids"]), ["ig", "ir-new", "ob-new"])
        self.assertEqual(data["bumped"], 3)

    def test_no_bump_when_bump_0(self):
        _call(self.driver, "kw=sd-server&top_n=3&bump=0")
        self.assertIsNone(self.driver.bump_params)

    def test_fulltext_fill_skips_same_name_versions(self):
        # 只有一篇胶囊合格，剩下名额由全文检索填；全文命中同名多版本只取得分最高的
        driver = _Driver(
            [_canon("kg-hub-canonical-ONBOARDING", "ob", "2026-10-14", "onboarding")],
            [
                {"name": "doc-x", "uuid": "x-new", "content": "x2", "source": "s", "score": 3.0},
                {"name": "doc-x", "uuid": "x-old", "content": "x1", "source": "s", "score": 2.0},
                {"name": "doc-y", "uuid": "y", "content": "y", "source": "s", "score": 1.0},
            ],
        )
        data = _call(driver, "kw=libtv-m&top_n=3&bump=1")
        self.assertEqual([p["name"] for p in data["picked"]],
                         ["kg-hub-canonical-ONBOARDING", "doc-x", "doc-y"])
        self.assertEqual(sorted(driver.bump_params["uuids"]), ["ob", "x-new", "y"])


if __name__ == "__main__":
    unittest.main()
