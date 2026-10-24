"""Native CLI transport and data-free readiness checks (stdlib only).

Receipts describe controlled local calls, not provider signatures.  A route
inspection is diagnostic; only a successful challenge response is readiness.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import hashlib
import socket
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

NATIVE_EVIDENCE_VERSION = 2
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
TRANSIENT_KINDS = frozenset({"rate_limit", "service", "transport"})


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def code_fingerprint() -> str:
    root = Path(__file__).resolve().parent.parent
    return _digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in ("utils/cli_runtime.py", "tools/extract_runtime.py", "tools/extract_bench.py",
                                 "tools/extract_campaign.py", "tools/extract_cli.py", "tools/extract")})


def probe_context(*, executor: str, model: str, effort="medium", proxy_mode="auto", environ=None) -> dict:
    env, route = cli_environment(proxy_mode=proxy_mode, environ=environ)
    # Bind route and credential *changes* without exposing credential values or
    # reading any credential files.  These are local integrity hashes, not auth.
    names = (*PROXY_KEYS, "NO_PROXY", "no_proxy", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
             "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK",
             "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "OPENAI_BASE_URL", "OPENAI_API_KEY",
             "CODEX_HOME", "QODER_HOME")
    executable = shutil.which(executor, path=env.get("PATH"))
    try:
        binary_hash = hashlib.sha256(Path(executable).read_bytes()).hexdigest() if executable else None
    except OSError:
        binary_hash = "unreadable"
    return {"executor": executor, "requested_model": model, "effort": effort,
            "code_sha256": code_fingerprint(),
            "configuration_sha256": _digest({"proxy_mode": proxy_mode,
                                               "executable": executable, "executable_sha256": binary_hash,
                                               "environment": {key: env.get(key) for key in names}}),
            "host_sha256": _digest({"host": socket.gethostname(), "uid": os.getuid() if hasattr(os, "getuid") else None}),
            "route": route}


def public_proxy(value: str) -> str:
    """Never include proxy credentials, query strings, or paths in receipts."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
            return "configured_unparseable"
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        return parsed.scheme + "://" + host + (":" + str(parsed.port) if parsed.port else "")
    except ValueError:
        return "configured_unparseable"


