"""Cost/idempotency contracts for kg-hub's centralized model client."""

from __future__ import annotations

import asyncio
import ast
import os
import re
import sys
import types
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
        self.assertEqual(captured["timeout"], 33.0)
        self.assertEqual(captured["auth_token"], "gateway-caller-token")
        self.assertEqual(captured["base_url"], "http://model-gateway:39000")

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

        for path in root.rglob("*.py"):
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
