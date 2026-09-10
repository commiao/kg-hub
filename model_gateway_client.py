"""Central Anthropic SDK construction for the model-gateway boundary.

The business layer supplies only the gateway URL, caller token and logical
business model key.  Provider identity and credentials never enter this module.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import ipaddress
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit


# credvault 路由的 `timeout`(routes.json 里每个 business_key 一个,现为 150s):网关
# 等供应商最多这么久。**客户端必须比它更晚放弃**,否则调用方断开时网关还在等,
# 那次付费调用白烧;更糟的是供应商同时超过路由 timeout 时,网关会留下一条永久
# `unknown` 幂等记录 —— readiness 从此报 error,而受控 cutover 硬性要求
# readiness==ok,部署就被永久挡住(2026-09-07 为此人工清了三轮共 67 条)。
#
# 2026-09-07 实测:kg-hub 的**每一个**调用点都是 90 或 120s < 150s,方向全反;
# 而 claude-mem 的 forwarder 用 175s,方向正确,可作参照。所以这条不变式不能靠
# 各调用点自觉,必须在工厂里兜住。
GATEWAY_ROUTE_TIMEOUT_SEC = float(
    os.environ.get("KG_HUB_GATEWAY_ROUTE_TIMEOUT_SEC", "150"))
# 余量 30s:够网关把供应商的终态写完幂等/见证记录并回传,不至于卡在最后一步。
CLIENT_TIMEOUT_MARGIN_SEC = float(
    os.environ.get("KG_HUB_GATEWAY_CLIENT_TIMEOUT_MARGIN_SEC", "30"))
MIN_CLIENT_TIMEOUT_SEC = GATEWAY_ROUTE_TIMEOUT_SEC + CLIENT_TIMEOUT_MARGIN_SEC
_timeout_floor_noted = False

import breakers

DEFAULT_BUSINESS_MODEL = "kg_hub.entity_extract"
DEFAULT_GATEWAY_URL = "http://model-gateway:39000"
BUSINESS_MODEL_PATTERN = re.compile(r"kg_hub\.[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_OPERATION_PART_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_operation: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "kg_hub_model_operation", default=None
)


@contextmanager
def model_operation(namespace: str, operation_id: str):
    """Bind paid substeps to a durable business operation identity."""
    namespace = str(namespace).strip()
    operation_id = str(operation_id).strip()
    if (_OPERATION_PART_PATTERN.fullmatch(namespace) is None
            or not operation_id or len(operation_id) > 1024
            or any(ord(ch) < 0x20 for ch in operation_id)):
        raise RuntimeError("invalid durable model operation identity")
    token = _operation.set((namespace, operation_id))
    try:
        yield
    finally:
        _operation.reset(token)


def stable_operation_id(*parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if hasattr(value, "model_dump"):
        return _canonical(value.model_dump())
    if hasattr(value, "dict"):
        return _canonical(value.dict())
    raise RuntimeError("model request contains a non-deterministic value")


def _durable_idempotency_key(namespace: str, operation_id: str,
                             args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    request = json.dumps(
        _canonical({"args": args, "kwargs": kwargs}), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    operation_digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    body_digest = hashlib.sha256(request).hexdigest()
    return "kg1-" + hashlib.sha256(
        f"{namespace}:{operation_digest}:{body_digest}".encode("ascii")
    ).hexdigest()


def _allow_ephemeral_operation() -> bool:
    """Return true only for an explicit development/test-only escape hatch."""
    explicit = os.environ.get(
        "KG_HUB_ALLOW_EPHEMERAL_IDEMPOTENCY", ""
    ).strip().lower()
    if explicit not in {"1", "true", "yes"}:
        return False
    environment = os.environ.get("KG_HUB_ENV", "").strip().lower()
    if environment not in {"dev", "development", "test"}:
        raise RuntimeError(
            "ephemeral idempotency is allowed only with KG_HUB_ENV=development/test"
        )
    return True


def _private_https_host(hostname: str) -> bool:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return ("." not in hostname or hostname.endswith(".ts.net")
                or hostname.endswith(".local"))
    return address.is_private or address.is_loopback or address in ipaddress.ip_network(
        "100.64.0.0/10"
    )


def gateway_base_url() -> str:
    raw = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_GATEWAY_URL).strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise RuntimeError("invalid kg-hub model gateway URL") from exc
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.path not in {"", "/"}):
        raise RuntimeError("kg-hub model gateway URL must be an origin")
    normalized = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if normalized == DEFAULT_GATEWAY_URL:
        return normalized
    if parsed.scheme != "https" or not _private_https_host(parsed.hostname.lower()):
        raise RuntimeError("kg-hub refuses public providers and cross-host plain HTTP")
    allowlist = {
        item.strip().rstrip("/") for item in
        os.environ.get("KG_HUB_GATEWAY_HTTPS_ALLOWLIST", "").split(",") if item.strip()
    }
    if normalized not in allowlist:
        raise RuntimeError("private HTTPS gateway is not explicitly allowlisted")
    return normalized


def gateway_model() -> str:
    """Return the logical business key used in the Anthropic ``model`` field."""
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_BUSINESS_MODEL).strip()
    if BUSINESS_MODEL_PATTERN.fullmatch(model) is None:
        raise RuntimeError("kg-hub model must be a kg_hub.* business key")
    return model


def gateway_token() -> str:
    """Read kg-hub's own caller token; never consult claude-mem files."""
    token = os.environ.get("KG_HUB_MODEL_GATEWAY_TOKEN", "").strip()
    if not token:
        raise RuntimeError("KG_HUB_MODEL_GATEWAY_TOKEN is required")
    return token


