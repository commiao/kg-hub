"""可撤销的 scoped Token：读得到、写不进去、撤了立刻失效。

T-0052 的结论是：`KG_HUB_API_TOKEN` 是**一个**静态值、鉴权只做一次相等比较，
所以在它之上说不出「只读」——不是没配好，是这个模型里没有「部分权限」这个概念。
于是向 dsh@win-main 承诺「仅 stats/search 的只读 Token」是承诺不出来的。

这套测试钉四件事：
1. 白名单内读得到、白名单外（含 /api/ingest）一律拒；
2. 拒绝要用 **403** 而不是 401 —— 对面据此决定「换 token」还是「别再试了」，
   混成一个码它就只能猜（准则 23）；
3. 撤销**不需要重启**：改文件即时生效；
4. 注册表缺失/损坏 = 一个 scoped token 都不认（失败关闭），不是放行。

另外钉一条**边界诚实**：/dashboard 那 12 条 POST 仍然免鉴权（2026-09-21 用户
明确决定维持现状，单人内网）。所以文档和代码都必须说「经 /api/* 写不进去」，
不许说成端口级只读——**一个说过头的安全声明，比没有声明更危险**。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import token_auth  # noqa: E402

SERVER_SRC = (ROOT / "kg_hub_server.py").read_text("utf-8")
MCP_SRC = (ROOT / "mcp_server.py").read_text("utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text("utf-8")

READ_ENDPOINTS = ("/api/stats", "/api/search", "/api/search_semantic",
                  "/api/node_neighbors", "/api/path_between", "/api/episode_search")


class _Bed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "read-tokens.json"
        self._orig = token_auth.ACCESS_CONTROL_PATH
        token_auth.ACCESS_CONTROL_PATH = self.path
        token_auth._cache.update(stamp=None, entries=())
        self.addCleanup(self._restore)

    def _restore(self):
        token_auth.ACCESS_CONTROL_PATH = self._orig
        token_auth._cache.update(stamp=None, entries=())

    def register(self, token="win-secret", scopes=("read",), disabled=False):
        self.path.write_text(json.dumps({"tokens": [{
            "name": "dsh@win-main", "sha256": token_auth.digest_for(token),
            "scopes": list(scopes), "disabled": disabled}]}), "utf-8")
        # mtime 粒度可能粗到看不出两次写入的差别，戳记里带了 size，但同长度内容
        # 仍可能撞上——测试里直接把缓存清掉，验的是判定不是缓存。
        token_auth._cache.update(stamp=None, entries=())


class ScopeTests(_Bed):
    def test_the_six_read_endpoints_are_allowed(self):
        self.register()
        p = token_auth.principal_for("win-secret")
        self.assertIsNotNone(p)
        for path in READ_ENDPOINTS:
            self.assertTrue(token_auth.allows(p, "GET", path), path)

    def test_ingest_is_refused_by_both_methods(self):
        """这条就是 T-0052 的题：只读 token 不许写进图。"""
        self.register()
        p = token_auth.principal_for("win-secret")
        self.assertFalse(token_auth.allows(p, "POST", "/api/ingest"))
        self.assertFalse(token_auth.allows(p, "GET", "/api/ingest"))

    def test_anything_not_listed_is_refused(self):
        """白名单之外一律拒 —— 将来新增端点默认不开放。"""
        self.register()
        p = token_auth.principal_for("win-secret")
        for m, path in (("GET", "/api/queue_stats"), ("POST", "/api/drain"),
                        ("GET", "/api/topology/latest"), ("POST", "/dashboard/tag")):
            self.assertFalse(token_auth.allows(p, m, path), f"{m} {path}")

    def test_a_scope_it_does_not_have_grants_nothing(self):
        self.register(scopes=("write",))
        p = token_auth.principal_for("win-secret")
        self.assertFalse(token_auth.allows(p, "GET", "/api/stats"))


class RevocationTests(_Bed):
    def test_disabled_takes_effect_without_a_restart(self):
        self.register()
        self.assertIsNotNone(token_auth.principal_for("win-secret"))
        self.register(disabled=True)
        self.assertIsNone(token_auth.principal_for("win-secret"),
                          "改 disabled 后仍然认，就等于撤销要重启")

    def test_removing_the_entry_also_revokes(self):
        self.register()
        self.path.write_text(json.dumps({"tokens": []}), "utf-8")
        token_auth._cache.update(stamp=None, entries=())
        self.assertIsNone(token_auth.principal_for("win-secret"))


class FailClosedTests(_Bed):
    def test_a_missing_registry_authorises_nobody(self):
        self.assertIsNone(token_auth.principal_for("win-secret"))

    def test_a_corrupt_registry_authorises_nobody(self):
        """读不懂时唯一安全的假设是不给——与断路器那条同向。"""
        self.path.write_text("{ this is not json", "utf-8")
        token_auth._cache.update(stamp=None, entries=())
        self.assertIsNone(token_auth.principal_for("win-secret"))

    def test_no_plaintext_token_is_ever_stored(self):
        self.register()
        self.assertNotIn("win-secret", self.path.read_text("utf-8"),
                         "注册表里出现了明文 token")


class WiringTests(unittest.TestCase):
    def test_middleware_separates_401_from_403(self):
        block = SERVER_SRC.split("class BearerAuthMiddleware", 1)[1][:2600]
        self.assertIn("token_auth.principal_for(token)", block)
        self.assertIn("token_auth.allows(principal", block)
        self.assertIn('"forbidden_scope"', block)
        self.assertIn("status_code=403", block)

    def test_admin_token_is_compared_in_constant_time(self):
        block = SERVER_SRC.split("class BearerAuthMiddleware", 1)[1][:2600]
        self.assertIn("hmac.compare_digest", block)
        self.assertNotIn("if token != API_TOKEN:", SERVER_SRC)

    def test_registry_is_mounted_read_only(self):
        """服务端无权改它 —— 能发能撤的只有宿主机上的人。"""
        self.assertIn("/access-control:ro", COMPOSE)
        self.assertIn("KG_HUB_ACCESS_CONTROL=/access-control/read-tokens.json", COMPOSE)

    def test_mcp_hides_the_write_tool_in_readonly_mode(self):
        self.assertIn("KG_HUB_MCP_READONLY", MCP_SRC)
        self.assertIn("@write_tool\nasync def kg_add_episode", MCP_SRC)
        self.assertNotIn("@mcp.tool()\nasync def kg_add_episode", MCP_SRC)

    def test_the_documented_boundary_is_not_overstated(self):
        """/dashboard 那 12 条 POST 仍免鉴权。说成端口级只读就是一句假的安全声明。"""
        module = (ROOT / "utils" / "token_auth.py").read_text("utf-8")
        self.assertIn("不是", module)
        self.assertIn("/dashboard", module)
        for text, where in ((module, "utils/token_auth.py"),
                            ((ROOT / "deploy" / "nas" / ".env.example").read_text("utf-8"),
                             ".env.example")):
            self.assertIn("/api/*", text, f"{where} 没写清边界只覆盖 /api/*")


if __name__ == "__main__":
    unittest.main()
