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
import logging
import os
import random
import re
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from utils.model_attempt_journal import (
    NeedsReconciliation, journal_from_backup_env, query_gateway_attempt_status,
)
from utils.reconciliation_mailbox import task_uuid


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

# credvault 的本地准入(每业务并发 / 每分钟)在幂等登记之后、供应商调用之前拒绝,
# 并当场撤销幂等与见证预占 —— 同一把 Idempotency-Key 重发不会拿到缓存的 429,
# 也不计费。网关对这两种拒绝只给通用码 cost_limit_exceeded,只能靠原文区分;
# 请求体/输入/输出超限、日上限用的也是这个码,但等多久都不会好,不能重试。
# 总等待要远小于 KG_HUB_STUCK_THRESHOLD_MIN(30min):单条观测已能跑到约 25min,
# 超过阈值的 pending 键会被改成 needs_reconciliation。
ADMISSION_RETRY_MAX_WAIT_SEC = float(
    os.environ.get("KG_HUB_GATEWAY_ADMISSION_RETRY_MAX_WAIT_SEC", "120"))
_ADMISSION_REJECTION_MESSAGES = frozenset({
    "该业务并发请求已达到本地上限",
    "该业务每分钟请求数已达到本地上限",
})
_ADMISSION_RETRIES_TOTAL = [0]

import breakers

DEFAULT_BUSINESS_MODEL = "kg_hub.entity_extract"
DEFAULT_GATEWAY_URL = "http://model-gateway:39000"
BUSINESS_MODEL_PATTERN = re.compile(r"kg_hub\.[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_OPERATION_PART_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_USAGE_SCENARIO_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_operation: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "kg_hub_model_operation", default=None
)
_usage_scenario: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kg_hub_model_usage_scenario", default=None
)
_business_task: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "kg_hub_business_task", default=None
)
_model_stage: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kg_hub_model_stage", default=None
)
_resume: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "kg_hub_manual_resume", default=None
)
_wire_attempt: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "kg_hub_wire_attempt", default=None
)


@contextmanager
def model_business_task(source_description: str, source_obs_id: str):
    """Bind every Graphiti model subcall to its original ingest identity."""
    token = _business_task.set((source_description, source_obs_id))
    try:
        yield
    finally:
        _business_task.reset(token)


@contextmanager
def model_stage(stage: str):
    """Tag journaled calls with the exact Graphiti stage that issued them."""
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", stage) is None:
        raise ValueError("invalid model stage")
    token = _model_stage.set(stage)
    try:
        yield
    finally:
        _model_stage.reset(token)


@contextmanager
def model_manual_resume(source_description: str, source_obs_id: str,
                        step_id: str, grant_id: str,
                        stage: str | None = None, *,
                        gateway_step_id: str | None = None,
                        execution_id: str | None = None):
    """Bind exactly one already authorized failed step to a business run."""
    journal = journal_from_backup_env()
    if journal is None:
        raise RuntimeError("manual resume requires a durable model journal")
    cached_steps = {row["step_id"] for row in
                    journal.find_task(source_description, source_obs_id)
                    if row["phase"] == "completed" and row["result_json"]}
    state = {"task": (source_description, source_obs_id), "step_id": step_id,
             "grant_id": grant_id, "consumed": False,
             "cached_pending": cached_steps, "stage": stage,
             "gateway_step_id": gateway_step_id, "execution_id": execution_id}
    token = _resume.set(state)
    try:
        yield
    finally:
        _resume.reset(token)


def manual_resume_stage() -> str | None:
    """Return the stage authorized by the active human retry, if any."""
    resume = _resume.get()
    return resume.get("stage") if resume is not None else None


_stage_steps: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar("kg_hub_stage_steps", default=None)


@contextmanager
def collect_model_steps():
    """Collect only the model steps invoked inside one persisted stage."""
    seen = set()
    token = _stage_steps.set(seen)
    try:
        yield seen
    finally:
        _stage_steps.reset(token)


def acknowledge_restored_steps(step_ids) -> None:
    """Called only after validating an immutable completed business stage."""
    resume = _resume.get()
    if resume is not None:
        resume["cached_pending"].difference_update(step_ids)


