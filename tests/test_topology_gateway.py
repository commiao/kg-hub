"""拓扑上的「模型网关」配额格:今日用量/日上限 → 颜色,必须与 2026-09-06 的真实事故对得上。

那晚 kg_hub 打满日上限 5000,218 篇抽取失败,而拓扑图上没有任何一格变色——
所以这里钉住:打满=红、≥80%=黄、refinery 停发=红、快照过旧/缺上限=灰(不猜)。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import topology as T  # noqa: E402

NOW = datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)


def usage(count: int, cap: int | None = 5000, *, age_s: int = 60, key: str = T.GATEWAY_PRIMARY_KEY):
    data = {
        "generated_at": (NOW - timedelta(seconds=age_s)).isoformat(),
        "daily": [
            {"day": "2026-09-07", "business_key": key, "count": count},
            {"day": "2026-09-06", "business_key": key, "count": 5000},   # 昨天的不算今天
            {"day": "2026-09-07", "business_key": "claude_mem.observation", "count": 15},
        ],
        "ceilings": {key: {"daily_requests": 120000, "requests_per_minute": 60}},
        "effective_limits": {},
    }
    if cap is not None:
        data["effective_limits"] = {key: {"daily_requests": cap, "requests_per_minute": 60},
                            "claude_mem.observation": {"daily_requests": 5000, "requests_per_minute": 60}}
    return data


class GatewayQuotaNodeTests(unittest.TestCase):
    def test_normal_usage_uses_actual_cap_not_approval_ceiling(self):
        node, edge = T.gateway_quota_node(usage(211), {"quota_paused": False}, now=NOW)
        self.assertEqual((node["id"], node["layer"], node["state"]), ("gateway", "graph", "green"))
        self.assertEqual(node["sub"], "今日 211/5000 · 4%")
        self.assertIn("审批上限 120000(非当前额度)", node["detail"])
        self.assertEqual(edge, {"from": "kghub", "to": "gateway", "state": "green"})
        self.assertEqual(node["metrics"]["keys"][T.GATEWAY_PRIMARY_KEY]["today"], 211)

    def test_eighty_percent_is_amber(self):
        node, _ = T.gateway_quota_node(usage(4000, cap=5000), {}, now=NOW)
        self.assertEqual(node["state"], "amber")
        self.assertIn("80%", node["detail"])

    def test_exhausted_is_red_even_when_refinery_has_not_noticed(self):
        node, edge = T.gateway_quota_node(usage(5000, cap=5000), {}, now=NOW)
        self.assertEqual(node["state"], "red")
        self.assertEqual(edge["state"], "red")
        self.assertIn("打满", node["detail"])

    def test_refinery_quota_pause_is_red_regardless_of_count(self):
        node, _ = T.gateway_quota_node(usage(10), {"quota_paused": True, "quota_hits": 2}, now=NOW)
        self.assertEqual(node["state"], "red")
        self.assertIn("停发", node["detail"])
        self.assertEqual(node["metrics"]["quota_hits"], 2)

    def test_stale_or_missing_snapshot_is_grey_not_green(self):
        node, _ = T.gateway_quota_node(usage(10, age_s=20 * 60), {}, now=NOW)
        self.assertEqual(node["state"], "grey")
        node, _ = T.gateway_quota_node(None, {}, now=NOW)
        self.assertEqual(node["state"], "grey")
        self.assertIn("未找到", node["detail"])

    def test_missing_effective_limits_never_falls_back_to_ceiling(self):
        node, _ = T.gateway_quota_node(usage(10, cap=None), {}, now=NOW)
        self.assertEqual(node["state"], "grey")
        self.assertIn("effective_limits", node["detail"])
        self.assertEqual(node["sub"], "今日 10 · 上限未知")

    def test_old_exporter_and_invalid_caps_are_unknown(self):
        for bad in [None, 0, -1, True, "5000"]:
            data = usage(10)
            data["effective_limits"][T.GATEWAY_PRIMARY_KEY]["daily_requests"] = bad
            node, _ = T.gateway_quota_node(data, {}, now=NOW)
            self.assertEqual(node["state"], "grey")
            self.assertIsNone(node["metrics"]["keys"][T.GATEWAY_PRIMARY_KEY]["ratio"])
        data = usage(10)
        del data["effective_limits"]
        self.assertEqual(T.gateway_quota_node(data, {}, now=NOW)[0]["state"], "grey")

    def test_annotate_only_snapshots_with_kghub_and_only_once(self):
        node, edge = T.gateway_quota_node(usage(1), {}, now=NOW)
        snap = {"nodes": [{"id": "kghub", "layer": "kghub"}], "edges": []}
        T.annotate_gateway(snap, node, edge)
        T.annotate_gateway(snap, node, edge)
        self.assertEqual([n["id"] for n in snap["nodes"]], ["kghub", "gateway"])
        self.assertEqual(len(snap["edges"]), 1)
        other = {"nodes": [{"id": "tool:x", "layer": "tool"}], "edges": []}
        T.annotate_gateway(other, node, edge)
        self.assertEqual(len(other["nodes"]), 1)

    def test_graph_layer_label_names_the_model_gateway(self):
        self.assertIn(("graph", "图谱 / 模型"), T.LAYERS)


if __name__ == "__main__":
    unittest.main()