def install_gateway_request_contract(client: Any, *, min_interval: float = 0.0,
                                     thinking_disabled: bool = True) -> Any:
    """Inject a durable operation-derived key and optional local throttle.

    The SDK client is always constructed with ``max_retries=0``.  Therefore the
    same namespace/operation/request substep deterministically reuses its key
    after an unknown outcome.  Production rejects a missing operation context;
    the random UUID fallback exists only for explicitly non-production tools.
    """
    original_create = client.messages.create
    throttle_lock = asyncio.Lock()
    last_call = {"at": 0.0}
    # 同一操作内字节相同的并发请求算出同一把 Idempotency-Key。graphiti 对 fact 文本
    # 相同、节点对不同的两条边会并行发出一模一样的 resolve_edge prompt;网关只认第一个
    # 在飞的,第二个必得 425,整篇抽取随之失败(2026-09-06 obs-20410 / 20417 皆如此)。
    # 后来者等第一个的结果而不是各自出门:零请求、零费用,也不改网关契约。
    inflight: dict[str, asyncio.Future] = {}

    async def create_with_gateway_contract(*args, **kwargs):
        # 人工断路器。判定放在这里而不是各个业务调用点,是因为这里是 kg-hub
        # 唯一的模型出口:调用方不看开关、看错了、或者压根不知道有这回事,请求
        # 也出不去。放在业务侧就只是个建议,挡不住跑飞的调用方——而断路器存在的
        # 全部意义就是挡住跑飞的调用方。
        #
        # 必须在 inflight 合并**之前**判:合并之后再判,已经在飞的那一个仍会把钱
        # 花掉,而扳开关的人以为已经断了。
        breakers.assert_closed(str(kwargs.get("model") or gateway_model()))
        headers = dict(kwargs.get("extra_headers") or {})
        # Business callers cannot supply or preserve their own paid-operation
        # identity. Remove every casing variant before digesting and forwarding;
        # the central layer remains the sole authority for this header.
        for name in list(headers):
            if name.lower() == "idempotency-key":
                del headers[name]
        key_kwargs = dict(kwargs)
        key_kwargs["extra_headers"] = headers
        operation = _operation.get()
        if operation is not None:
            headers["Idempotency-Key"] = _durable_idempotency_key(
                operation[0], operation[1], args, key_kwargs
            )
        elif not _allow_ephemeral_operation():
            raise RuntimeError(
                "durable model operation_id is required"
            )
        else:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        kwargs["extra_headers"] = headers
        if thinking_disabled:
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body.setdefault("thinking", {"type": "disabled"})
            kwargs["extra_body"] = extra_body
        key = headers["Idempotency-Key"]
        pending = inflight.get(key)
        if pending is not None:
            # shield:等待方被取消不能连带取消发起方的结果
            return await asyncio.shield(pending)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        inflight[key] = future
        try:
            if min_interval > 0:
                async with throttle_lock:
                    wait = min_interval - (time.monotonic() - last_call["at"])
                    if wait > 0:
                        await asyncio.sleep(wait)
                    last_call["at"] = time.monotonic()
            result = await original_create(*args, **kwargs)
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
                future.exception()  # 本处已 raise;避免无等待方时的 never-retrieved 噪音
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            inflight.pop(key, None)

    client.messages.create = create_with_gateway_contract
    return client


def enforced_client_timeout(timeout: float | None) -> float:
    """把客户端超时抬到不低于 MIN_CLIENT_TIMEOUT_SEC。

    抬而不是报错:所有历史调用点都传了过小的值,报错会让它们全部启动失败;而"提前
    放弃"只会造成浪费与永久未决记录,抬高永远是安全方向。抬高时在 stderr 说一次
    —— 静默降级正是这套系统反复栽的跟头。
    """
    global _timeout_floor_noted
    if timeout is None:
        return MIN_CLIENT_TIMEOUT_SEC
    if timeout >= MIN_CLIENT_TIMEOUT_SEC:
        return float(timeout)
    if not _timeout_floor_noted:
        _timeout_floor_noted = True
        sys.stderr.write(
            f"kg-hub: client timeout {timeout}s raised to {MIN_CLIENT_TIMEOUT_SEC}s "
            f"(must outlast the gateway route timeout {GATEWAY_ROUTE_TIMEOUT_SEC}s)\n")
    return MIN_CLIENT_TIMEOUT_SEC


def create_gateway_client(*, timeout: float | None = None, min_interval: float = 0.0,
                          thinking_disabled: bool = True):
    """Create the sole supported paid-model client (transport retries disabled)."""
    from anthropic import AsyncAnthropic

    timeout = enforced_client_timeout(timeout)

    # Validate every boundary before the SDK constructor can create a transport.
    base_url = gateway_base_url()
    model = gateway_model()
    if not model.startswith("kg_hub."):  # defensive after canonical validation
        raise RuntimeError("invalid kg-hub business key")
    auth_token = gateway_token()

    client = AsyncAnthropic(
        auth_token=auth_token,
        base_url=base_url,
        max_retries=0,
        timeout=timeout,
    )
    return install_gateway_request_contract(
        client, min_interval=min_interval, thinking_disabled=thinking_disabled
    )