async def gateway_task_correlation_request_hook(request: Any) -> None:
    """Attach reconciliation identity to the serialized HTTP request.

    The gateway's ``X-Model-Gateway-Step-Id`` contract is the SHA-256 of the
    exact request body bytes after SDK serialization.  Computing it around
    ``messages.create`` would hash a different representation, so inject both
    correlation headers at HTTPX's request event boundary instead.
    """
    task = _business_task.get()
    if task is None:
        return
    await request.aread()
    request.headers["X-Model-Gateway-Task-Ids"] = task_uuid(*task)
    body_digest = hashlib.sha256(request.content).hexdigest()
    wire_step_id = body_digest
    mailbox_step_id = wire_step_id
    resume = _resume.get()
    local_attempt = _wire_attempt.get()
    if (resume is not None and resume.get("consumed")
            and local_attempt is not None
            and local_attempt[1] == resume["step_id"]
            and resume.get("gateway_step_id")):
        # HTTPX's current body hash remains the wire contract. Keep a separate
        # stable mailbox alias so dashboard commands can continue following
        # the originally authorized exact model call after a serialization
        # change; the local SDK digest/grant must already match.
        mailbox_step_id = resume["gateway_step_id"]
    request.headers["X-Model-Gateway-Step-Id"] = wire_step_id
    local_attempt = _wire_attempt.get()
    if local_attempt:
        journal = await asyncio.to_thread(journal_from_backup_env)
        if journal is None:
            raise RuntimeError("gateway wire step has no durable local journal")
        await asyncio.to_thread(journal.record_gateway_step,
            *task, local_attempt[0], wire_step_id, body_digest,
            mailbox_step_id=mailbox_step_id)


# —— 结构化外壳修正 ——
#
# 2026-09-21 实测:图里 17 条非 503 的 error 键里约 10 条**载荷是对的、外壳错了**:
#
#   形态 A  extracted_entities 该是 list,模型给的是一个内容正确的 JSON **字符串**
#           ValidationError: Input should be a valid list [type=list_type,
#             input_value='\n[{"name": "192.168.10...", "episode_indices": [0]}]\n']
#   形态 B  列表被多包了一层:{"edges": [{"edges": [ ...真正的边... ]}]}
#           ValidationError: edges.0.source_entity_name Field required,
#             input_value={'edges': [...]}
#
# 抽取结果本身没问题,却因为外壳被 pydantic 拒掉,于是整条观测落成 error 键、按
# 24h 锁住、重推撞 409。修在这里而不是各调用点,与断路器同一个理由:这是 kg-hub
# **唯一的模型出口**。
#
# 两条规则都刻意极窄:只在「否则必定校验失败」的形状上动手。代价必须说清 ——
# 它们同时会把「模型真的少答了」也一起放过去(比如本该 20 个实体只给了字符串形式
# 的 3 个,修正后会静默入图)。所以**每次修正都要计数并对外可见**,否则这就是一个
# 没人看得见的静默修补 —— 那正是本项目反复消灭的东西。
_repairs: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "kg_hub_envelope_repairs", default=None
)
_REPAIRS_TOTAL: dict[str, int] = {}


# —— 脱稿:请求里**强制**要求调工具,模型却回了一段文本 ——
#
# 2026-09-21 实测 4 条 `ValueError: Could not extract JSON from model response`,
# 文本长这样(取自线上 error_message):
#
#     =
#     <parameter=path>
#     /private/tmp/openclaw-skill-sync-validate/.../openclaw-inventory.json
#     </parameter>
#     </function>
#
# 一开始以为是观测正文里含这种标记、模型顺着续写。**查了:28184 条观测里只有 1 条
# 含 `<parameter=`,而且那条正是记录本次排查的产物**。所以不是正文污染 —— 是模型
# 自己编了一段 XML 风格的函数调用去"读文件"(那些路径来自观测的 files_read 字段,
# 而且它编出来的路径还和观测里的不是同一个:观测是 wave7,它写的是 wave8)。
#
# 判据不靠文案:graphiti 传的是 `tool_choice={'type':'tool','name':...}`,**强制**
# 调那一个工具。强制之下回来没有 tool_use 块,就只有一种解释。这样判还有个好处 ——
# 不必去 match 第三方库的那句英文报错(准则 22:别拿"关于实现的一段话"当判据)。
#
# 不把它当成"这条观测有毛病":84 条观测提到 HANDOVER.md,只有其中一两条踩到,
# 说明是采样噪声而非内容决定 —— 和 5xx 同类,该按 1h 释放而不是锁 24h。
_OFFSCRIPT_TOTAL = [0]


