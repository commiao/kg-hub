"""看板按 project 关键词匹配，不能停在 ~/workspace_<工具> 时代。

仓库迁到 ~/work-ai 后：
- 工具贡献看板只认 project=workspace_<工具>，新数据的 project 是仓库名，几乎全落进
  「工具未知」（2026-10-14：最新 2000 条里约 1443 条）。
- 胶囊看板模拟 PUSH hook 关键词时还用 workspace_claudeCode，hook 早已推导不出它。
"""
import sys as _sys
import os as _os
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # 仓库根:否则 ModuleNotFoundError
_os.environ.setdefault("KG_HUB_API_TOKEN", "unit-test-only-token")
import asyncio
import json
import unittest
from unittest.mock import patch

try:
    import graphiti_core  # noqa: F401
    HAVE_GRAPHITI = True
except ImportError:
    HAVE_GRAPHITI = False

# 2026-10-14 线上分组（_TOOL_CONTRIB_QUERY 的形状：ws 只保留 workspace_* 目录，否则为空）
LIVE_ROWS = [
    {"ws": "workspace_claudeCode", "plat": "_default", "c": 2153},
    {"ws": "workspace_claudeCode", "plat": "claude", "c": 1736},
    {"ws": "workspace_claudeCode", "plat": "", "c": 863},
    {"ws": "workspace_codex", "plat": "_default", "c": 1309},
    {"ws": "workspace_codex", "plat": "codex", "c": 1264},
    {"ws": "workspace_codex", "plat": "", "c": 172},
    {"ws": "workspace_cursor", "plat": "", "c": 263},
    {"ws": "workspace_cursor", "plat": "_default", "c": 184},
    {"ws": "workspace_cursor", "plat": "cursor", "c": 131},
    {"ws": "workspace_cursor", "plat": "claude", "c": 8},
    {"ws": "workspace_qoder", "plat": "claude", "c": 35},
    {"ws": "workspace_qoder", "plat": "_default", "c": 4},
    {"ws": "workspace_qoder", "plat": "qoder", "c": 1},
    {"ws": "", "plat": "claude", "c": 5621},
    {"ws": "", "plat": "codex", "c": 2599},
    {"ws": "", "plat": "_default", "c": 2330},
    {"ws": "", "plat": "", "c": 863},
]


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class ToolAttribution(unittest.TestCase):
    def test_rules(self):
        from kg_hub_server import tool_of
        cases = [
            ("workspace_claudeCode", "_default", "Claude Code"),
            ("workspace_codex", "", "Codex"),
            ("workspace_cursor", "claude", "Cursor"),     # 目录优先：claude-mem 曾把 Cursor 标成 claude
            ("workspace_qoder", "claude", "Qoder"),
            ("", "claude", "Claude Code"),                 # 迁移后的仓库目录，按 platform
            ("", "codex", "Codex"),
            ("", "cursor", "Cursor"),
            ("", "qoder", "Qoder"),
            ("workspace_other", "codex", "Codex"),         # 不认识的 workspace_ 目录退回 platform
            ("", "_default", "工具未知"),
            ("", "", "工具未知"),
        ]
        for ws, plat, want in cases:
            self.assertEqual(tool_of(ws, plat), want, (ws, plat))

    def test_live_distribution(self):
        from kg_hub_server import tool_contributions
        got = {c["tool"]: c["count"] for c in tool_contributions(LIVE_ROWS)}
        self.assertEqual(got, {"Claude Code": 10373, "Codex": 5344, "Cursor": 586,
                               "Qoder": 40, "工具未知": 3193})
        self.assertEqual(sum(got.values()), sum(r["c"] for r in LIVE_ROWS))   # 不丢不重

    def test_dashboard_renders_new_attribution(self):
        import kg_hub_server as S
        queries = []

        class Driver:
            async def execute_query(self, cypher, **params):
                queries.append(cypher)
                if "ToolStat" in cypher:
                    return [], None, None
                return LIVE_ROWS, None, None

        with patch.object(S, "get_status_driver", return_value=Driver()):
            resp = asyncio.run(S.dashboard_tools(None))
        html = resp.body.decode()
        data = json.loads(html.split("var D=", 1)[1].split(";", 1)[0])
        self.assertEqual({c["tool"]: c["count"] for c in data["contrib"]}["Claude Code"], 10373)
        self.assertIs(queries[0], S._TOOL_CONTRIB_QUERY)
        self.assertNotIn("project=workspace_claudeCode", " ".join(queries))


@unittest.skipUnless(HAVE_GRAPHITI, "需要 graphiti_core（镜像里有；本机系统 python 没有）")
class CapsuleKeywords(unittest.TestCase):
    def test_dashboard_keywords_follow_the_hook(self):
        """胶囊看板模拟的关键词必须是 hook 在现目录下真推导得出的。"""
        from kg_hub_server import _DASH_KWS
        from tools.kg_push_hook import derive_project_keywords
        for kw in derive_project_keywords("/Users/mac/work-ai/kg-hub"):
            self.assertIn(kw, _DASH_KWS)
        self.assertNotIn("workspace_claudeCode", _DASH_KWS)


if __name__ == "__main__":
    unittest.main()
