"""Cost/idempotency contracts for kg-hub's centralized model client."""

from __future__ import annotations

import asyncio
import ast
import os
import re
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_gateway_client as mgc


class FakeMessages:
    def __init__(self):
        self.calls = []

    async def create(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return kwargs


class FakeClient:
    def __init__(self):
        self.messages = FakeMessages()


def production_py_files(root):
    """仓库里属于「这个检出的生产代码」的 .py 文件。

    为什么要单独一个函数：`root.rglob("*.py")` 会走进**嵌套的 git 工作树**。
    2026-09-20 实测代价：`.claude/worktrees/view-assigned-tasks-91de86/` 里的
    文件被扫进来，`test_every_production_paid_call_is_lexically_operation_bound`
    一次报出 23 条 offender，全是假的；排除它花了三轮（先拿 git archive 做基线，
    基线自身因为没有 .git 而脏；换 git worktree 才准；最后同目录切主干代码
    对照才定案）。而假红花掉的是这条检查以后还有没有人信（准则 28）。

    判据取「自带 .git 的子目录」而不是排除 `.claude` 这个具体名字：
    worktree 放在哪儿都挡得住，语义也更准——**嵌套工作树里的文件本来就不属于
    这个检出**，它们有自己的 HEAD、自己的分支，是另一份代码。

    两处扫全树的用例共用它：同一个病修一次，不会只修一处（准则 28 的形状）。
    """
    nested = {q.parent for q in root.rglob(".git")}
    for path in sorted(root.rglob("*.py")):
        if any(n in path.parents for n in nested):
            continue
        yield path


class ProductionFileScanTests(unittest.TestCase):
    """扫全树的用例必须跳过嵌套 git 工作树，否则报的是别人的代码。

    出处：2026-09-20，`.claude/worktrees/view-assigned-tasks-91de86/` 被扫进来，
    一次报出 23 条假 offender。前后对照在同一条件下做过：造出嵌套工作树后，
    旧写法 FAILED（23 条），改用 production_py_files 后 OK。
    """

    def _tree(self, tmp):
        root = Path(tmp)
        (root / "prod.py").write_text("x = 1\n", encoding="utf-8")
        nested = root / ".claude" / "worktrees" / "sess"
        nested.mkdir(parents=True)
        # 真实工作树里 .git 是**文件**（gitdir: 指针），不是目录 —— 判据必须两种都认
        (nested / ".git").write_text("gitdir: /somewhere/.git/worktrees/sess\n",
                                     encoding="utf-8")
        (nested / "ghost.py").write_text("y = 2\n", encoding="utf-8")
        return root, nested

    def test_nested_worktree_files_are_not_production_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, nested = self._tree(tmp)
            got = {p.relative_to(root).as_posix()
                   for p in production_py_files(root)}
            self.assertIn("prod.py", got)
            self.assertNotIn(".claude/worktrees/sess/ghost.py", got,
                             "嵌套工作树里的文件不属于这个检出")

    def test_a_nested_repo_anywhere_is_skipped_not_just_dot_claude(self):
        # 判据是「自带 .git 的子目录」，不是某个具体目录名——worktree 放哪儿都挡得住
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prod.py").write_text("x = 1\n", encoding="utf-8")
            odd = root / "vendor" / "someclone"
            odd.mkdir(parents=True)
            (odd / ".git").mkdir()
            (odd / "theirs.py").write_text("y = 2\n", encoding="utf-8")
            got = {p.relative_to(root).as_posix() for p in production_py_files(root)}
            self.assertEqual(got, {"prod.py"}, got)

    def test_an_ordinary_tree_loses_nothing(self):
        # 反向：没有嵌套树时一个文件都不能少，否则这个跳过就成了新的假绿
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("", encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "b.py").write_text("", encoding="utf-8")
            got = {p.relative_to(root).as_posix() for p in production_py_files(root)}
            self.assertEqual(got, {"a.py", "sub/b.py"}, got)


class GatewayClientContractTests(unittest.TestCase):
    def test_durable_operation_reuses_key_after_unknown_and_distinct_ops_differ(self):
        class UnknownGatewayMessages:
            def __init__(self):
                self.unknown = set()
                self.provider_calls = 0
                self.keys = []

            async def create(self, *args, **kwargs):
                key = kwargs["extra_headers"]["Idempotency-Key"]
                self.keys.append(key)
                if key in self.unknown:
                    raise RuntimeError("gateway reports unknown outcome")
                self.unknown.add(key)
                self.provider_calls += 1
                raise TimeoutError("provider outcome unknown")

        client = FakeClient()
        client.messages = UnknownGatewayMessages()
        mgc.install_gateway_request_contract(client)

        async def attempt(operation_id):
            with mgc.model_operation("scheduled.document", operation_id):
                with self.assertRaises((TimeoutError, RuntimeError)):
                    await client.messages.create(
                        model="kg_hub.entity_extract",
                        messages=[{"role": "user", "content": "same document"}],
                    )

        asyncio.run(attempt("doc-sha256-stable"))
        asyncio.run(attempt("doc-sha256-stable"))
        self.assertEqual(client.messages.provider_calls, 1)
        self.assertEqual(client.messages.keys[0], client.messages.keys[1])
        asyncio.run(attempt("different-document"))
        self.assertEqual(client.messages.provider_calls, 2)
        self.assertNotEqual(client.messages.keys[1], client.messages.keys[2])

    def test_identical_inflight_requests_share_one_provider_call(self):
        class SlowMessages:
            def __init__(self):
                self.provider_calls = 0
                self.keys = []

            async def create(self, *args, **kwargs):
                self.provider_calls += 1
                call_no = self.provider_calls
                self.keys.append(kwargs["extra_headers"]["Idempotency-Key"])
                await asyncio.sleep(0.05)
                return {"id": f"msg-{call_no}"}

        client = FakeClient()
        client.messages = SlowMessages()
        mgc.install_gateway_request_contract(client)

        async def scenario():
            with mgc.model_operation("ingest.episode", "same-episode"):
                same = dict(model="kg_hub.entity_extract",
                            messages=[{"role": "user", "content": "identical prompt"}])
                other = dict(model="kg_hub.entity_extract",
                             messages=[{"role": "user", "content": "different prompt"}])
                first, second, third = await asyncio.gather(
                    client.messages.create(**same),
                    client.messages.create(**same),
                    client.messages.create(**other),
                )
                # 在飞期间字节相同的请求只出门一次,两个调用方拿到同一个结果
                self.assertEqual(client.messages.provider_calls, 2)
                self.assertIs(first, second)
                self.assertNotEqual(first, third)
                self.assertEqual(len(set(client.messages.keys)), 2)
                # 完成后再发同样的请求:合并只覆盖"在飞";完成后的重放由网关缓存裁决
                await client.messages.create(**same)
                self.assertEqual(client.messages.provider_calls, 3)

        asyncio.run(scenario())

    def test_inflight_failure_propagates_to_every_waiter(self):
        class FailingMessages:
            def __init__(self):
                self.provider_calls = 0

            async def create(self, *args, **kwargs):
                self.provider_calls += 1
                await asyncio.sleep(0.02)
                raise RuntimeError("gateway 425")

        client = FakeClient()
        client.messages = FailingMessages()
        mgc.install_gateway_request_contract(client)

        async def scenario():
            with mgc.model_operation("ingest.episode", "same-episode"):
                same = dict(model="kg_hub.entity_extract", messages=[])
                results = await asyncio.gather(
                    client.messages.create(**same), client.messages.create(**same),
                    return_exceptions=True)
            self.assertTrue(all(isinstance(r, RuntimeError) for r in results), results)
            self.assertEqual(client.messages.provider_calls, 1)

        asyncio.run(scenario())

    def test_missing_operation_id_fails_before_transport_by_default(self):
        client = FakeClient()
        original = client.messages.create
        mgc.install_gateway_request_contract(client)
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            RuntimeError, "operation_id"
        ):
            asyncio.run(client.messages.create(
                model="kg_hub.entity_extract", messages=[]
            ))
        self.assertEqual(original.__self__.calls, [])

    def test_caller_idempotency_header_cannot_bypass_durable_context(self):
        client = FakeClient()
        original = client.messages.create
        mgc.install_gateway_request_contract(client)
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            RuntimeError, "operation_id"
        ):
            asyncio.run(client.messages.create(
                model="kg_hub.entity_extract", messages=[],
                extra_headers={"idempotency-key": "caller-chosen-key"},
            ))
        self.assertEqual(original.__self__.calls, [])

        with mgc.model_operation("test.header", "stable-operation"):
            asyncio.run(client.messages.create(
                model="kg_hub.entity_extract", messages=[],
                extra_headers={"Idempotency-Key": "caller-chosen-key"},
            ))
        forwarded = original.__self__.calls[0][1]["extra_headers"]
        self.assertNotEqual(forwarded["Idempotency-Key"], "caller-chosen-key")
        self.assertRegex(forwarded["Idempotency-Key"], r"^kg1-[0-9a-f]{64}$")

    def test_explicit_development_escape_hatch_ignores_explicit_key(self):
        client = FakeClient()
        original = client.messages.create
        mgc.install_gateway_request_contract(client)

        async def run():
            await client.messages.create(model="kg_hub.entity_extract", messages=[])
            await client.messages.create(model="kg_hub.entity_extract", messages=[])
            await client.messages.create(
                model="kg_hub.entity_extract", messages=[],
                extra_headers={"Idempotency-Key": "operator-stable-key"},
            )

        with mock.patch.dict(os.environ, {
            "KG_HUB_ENV": "development",
            "KG_HUB_ALLOW_EPHEMERAL_IDEMPOTENCY": "true",
        }, clear=True):
            asyncio.run(run())
        calls = original.__self__.calls
        first = calls[0][1]["extra_headers"]["Idempotency-Key"]
        second = calls[1][1]["extra_headers"]["Idempotency-Key"]
        self.assertRegex(first, r"^[0-9a-f-]{36}$")
        self.assertNotEqual(first, second)
        third = calls[2][1]["extra_headers"]["Idempotency-Key"]
        self.assertRegex(third, r"^[0-9a-f-]{36}$")
        self.assertNotIn("operator-stable-key", (first, second, third))
        for _, kwargs in calls:
            self.assertEqual(kwargs["extra_body"]["thinking"], {"type": "disabled"})

    def test_ephemeral_escape_hatch_is_rejected_outside_development(self):
        client = FakeClient()
        original = client.messages.create
        mgc.install_gateway_request_contract(client)
        with mock.patch.dict(os.environ, {
            "KG_HUB_ENV": "production",
            "KG_HUB_ALLOW_EPHEMERAL_IDEMPOTENCY": "true",
        }, clear=True), self.assertRaisesRegex(RuntimeError, "development/test"):
            asyncio.run(client.messages.create(
                model="kg_hub.entity_extract", messages=[]
            ))
        self.assertEqual(original.__self__.calls, [])

    def test_factory_disables_sdk_retries_and_uses_only_gateway_env(self):
        captured = {}

        class Constructor(FakeClient):
            def __init__(self, **kwargs):
                super().__init__()
                captured.update(kwargs)

        fake_module = types.SimpleNamespace(AsyncAnthropic=Constructor)
        with mock.patch.dict(sys.modules, {"anthropic": fake_module}), mock.patch.dict(
            os.environ,
            {
                "KG_HUB_MODEL_GATEWAY_TOKEN": "gateway-caller-token",
                "ANTHROPIC_BASE_URL": "http://model-gateway:39000",
                "ANTHROPIC_MODEL": "kg_hub.entity_extract",
            },
            clear=False,
        ):
            client = mgc.create_gateway_client(timeout=33.0)
        self.assertIsInstance(client, Constructor)
        self.assertEqual(captured["max_retries"], 0)
        # 过小的超时被抬到下限:提前放弃会让网关留下永久 unresolved 记录
        self.assertEqual(captured["timeout"], mgc.MIN_CLIENT_TIMEOUT_SEC)
        self.assertEqual(captured["auth_token"], "gateway-caller-token")
        self.assertEqual(captured["base_url"], "http://model-gateway:39000")

    def test_client_timeout_must_outlast_the_gateway_route_timeout(self):
        """客户端提前放弃 = 白烧一次付费调用 + 可能留下永久 unknown 幂等记录。

        2026-09-07:kg-hub 每个调用点都是 90/120s,而路由 timeout 是 150s,方向全反;
        网关侧因此攒下 67 条未决记录,readiness 长期 error,受控 cutover 被挡住。
        """
        self.assertGreater(mgc.MIN_CLIENT_TIMEOUT_SEC, mgc.GATEWAY_ROUTE_TIMEOUT_SEC)
        # 不足即抬到下限;够大的值原样保留
        self.assertEqual(mgc.enforced_client_timeout(None), mgc.MIN_CLIENT_TIMEOUT_SEC)
        self.assertEqual(mgc.enforced_client_timeout(1.0), mgc.MIN_CLIENT_TIMEOUT_SEC)
        self.assertEqual(mgc.enforced_client_timeout(mgc.GATEWAY_ROUTE_TIMEOUT_SEC),
                         mgc.MIN_CLIENT_TIMEOUT_SEC)
        self.assertEqual(mgc.enforced_client_timeout(999.0), 999.0)

    def test_no_caller_hardcodes_a_timeout_below_the_floor(self):
        """任何调用点都不该再自带一个小于下限的字面量(否则又要靠工厂兜)。"""
        root = Path(__file__).resolve().parent.parent
        offenders = []
        pattern = re.compile(r"create_gateway_client\([^)]*timeout\s*=\s*([0-9.]+)")
        for path in production_py_files(root):
            if "/tests/" in str(path) or path.name == "model_gateway_client.py":
                continue
            for value in pattern.findall(path.read_text("utf-8", errors="ignore")):
                if float(value) < mgc.MIN_CLIENT_TIMEOUT_SEC:
                    offenders.append(f"{path.name}: {value}")
        self.assertEqual(offenders, [])

    def test_all_named_production_callers_use_central_factory(self):
        root = Path(__file__).resolve().parent.parent
        paths = [
            root / "graphiti_client.py",
            root / "kg_hub_server.py",
            root / "tools/backfill_schema.py",
            root / "tools/predigest_backfill.py",
        ]
        for path in paths:
            text = path.read_text("utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("AsyncAnthropic(", text)
                self.assertIn("model_gateway_client", text)
        central = (root / "model_gateway_client.py").read_text("utf-8")
        self.assertRegex(central, re.compile(r"max_retries\s*=\s*0"))

    def test_every_production_paid_call_is_lexically_operation_bound(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []

        class Audit(ast.NodeVisitor):
            def __init__(self, path):
                self.path = path
                self.operation_depth = 0

            @staticmethod
            def is_operation_context(item):
                expression = item.context_expr
                return (isinstance(expression, ast.Call)
                        and isinstance(expression.func, ast.Name)
                        and expression.func.id == "model_operation")

            def visit_With(self, node):
                bound = any(self.is_operation_context(item) for item in node.items)
                self.operation_depth += int(bound)
                for statement in node.body:
                    self.visit(statement)
                self.operation_depth -= int(bound)

            def visit_Call(self, node):
                function = node.func
                paid = (isinstance(function, ast.Attribute)
                        and function.attr == "add_episode")
                paid = paid or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "create"
                    and isinstance(function.value, ast.Attribute)
                    and function.value.attr == "messages"
                )
                if paid and self.operation_depth == 0:
                    offenders.append(f"{self.path.relative_to(root)}:{node.lineno}")
                self.generic_visit(node)

        for path in production_py_files(root):
            relative = path.relative_to(root)
            if (relative.parts[0] in {"tests", "spike-graphiti"}
                    or relative.parts[:2] == ("tools", "experimental")):
                continue
            Audit(path).visit(ast.parse(path.read_text("utf-8"), filename=str(path)))
        self.assertEqual(offenders, [])

    def test_graphiti_config_never_reads_direct_provider_token(self):
        source = (Path(__file__).resolve().parent.parent / "graphiti_client.py").read_text(
            "utf-8"
        )
        self.assertNotIn('os.environ["ANTHROPIC_AUTH_TOKEN"]', source)
        self.assertIn("gateway_token()", source)

    def test_compose_contract_needs_only_gateway_caller_model_and_base(self):
        root = Path(__file__).resolve().parent.parent
        compose = yaml.safe_load((root / "docker-compose.yml").read_text("utf-8"))
        example = {}
        for raw in (root / "deploy/nas/.env.example").read_text("utf-8").splitlines():
            if raw and not raw.startswith("#") and "=" in raw:
                key, value = raw.split("=", 1)
                example[key] = value

        def resolved_environment(service_name):
            result = {}
            for item in compose["services"][service_name]["environment"]:
                key, raw = item.split("=", 1)
                required = re.fullmatch(r"\$\{([^}:]+):\?[^}]+\}", raw)
                defaulted = re.fullmatch(r"\$\{([^}:]+):-([^}]*)\}", raw)
                plain = re.fullmatch(r"\$\{([^}]+)\}", raw)
                if required:
                    result[key] = example[required.group(1)]
                elif defaulted:
                    result[key] = example.get(defaulted.group(1), defaulted.group(2))
                elif plain:
                    result[key] = example.get(plain.group(1), "")
                else:
                    result[key] = raw
            return result

        for service_name in ("kg_hub_server", "ingester"):
            actual_env = resolved_environment(service_name)
            with self.subTest(service=service_name):
                self.assertIn("KG_HUB_MODEL_GATEWAY_TOKEN", actual_env)
                self.assertEqual(actual_env["ANTHROPIC_BASE_URL"],
                                 "http://model-gateway:39000")
                self.assertEqual(actual_env["ANTHROPIC_MODEL"],
                                 "kg_hub.entity_extract")
                self.assertNotIn("ANTHROPIC_AUTH_TOKEN", actual_env)
                client = FakeClient()
                original = client.messages.create
                mgc.install_gateway_request_contract(client)
                with mock.patch.dict(os.environ, actual_env, clear=True), \
                        self.assertRaisesRegex(RuntimeError, "operation_id"):
                    asyncio.run(client.messages.create(
                        model=actual_env["ANTHROPIC_MODEL"], messages=[]
                    ))
                self.assertEqual(original.__self__.calls, [])

    def test_network_override_neutralizes_legacy_provider_environment(self):
        root = Path(__file__).resolve().parent.parent
        override = yaml.safe_load(
            (root / "deploy/model-gateway-network.override.yml").read_text("utf-8")
        )
        for service_name in ("kg_hub_server", "ingester"):
            environment = override["services"][service_name]["environment"]
            with self.subTest(service=service_name):
                self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], "")
                self.assertIn("KG_HUB_MODEL_GATEWAY_TOKEN", environment)
                self.assertIn("model-gateway:39000", environment["ANTHROPIC_BASE_URL"])
                self.assertIn("kg_hub.entity_extract", environment["ANTHROPIC_MODEL"])

    def test_public_provider_and_raw_or_cross_consumer_models_fail_closed(self):
        captured = {"constructed": 0}

        class Constructor(FakeClient):
            def __init__(self, **kwargs):
                super().__init__()
                captured["constructed"] += 1

        fake_module = types.SimpleNamespace(AsyncAnthropic=Constructor)
        for url, model in (
            ("https://api.anthropic.com", "kg_hub.entity_extract"),
            ("http://model-gateway:39000", "qwen3.6-plus"),
            ("http://model-gateway:39000", "claude_mem.observation"),
        ):
            with self.subTest(url=url, model=model), mock.patch.dict(
                sys.modules, {"anthropic": fake_module}
            ), mock.patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL": url, "ANTHROPIC_MODEL": model,
                "KG_HUB_MODEL_GATEWAY_TOKEN": "independent-token",
            }, clear=True), self.assertRaises(RuntimeError):
                mgc.create_gateway_client()
        self.assertEqual(captured["constructed"], 0)

    def test_private_https_requires_exact_allowlist(self):
        with mock.patch.dict(os.environ, {
            "ANTHROPIC_BASE_URL": "https://nas.tailnet.ts.net:39000",
            "ANTHROPIC_MODEL": "kg_hub.entity_extract",
            "KG_HUB_MODEL_GATEWAY_TOKEN": "independent-token",
        }, clear=True), self.assertRaises(RuntimeError):
            mgc.gateway_base_url()
        with mock.patch.dict(os.environ, {
            "ANTHROPIC_BASE_URL": "https://nas.tailnet.ts.net:39000",
            "KG_HUB_GATEWAY_HTTPS_ALLOWLIST": "https://nas.tailnet.ts.net:39000",
        }, clear=True):
            self.assertEqual(mgc.gateway_base_url(),
                             "https://nas.tailnet.ts.net:39000")

    def test_no_python_or_shell_loads_claude_mem_env(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []
        source_pattern = "source ~/" + ".claude-mem/.env"
        loader_pattern = "load_dotenv(Path.home() / " + "\".claude-mem\""
        for pattern in ("*.py", "*.sh"):
            for path in root.rglob(pattern):
                if path.is_file() and source_pattern in path.read_text(
                        "utf-8", errors="ignore"):
                    offenders.append(str(path))
                elif path.is_file() and loader_pattern in path.read_text(
                        "utf-8", errors="ignore"):
                    offenders.append(str(path))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