def is_local_admission_rejection(exc: BaseException) -> bool:
    if getattr(exc, "status_code", None) != 429:
        return False
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    return (isinstance(error, dict)
            and error.get("code") == "cost_limit_exceeded"
            and error.get("message") in _ADMISSION_REJECTION_MESSAGES)


def admission_retries_total() -> int:
    return _ADMISSION_RETRIES_TOTAL[0]


async def _create_with_admission_retry(create, args, kwargs, *,
                                       max_wait: float | None = None):
    budget = ADMISSION_RETRY_MAX_WAIT_SEC if max_wait is None else max_wait
    deadline = time.monotonic() + budget
    delay = 2.0
    while True:
        try:
            return await create(*args, **kwargs)
        except Exception as exc:
            pause = delay * (0.5 + random.random())
            if (not is_local_admission_rejection(exc)
                    or time.monotonic() + pause > deadline):
                raise
            _ADMISSION_RETRIES_TOTAL[0] += 1
            logging.getLogger("kg_hub.gateway").warning(
                "[gateway_admission] %s; retry in %.1fs", exc.body["error"]["message"], pause)
            await asyncio.sleep(pause)
            delay = min(delay * 2, 30.0)


def offscript_total() -> int:
    """进程累计:模型被强制调工具却回了文本的次数。"""
    return _OFFSCRIPT_TOTAL[0]


def _note_offscript() -> None:
    _OFFSCRIPT_TOTAL[0] += 1
    current = _repairs.get()
    if current is not None:
        current["offscript"] = current.get("offscript", 0) + 1


def note_offscript_if_missing_tool_use(kwargs: dict, response: Any) -> None:
    """强制调工具却没回 tool_use → 记一次脱稿。任何异常都吞掉。"""
    try:
        choice = kwargs.get("tool_choice")
        if not (isinstance(choice, dict) and choice.get("type") in ("tool", "any")):
            return
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "tool_use":
                return
        _note_offscript()
    except Exception:  # noqa: BLE001
        logging.getLogger("kg_hub.gateway").warning(
            "[offscript_check] skipped (non-fatal)", exc_info=True)


def envelope_repairs_total() -> dict[str, int]:
    """进程累计的外壳修正次数(按形态)。/health 对外播这个。"""
    return dict(_REPAIRS_TOTAL)


def _note_repair(shape: str) -> None:
    _REPAIRS_TOTAL[shape] = _REPAIRS_TOTAL.get(shape, 0) + 1
    current = _repairs.get()
    if current is not None:
        current[shape] = current.get(shape, 0) + 1


def _repair_field(key: str, value: Any) -> tuple[Any, str | None]:
    """返回 (修好的值, 形态名);不认识的形状原样返回、形态名 None。"""
    # A:整个列表/对象被序列化成字符串。只认能解析出 list/dict 的,别把普通文本
    #   字段(比如某个 summary)误当成 JSON。
    if isinstance(value, str):
        text = value.strip()
        shape = "json_string"
        # 2026-09-29 线上 _TimestampResponse / NodeResolutions 各一次:内容完整的
        # JSON 列表前多了一个 `>` 和换行。只认这一个字符,不做通用的前缀剥离。
        if text[:1] == ">":
            text, shape = text[1:].lstrip(), "prefixed_json_string"
        # 首字符就是那道闸,别再在后面加一个 isinstance(parsed, (list, dict)) ——
        # 有了这一句,json.loads 要么抛,要么只能得出 list / dict,那个 isinstance
        # 永远为真。**写一个走不到的分支比不写更坏:它看起来像在处理一种情况**
        # (2026-09-21 变异验证抓到的:摘掉它没有任何用例转红)。
        #
        # 闸挡住的是标量:一个正当的字符串字段写着 "123" 或 "null",不该被悄悄
        # 换成整数 123 或 None —— 那是改类型,不是修外壳。
        if text[:1] not in ("[", "{"):
            return value, None
        try:
            return json.loads(text), shape
        except ValueError:
            pass
        mended, fixes = _mend_brackets(text)
        try:
            return json.loads(mended), "+".join(fixes)
        except ValueError:
            return value, None
    # B:外壳多包一层,且内层用的是同一个字段名 —— 同名是关键,它把「多包一层」
    #   和「一个正当的单元素列表」区分开。
    if (isinstance(value, list) and len(value) == 1
            and isinstance(value[0], dict) and set(value[0]) == {key}
            and isinstance(value[0][key], list)):
        return value[0][key], "double_wrapped"
    return value, None


