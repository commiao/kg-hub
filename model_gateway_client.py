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
import time
import uuid
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit


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


def create_gateway_client(*, timeout: float = 90.0, min_interval: float = 0.0,
                          thinking_disabled: bool = True):
    """Create the sole supported paid-model client (transport retries disabled)."""
    from anthropic import AsyncAnthropic

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