def system_proxies(runner=None) -> dict:
    """Read the user's already enabled macOS proxies; never change them.

    An empty scutil view can result from sandbox visibility.  It is explicitly
    unverified, rather than evidence that the host has no configured proxy.
    """
    runner = runner or subprocess.run
    try:
        result = runner(["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return {"status": "system_proxy_unverified", "values": {}}
    values = {}
    for line in result.stdout.splitlines():
        match = re.match(r"\s*([A-Za-z]+)\s*:\s*(.*?)\s*$", line)
        if match:
            values[match[1]] = match[2]
    proxies = {}
    for prefix, key in (("HTTP", "HTTP_PROXY"), ("HTTPS", "HTTPS_PROXY")):
        host, port = values.get(prefix + "Proxy"), values.get(prefix + "Port")
        if values.get(prefix + "Enable") == "1" and host and port and port.isdigit():
            # scutil hosts are not URLs; do not accept credentials or paths.
            if re.fullmatch(r"[A-Za-z0-9._:\[\]-]+", host) and 0 < int(port) < 65536:
                if ":" in host and not host.startswith("["):
                    host = "[" + host + "]"
                proxies[key] = "http://" + host + ":" + port
    status = "system_proxy_configured" if proxies else "system_proxy_unverified"
    if not proxies and result.returncode == 0 and any(k in values for k in ("HTTPEnable", "HTTPSEnable")):
        status = "system_proxy_disabled_observed"
    return {"status": status, "values": proxies}


def cli_environment(*, proxy_mode: str = "auto", environ=None, platform=None, discover=None) -> tuple[dict, dict]:
    if proxy_mode not in {"auto", "env-only", "none"}:
        raise ValueError("proxy mode must be auto, env-only or none")
    env = dict(os.environ if environ is None else environ)
    platform = sys.platform if platform is None else platform
    supplied = {key: env[key] for key in PROXY_KEYS if env.get(key)}
    status = "existing_environment" if supplied else "no_process_proxy"
    if proxy_mode == "none":
        for key in PROXY_KEYS:
            env.pop(key, None)
        status = "explicit_direct"
    elif proxy_mode == "auto" and not supplied and platform == "darwin":
        found = (discover or system_proxies)()
        status = found["status"]
        env.update(found.get("values") or {})
    public = {key: public_proxy(env[key]) for key in PROXY_KEYS if env.get(key)}
    return env, {"proxy_mode": proxy_mode, "route_source": status, "proxies": public,
                 "readiness": "not_probed", "provider_route_override_present": bool(env.get("ANTHROPIC_BASE_URL"))}


def classify_failure(response: dict) -> dict:
    """Classify only an error envelope, never a generated fact's text."""
    error = response.get("error")
    if not error:
        return {"kind": None, "retryable": False, "terminal": False}
    declared = response.get("error_kind")
    if declared in {"auth", "identity", "configuration", "rate_limit", "service", "transport", "output", "unknown"}:
        kind = declared
    else:
        text = str(error)
        status = response.get("status_code")
        if not isinstance(status, int):
            match = re.search(r"(?:API\s*Error\s*:|HTTP(?:\s+status)?\s*[:=]?|status(?:_code)?[\"']?\s*[:=])\s*[\"']?([45]\d\d)\b", text, re.I)
            status = int(match.group(1)) if match else None
        if status in {401, 403} or re.search(r"failed to authenticate|unauthorized|invalid.api.key|authentication.failed", text, re.I):
            kind = "auth"
        elif status == 429:
            kind = "rate_limit"
        elif status is not None and 500 <= status < 600:
            kind = "service"
        elif text == "timeout" or re.search(r"connection (?:reset|refused)|timed? ?out|network error", text, re.I):
            kind = "transport"
        elif response.get("retryable") is False:
            kind = "identity" if response.get("actual_models") is not None else "configuration"
        else:
            kind = "unknown"
    return {"kind": kind, "retryable": kind in TRANSIENT_KINDS,
            "terminal": kind in {"auth", "identity", "configuration", "unknown"}}


def error_status(envelope) -> int | None:
    """Read status fields only from a CLI's error envelope (never result text)."""
    if isinstance(envelope, list):
        return next((status for item in envelope if (status := error_status(item)) is not None), None)
    if isinstance(envelope, dict):
        for key in ("status_code", "status", "http_status", "code"):
            value = envelope.get(key)
            if type(value) is int and 400 <= value < 600:
                return value
        for key in ("error", "errors"):
            status = error_status(envelope.get(key))
            if status is not None:
                return status
    return None


def cli_error(envelope: dict, returncode: int):
    error = envelope.get("error")
    if error:
        return error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
    if envelope.get("is_error"):
        return envelope.get("result") or "is_error"
    return "CLI exit code " + str(returncode) if returncode else None


def result_cost(response: dict) -> dict:
    return {"calls": 1, "tokens": response.get("tokens") or 0,
            "tokens_known": bool(response.get("tokens_known", response.get("tokens") is not None)),
            "credits": response.get("credits") or 0.0, "credits_known": response.get("credits") is not None,
            "ms": response.get("ms") or 0}


def invoke(call, *args) -> dict:
    """Unexpected local adapter failures still produce an unknown-cost receipt."""
    try:
        result = call(*args)
        if not isinstance(result, dict):
            raise TypeError("adapter returned no envelope")
        return result
    except Exception as exc:
        return {"text": "", "error": "CLI invocation failed: " + type(exc).__name__,
                "error_kind": "configuration", "tokens": None, "tokens_known": False,
                "credits": None, "stop": None, "ms": 0}


def probe(call, *, executor: str, model: str, route: dict | None = None, nonce: str | None = None,
          effort="medium", proxy_mode="auto", environ=None) -> dict:
    """One minimal request, no private observations and no automatic retries."""
    nonce = nonce or secrets.token_hex(16)
    challenge = json.dumps({"kg_hub_canary": nonce}, separators=(",", ":"))
    system = "Connectivity check. Return exactly the JSON object provided by the user. Do not use tools."
    context = probe_context(executor=executor, model=model, effort=effort, proxy_mode=proxy_mode, environ=environ)
    response = invoke(call, system, challenge)
    failure = classify_failure(response)
    valid = False
    if not response.get("error"):
        try:
            valid = json.loads((response.get("text") or "").strip()) == {"kg_hub_canary": nonce}
        except (ValueError, TypeError):
            pass
    identity = response.get("actual_models") or []
    if executor == "claude" and identity != [model]:
        valid = False
        if not response.get("error"):
            failure = {"kind": "identity", "retryable": False, "terminal": True}
    if not valid and not failure["kind"]:
        failure = {"kind": "canary_response", "retryable": False, "terminal": True}
    receipt = {"version": "cli-readiness-v1", "status": "ready" if valid else "infrastructure_failed",
            "executor": executor, "requested_model": model, "actual_models": identity,
            "model_identity_status": "verified" if executor == "claude" and valid else "requested-only",
            "identity_evidence": "provider_model_usage" if executor == "claude" and valid else "cli_request_only",
            "nonce": nonce, "challenge": challenge, "response": response,
            "failure": failure, "cost": result_cost(response),
            "created_at": datetime.now(timezone.utc).isoformat(),
            **context}
    if route is not None:
        receipt["route"] = route
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def validate_probe_receipt(receipt: dict, *, executor: str, model: str, effort="medium", proxy_mode="auto",
                           max_age: int = 900, environ=None, now=None) -> dict:
    """Validate a fresh, same-host/code/config probe before a *new* campaign.

    A receipt is not a permission grant or a provider signature.  API access is
    still verified by the actual nonce response, and runtime errors still stop.
    """
    validate_probe_integrity(receipt, executor=executor, model=model)
    def require(ok, message):
        if not ok:
            raise ValueError("CLI readiness precondition: " + message)
    require(isinstance(receipt, dict), "receipt must be an object")
    require(receipt.get("receipt_sha256") == _digest({k: v for k, v in receipt.items() if k != "receipt_sha256"}),
            "receipt changed")
    require(receipt.get("version") == "cli-readiness-v1" and receipt.get("status") == "ready", "probe is not ready")
    context = probe_context(executor=executor, model=model, effort=effort, proxy_mode=proxy_mode, environ=environ)
    require(all(receipt.get(k) == context[k] for k in ("executor", "requested_model", "effort", "code_sha256",
                                                    "configuration_sha256", "host_sha256")), "model/code/configuration differs")
    try:
        created = datetime.fromisoformat(receipt["created_at"])
        age = ((now or datetime.now(timezone.utc)) - created).total_seconds()
    except (ValueError, TypeError, KeyError):
        raise ValueError("CLI readiness precondition: invalid timestamp") from None
    require(0 <= age <= max_age, "probe expired or future-dated")
    response = receipt.get("response") or {}
    require(not response.get("error"), "probe response has an error")
    try:
        content = json.loads(response.get("text") or "")
    except (ValueError, TypeError):
        content = None
    require(isinstance(receipt.get("nonce"), str) and bool(receipt["nonce"]) and
            content == {"kg_hub_canary": receipt["nonce"]}, "probe did not answer the challenge")
    if executor == "claude":
        require(response.get("actual_models") == [model] and receipt.get("actual_models") == [model],
                "Claude model identity differs or is missing")
    require(receipt.get("cost") == result_cost(response), "probe cost differs from response")
    return receipt


def validate_probe_integrity(receipt: dict, *, executor: str, model: str) -> dict:
    """Historical verification checks evidence, never today's route or TTL."""
    def require(ok, message):
        if not ok:
            raise ValueError("CLI readiness evidence: " + message)
    require(executor in {"claude", "codex", "qodercli"}, "unsupported native executor")
    require(isinstance(receipt, dict), "receipt must be an object")
    require(receipt.get("receipt_sha256") == _digest({k: v for k, v in receipt.items() if k != "receipt_sha256"}),
            "receipt changed")
    require(receipt.get("version") == "cli-readiness-v1" and receipt.get("status") == "ready", "not ready")
    require(receipt.get("executor") == executor and receipt.get("requested_model") == model, "request differs")
    response = receipt.get("response") or {}
    require(not response.get("error"), "response has an error")
    try:
        challenge, content = json.loads(receipt.get("challenge") or ""), json.loads(response.get("text") or "")
    except (ValueError, TypeError):
        challenge, content = None, None
    require(isinstance(receipt.get("nonce"), str) and bool(receipt["nonce"]) and
            challenge == content == {"kg_hub_canary": receipt["nonce"]}, "challenge response differs")
    if executor == "claude":
        require(response.get("actual_models") == receipt.get("actual_models") == [model], "identity differs")
    require(receipt.get("cost") == result_cost(response), "cost differs")
    return receipt


class InfrastructureFailure(SystemExit):
    """No quality/admission verdict may be inferred from this incomplete run."""

    def __init__(self, receipt: dict):
        self.receipt = receipt
        super().__init__("native CLI infrastructure failure; see run-aborted.json: " +
                         str(receipt.get("failure", {}).get("kind", "unknown")))