_CLOSER_OF = {"[": "]", "{": "}"}


def _mend_brackets(text: str) -> tuple[str, list[str]]:
    """只修字符串**外面**的括号错,返回 (修后文本, 用到的规则)。

    2026-09-29 线上原文,整串 json.loads 失败、其余全部完好:
      ExtractedEntities   ..."episode_indices": [0)}, {"name": ...
      SummarizedEntities  ..."attributes": {}}}]
    字符串外的 `)` 在 JSON 里从来不合法,对着 `[` 时只能是 `]`;对不上栈顶的
    闭合括号只能是多写的那一个。字符串里的括号一律不动 —— summary 里写着
    "见 [1)" 是正文。修完仍须整串 json.loads 成功才算数,少写的括号修不出来。
    """
    out: list[str] = []
    stack: list[str] = []
    fixes: list[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in _CLOSER_OF:
            stack.append(ch)
        elif ch == ")" and stack and stack[-1] == "[":
            stack.pop()
            out.append("]")
            if "paren_as_bracket" not in fixes:
                fixes.append("paren_as_bracket")
            continue
        elif ch in ("]", "}"):
            if not stack or _CLOSER_OF[stack[-1]] != ch:
                if "stray_closer" not in fixes:
                    fixes.append("stray_closer")
                continue
            stack.pop()
        out.append(ch)
    return "".join(out), fixes


def _tool_schemas(tools: Any) -> dict[str, dict]:
    schemas = {}
    for tool in tools or []:
        if isinstance(tool, dict) and isinstance(tool.get("input_schema"), dict):
            schemas[str(tool.get("name"))] = tool["input_schema"]
    return schemas


def _unwrap_object(payload: dict, schema: dict | None) -> dict | None:
    """C:整个参数对象被多包一层 {"result": {...真正的参数...}}。

    只有请求自带的工具 schema 能证明「外层那个键不属于参数、内层恰好是参数」:
    外层唯一的键不在 properties 里,内层的键全在 properties 里且覆盖全部 required。
    """
    if not schema or len(payload) != 1:
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    (outer, inner), = payload.items()
    required = set(schema.get("required") or ())
    if (outer in properties or not isinstance(inner, dict) or not inner
            or not required <= set(inner) or not set(inner) <= set(properties)):
        return None
    return inner


def repair_structured_envelopes(response: Any, tools: Any = None) -> None:
    """就地修正响应里 tool_use 块的 input。

    只改 dict 的内容,不给 SDK 模型对象赋属性(那些 pydantic 对象可能不可变)。
    任何异常都吞掉:修正是锦上添花,绝不能让它把一次已经付过费的调用弄失败。
    """
    try:
        schemas = _tool_schemas(tools)
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            payload = getattr(block, "input", None)
            if not isinstance(payload, dict):
                continue
            inner = _unwrap_object(payload, schemas.get(str(getattr(block, "name", ""))))
            if inner is not None:
                payload.clear()
                payload.update(inner)
                _note_repair("wrapped_object")
            for key in list(payload):
                fixed, shape = _repair_field(key, payload[key])
                if shape is not None:
                    payload[key] = fixed
                    _note_repair(shape)
    except Exception:  # noqa: BLE001
        logging.getLogger("kg_hub.gateway").warning(
            "[envelope_repair] skipped (non-fatal)", exc_info=True)


@contextmanager
def model_operation(namespace: str, operation_id: str):
    """Bind paid substeps to a durable business operation identity.

    yield 出来的 dict 是**本次操作**的外壳修正计数,调用方要的话可以落账。"""
    namespace = str(namespace).strip()
    operation_id = str(operation_id).strip()
    if (_OPERATION_PART_PATTERN.fullmatch(namespace) is None
            or not operation_id or len(operation_id) > 1024
            or any(ord(ch) < 0x20 for ch in operation_id)):
        raise RuntimeError("invalid durable model operation identity")
    token = _operation.set((namespace, operation_id))
    parent_tally = _repairs.get()
    tally: dict[str, int] = {}
    repairs_token = _repairs.set(tally)
    try:
        yield tally
    finally:
        _operation.reset(token)
        _repairs.reset(repairs_token)
        if parent_tally is not None:
            for shape, count in tally.items():
                parent_tally[shape] = parent_tally.get(shape, 0) + count


@contextmanager
def model_usage_scenario(scenario: str | None):
    """Attach a reporting-only scenario without changing durable idempotency."""
    if scenario is not None:
        scenario = str(scenario).strip()
        if _USAGE_SCENARIO_PATTERN.fullmatch(scenario) is None:
            raise RuntimeError("invalid model usage scenario")
    token = _usage_scenario.set(scenario)
    try:
        yield
    finally:
        _usage_scenario.reset(token)


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
                                     thinking_disabled: bool = True, queue_transport=None) -> Any:
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
        # identity or usage scenario. Remove every casing variant before
        # digesting and forwarding; the central layer remains the sole authority
        # for both fields.
        for name in list(headers):
            if name.lower() in {
                "idempotency-key", "x-model-gateway-scenario",
                "x-model-gateway-task-ids", "x-model-gateway-step-id",
            }:
                del headers[name]
        key_kwargs = dict(kwargs)
        key_kwargs["extra_headers"] = headers
        operation = _operation.get()
        if operation is not None:
            headers["Idempotency-Key"] = _durable_idempotency_key(
                operation[0], operation[1], args, key_kwargs
            )
            # The namespace is application-owned, bounded by
            # _OPERATION_PART_PATTERN, and remains a reporting label only. It
            # cannot select a route, credential, model, or billing policy.
            scenario = _usage_scenario.get() or operation[0]
            if _USAGE_SCENARIO_PATTERN.fullmatch(scenario) is not None:
                headers["X-Model-Gateway-Scenario"] = scenario
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
        task = _business_task.get()
        journal = await asyncio.to_thread(journal_from_backup_env) if task else None
        request_digest = ""
        step_id = ""
        if journal and task:
            request_kwargs = dict(kwargs)
            request_headers = dict(request_kwargs.get("extra_headers") or {})
            request_headers.pop("Idempotency-Key", None)
            request_kwargs["extra_headers"] = request_headers
            request_digest = hashlib.sha256(json.dumps(
                _canonical({"args": args, "kwargs": request_kwargs}),
                ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            step_id = hashlib.sha256(
                f"{task[0]}\x00{task[1]}\x00{request_digest}".encode("utf-8")
            ).hexdigest()
        collector = _stage_steps.get()
        if collector is not None and step_id:
            collector.add(step_id)
        resume = _resume.get()
        reserved_by_grant = False
        if resume is not None:
            if not journal or task != resume["task"]:
                raise RuntimeError("manual resume has no durable task journal")
            if not resume["consumed"]:
                if step_id == resume["step_id"]:
                    key = journal.claim_retry(
                        resume["grant_id"], source_description=task[0],
                        source_obs_id=task[1], step_id=step_id,
                        request_digest=request_digest,
                        business_key=str(kwargs.get("model") or gateway_model()),
                        base_key=key, deadline_seconds=MIN_CLIENT_TIMEOUT_SEC,
                        stage=_model_stage.get(), execution_id=resume.get("execution_id"),
                        queue_owned=queue_transport is not None)
                    headers["Idempotency-Key"] = key
                    resume["consumed"] = True
                    reserved_by_grant = True
                elif not any(row["step_id"] == step_id for row in journal.find_task(*task)):
                    raise NeedsReconciliation(step_id, "resume_input_drift", None)
            elif (step_id not in {row["step_id"] for row in journal.find_task(*task)}
                  and resume["cached_pending"]):
                raise NeedsReconciliation(step_id, "resume_unreplayed_paid_steps", None)
        pending = inflight.get(key)
        if pending is not None:
            # shield:等待方被取消不能连带取消发起方的结果
            return await asyncio.shield(pending)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        inflight[key] = future
        prepared = False
        try:
            if min_interval > 0 and queue_transport is None:
                async with throttle_lock:
                    wait = min_interval - (time.monotonic() - last_call["at"])
                    if wait > 0:
                        await asyncio.sleep(wait)
                    last_call["at"] = time.monotonic()
            if journal and task and not reserved_by_grant:
                cached_result = await asyncio.to_thread(journal.prepare,
                    key=key, business_key=str(kwargs.get("model") or gateway_model()),
                    source_description=task[0], source_obs_id=task[1],
                    step_id=step_id, request_digest=request_digest,
                    stage=_model_stage.get(), queue_owned=queue_transport is not None,
                )
                if cached_result is not None:
                    from anthropic.types import Message
                    result = Message.model_validate_json(cached_result)
                    # 账本存的是当时修正规则下的结果;重放时用现行规则再修一遍,
                    # 否则一次外壳错会随同一请求的每次重试原样重现。
                    repair_structured_envelopes(result, kwargs.get("tools"))
                    if resume is not None:
                        resume["cached_pending"].discard(step_id)
                    future.set_result(result)
                    return result
                prepared = True
            elif reserved_by_grant:
                prepared = True
            if journal and prepared and queue_transport is None:
                # Commit the local HTTP-start boundary before entering the SDK.
                # A crash/timeout after this point is one failed business
                # model attempt once the maximum timeout expires, even when
                # gateway provider admission remains unknown.
                await asyncio.to_thread(journal.start_http, key)
            wire_token = (_wire_attempt.set((key, step_id))
                          if journal and task else None)
            try:
                if queue_transport is not None:
                    if args:
                        raise RuntimeError('queue transport requires named parameters')
                    async def record_wire(digest):
                        if journal and task:
                            await asyncio.to_thread(journal.record_gateway_step,
                                *task, key, digest, digest,
                                mailbox_step_id=resume.get("gateway_step_id") if reserved_by_grant else None)
                    result = await queue_transport(key, kwargs, task, record_wire)
                else:
                    result = await _create_with_admission_retry(original_create, args, kwargs)
            finally:
                if wire_token is not None:
                    _wire_attempt.reset(wire_token)
            # 付过费的答案已经拿到了,外壳错不该让它作废。就地修正 + 计数。
            repair_structured_envelopes(result, kwargs.get("tools"))
            note_offscript_if_missing_tool_use(kwargs, result)
            if journal and prepared:
                await asyncio.to_thread(journal.complete, key, result.model_dump_json())
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except Exception as exc:
            if journal and prepared and queue_transport is None:
                try:
                    status = await asyncio.to_thread(
                        query_gateway_attempt_status, gateway_base_url(), gateway_token(),
                        str(kwargs.get("model") or gateway_model()), key,
                    )
                except Exception:
                    status = {"phase": "unknown", "provider_call_started": None}
                reconciliation = await asyncio.to_thread(journal.update_gateway_status, key, status)
                if reconciliation.provider_call_started is not False:
                    exc = reconciliation
            if not future.done():
                future.set_exception(exc)
                future.exception()  # 本处已 raise;避免无等待方时的 never-retrieved 噪音
            raise exc
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
    import httpx
    from anthropic import AsyncAnthropic

    timeout = enforced_client_timeout(timeout)

    # Validate every boundary before the SDK constructor can create a transport.
    base_url = gateway_base_url()
    model = gateway_model()
    if not model.startswith("kg_hub."):  # defensive after canonical validation
        raise RuntimeError("invalid kg-hub business key")
    auth_token = gateway_token()

    wire_client = httpx.AsyncClient(
        timeout=timeout,
        event_hooks={"request": [gateway_task_correlation_request_hook]},
    )
    client = AsyncAnthropic(
        auth_token=auth_token,
        base_url=base_url,
        max_retries=0,
        timeout=timeout,
        http_client=wire_client,
    )
    async def queued_transport(key, kwargs, task, record_wire):
        from utils.gateway_queue import execute_queued, request_body
        return await execute_queued(base_url, auth_token, key, request_body(kwargs),
            task_ids=[task_uuid(*task)] if task else None,
            scenario=kwargs['extra_headers'].get('X-Model-Gateway-Scenario', 'unclassified'),
            before_submit=record_wire)
    return install_gateway_request_contract(
        client, min_interval=min_interval, thinking_disabled=thinking_disabled,
        queue_transport=queued_transport,
    )
