"""网关路径的「单次抽取」：一次模型调用同时拿到实体与关系，结果交给 extracted_ingest 落图。

为什么要有这条路（2026-10-19）：graphiti 0.29 的 add_episode 把一条观测拆成节点抽取、消解、
抽边、去重、属性五个阶段，经网关每条 7.07 次调用、67.6k 输入 token；其中「抽边」那一次调用
直接返回 ``edges: []`` 的占 20%（docs/extraction-model-comparison-20261017.md §4.2），追问重试
治不了根。对照：会话自抽取路径一次调用同时出实体和关系，三轮 1,500 条零边 0–0.2%、合规 100%。
本模块把那条路的提示词（config/agent_extract_prompt.md）搬到网关路径上：system 用提示词正文，
user 给观测，强制模型以工具调用返回 ``{entities, edges}``，经**同一个**网关客户端
（graphiti_client.SingleAttemptAnthropicClient，业务键 kg_hub.entity_extract，Idempotency 账本）
只调一次；解析失败由客户端按既有 reject_model_result 处理，不追问、不重试。

本模块只管「组提示 → 调一次 → 解析成 ExtractedEntity/ExtractedEdge」，不读图不写图；
落图与 IngestedKey/断路器/held 逻辑在 kg_hub_server 里与 graphiti 模式共用。
"""
from __future__ import annotations

import os
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from utils.extracted_ingest import (
    ExtractedEntity, ExtractedEdge, MAX_EDGES, MAX_ENTITIES, MAX_FACT_LEN, MAX_NAME_LEN,
    MAX_SUMMARY_LEN,
)

try:  # graphiti 的消息模型；没装 graphiti_core 的宿主（如纯逻辑测试）用同形的替身
    from graphiti_core.prompts.models import Message
except ImportError:  # pragma: no cover - 容器与 venv 都有 graphiti_core
    class Message(BaseModel):  # type: ignore[no-redef]
        role: str
        content: str

# ---------- 入图模式开关 ----------
INGEST_MODE_GRAPHITI = "graphiti"
logger = logging.getLogger("kg_hub.single_pass")

INGEST_MODE_SINGLE_PASS = "single_pass"
INGEST_MODES = (INGEST_MODE_GRAPHITI, INGEST_MODE_SINGLE_PASS)
INGEST_MODE_ENV = "KG_HUB_INGEST_MODE"
# 落图来源标记：与会话自抽取的 'agent'、graphiti 归一后的 'gateway' 并列，看板与 extractor_compare 按它分组
EXTRACTION_MODE = "gateway_single"
# 账本里这一次调用的 stage 名（graphiti 模式是 node_extraction/edge_phase/...）
MODEL_STAGE = "single_pass"
PROMPT_NAME = "single_pass.extract"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "agent_extract_prompt.md"
_VERSION_RE = re.compile(r"prompt_version\s*=\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# 提示词是给会话执行者写的（多条 items、lookahead、names 文件、dry-run）；网关一次只给一条观测，
# 这段补充把那些不适用的约定关掉，并把输出形状钉死为工具入参。
# 单次调用补充约定的版本，拼在 prompt_version 后面（agent-v4+sp4），让同为 gateway_single 的批次可按规则版本分开比。
# sp1：#190/#195 首版；sp2：2026-10-19 fact 改英文完整句 60–200 字符（按条可检索率 29% → 复测）；
# sp3：2026-10-22 三模型 2000 条对比（T-0346）的链路结论移植：标题原文 + 另一语言译文两条标题结论边、增量观测、推断标注。
# sp4：那些链路规则已并入提示词正文（agent-v4 规则 21–28，会话执行者与网关共用），这里只留单次调用特有的约定。
ADDENDUM_VERSION = "sp4"
SINGLE_CALL_ADDENDUM = """
## 本次调用的补充约定（网关单次抽取，sp4）

- 本次只有**一条**观测，直接返回 `{"entities": [...], "edges": [...]}`：不要 `items` 包装，不要 `id` 字段。
- 没有 `lookahead`、`names-<model>.tsv`、`submit --dry-run`，规则 19、20、28 不适用；其余规则照常。
- 实体与关系**一次给全**：先列实体，再给关系，关系的 source/target 必须逐字等于某个实体的 name。
- 只输出**一个 JSON 对象**：不要 Markdown 代码围栏，不要解释文字，不要调用工具。
- `edges` 键必须存在；确实没有关系时写 `"edges": []`。边的 name 只用 schema 里的关系名。
- 规则 21 的标题原文边服务端会核对，缺了会自动补上；规则 22 的译文边仍要你写。
- 规则 23 的例子：`kg-hub-server depends_on FalkorDB because every /api/ingest write goes through the writer.lock on the kg_hub graph`。
""".strip()


def resolve_ingest_mode(raw: str | None) -> str:
    """把环境变量解析成合法模式；空值 = graphiti（线上默认，保证可切回），非法值报错而不是静默回退。"""
    value = (raw or "").strip().lower() or INGEST_MODE_GRAPHITI
    if value not in INGEST_MODES:
        raise RuntimeError(
            f"{INGEST_MODE_ENV}={raw!r} 不合法，只能是 {'|'.join(INGEST_MODES)}")
    return value


def ingest_mode_from_env() -> str:
    return resolve_ingest_mode(os.environ.get(INGEST_MODE_ENV))


def load_prompt(path: Path = PROMPT_PATH) -> tuple[str, str]:
    """返回 (提示词正文, prompt_version)。版本号取自文件首行的 ``prompt_version = agent-vN``，
    再拼上单次调用补充约定的版本（``agent-vN+spM``）。"""
    text = path.read_text(encoding="utf-8").strip()
    match = _VERSION_RE.search(text[:500])
    if match is None:
        raise RuntimeError(f"{path} 首部没有 prompt_version = ... 标记")
    return text, f"{match.group(1)}+{ADDENDUM_VERSION}"


def build_messages(prompt_text: str, *, name: str, episode_body: str,
                   source_description: str, reference_time: datetime) -> list[Message]:
    """system = 提示词正文 + 单次调用补充；user = 观测元数据 + 正文。纯函数，便于测试与幂等钥匙稳定。"""
    system = prompt_text.rstrip() + "\n\n" + SINGLE_CALL_ADDENDUM
    user = (
        f"观测名称：{name}\n"
        f"来源：{source_description}\n"
        f"参考时间：{reference_time.isoformat()}\n\n"
        f"观测正文：\n{episode_body}"
    )
    return [Message(role="system", content=system), Message(role="user", content=user)]


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text[:limit]


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_object(text: str) -> dict:
    """从模型文本里取出那一个 JSON 对象：先剥代码围栏，再整体 json.loads，最后退到首个 ``{``
    到末个 ``}`` 之间。拿不到对象、或对象缺 ``entities`` / ``edges`` 数组（空观测允许只有
    ``entities: []``）都抛 ValueError，由调用方按形状错误处理。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("model returned no text")
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    data = None
    for cand in candidates:
        try:
            data = json.loads(cand)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        raise ValueError("no JSON object in model text")
    if "entities" not in data and isinstance(data.get("items"), list) and len(data["items"]) == 1 \
            and isinstance(data["items"][0], dict):
        data = data["items"][0]
    entities = data.get("entities")
    if not isinstance(entities, list):
        raise ValueError("JSON object lacks entities array")
    if "edges" not in data:
        if entities:
            raise ValueError("JSON object lacks edges array")
        data = {**data, "edges": []}
    elif not isinstance(data["edges"], list):
        raise ValueError("edges is not an array")
    return data


class SinglePassExtraction(BaseModel):
    """一条观测抽出的实体与关系，字段与 kg-hub schema v0.2 的预抽取入库请求一致。"""

    # entities 必填：没有这个键的答案是形状错误（要报 ValidationError 走 reject），不是「空观测」；
    # 空观测按提示词规则 8 是显式的 entities: []。
    entities: list[ExtractedEntity]
    # edges 也必填：2026-10-19 首批实测，键可省略时 deepseek 11 次里 11 次不写它，边全丢。
    # 只有空观测（entities 为空）允许省略 edges。
    edges: list[ExtractedEdge]

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        """付过费的答案尽量用上：容忍 ``{"items": [一条]}`` 包装与多余的 ``id``，补齐空 summary，
        超长字段截断，缺 source/target/name 的边丢弃，超出条数上限的尾部丢弃。
        形状完全不对（不是对象、缺 entities、entities 不是数组）仍交给 pydantic 报 ValidationError，
        由调用方按 reject_model_result 处理。"""
        if not isinstance(data, dict):
            return data
        if "entities" not in data and isinstance(data.get("items"), list) and len(data["items"]) == 1 \
                and isinstance(data["items"][0], dict):
            data = data["items"][0]
        entities = data.get("entities")
        if "edges" not in data and isinstance(entities, list) and not entities:
            data = {**data, "edges": []}          # 空观测：规则 8 只要求 entities: []
        edges = data.get("edges")
        if not isinstance(entities, list) or not isinstance(edges, list):
            return data
        out_entities = []
        for item in entities[:MAX_ENTITIES]:
            if not isinstance(item, dict):
                continue
            name = _clip(item.get("name"), MAX_NAME_LEN)
            if not name:
                continue
            out_entities.append({
                "name": name,
                "type": _clip(item.get("type"), 40) or "Concept",
                "summary": _clip(item.get("summary"), MAX_SUMMARY_LEN),
            })
        out_edges = []
        for item in edges[:MAX_EDGES]:
            if not isinstance(item, dict):
                continue
            source = _clip(item.get("source"), MAX_NAME_LEN)
            target = _clip(item.get("target"), MAX_NAME_LEN)
            rel = _clip(item.get("name"), 80)
            if not (source and target and rel):
                continue
            fact = _clip(item.get("fact"), MAX_FACT_LEN) or f"{source} {rel} {target}"
            out_edges.append({"source": source, "target": target, "name": rel, "fact": fact})
        return {"entities": out_entities, "edges": out_edges}


_TITLE_PREFIX = re.compile(r"^\s*(?:\[\[?\s*[A-Z][A-Z_ -]*\s*\]\]?\s*|\[?\*\*title\*\*:\s*)+")   # 类型只认大写


def observation_title(episode_body: str) -> str:
    """观测正文第一行去掉 [TYPE] / **title**: 前缀后的标题；没有就返回空串。"""
    first = (episode_body or "").split("\n", 1)[0]
    title = _TITLE_PREFIX.sub("", first).strip()
    if (first.lstrip().startswith("[**title**") or title.startswith("[")) and title.endswith("]"):
        title = title[:-1].rstrip()                 # 「[**title**: …]」「[[DECISION]] [ … ]」的配对括号
    if title.startswith("["):
        title = title[1:].lstrip()
    return title[:MAX_FACT_LEN]


def _norm_fact(text: str) -> str:
    return " ".join((text or "").split()).lower()


def ensure_headline_edge(entities: list, edges: list, title: str) -> tuple[list, bool]:
    """确定性补齐「标题原文」这条边（三模型对比实验：照抄标题后三模型可检索率都到 96–100%，
    与模型改写能力无关）。已有一条 fact 以标题原文开头的边就不动；否则在最前面补一条 relates_to，
    两端取模型第一条边的两端，没有边就取前两个实体；实体少于 2 个时无法成边，不补。返回 (edges, 是否补了)。"""
    t = _norm_fact(title)
    if not t or any(_norm_fact(e.fact).startswith(t) for e in edges):
        return edges, False
    if edges:
        src, dst = edges[0].source, edges[0].target
    elif len(entities) >= 2:
        src, dst = entities[0].name, entities[1].name
    else:
        return edges, False
    return [ExtractedEdge(source=src, target=dst, name="relates_to", fact=title)] + list(edges), True


@dataclass
class SinglePassResult:
    entities: list[ExtractedEntity]
    edges: list[ExtractedEdge]
    prompt_version: str
    raw: dict = field(default_factory=dict)
    headline_injected: bool = False


async def extract_single_pass(llm, *, name: str, episode_body: str, source_description: str,
                              reference_time: datetime, prompt: tuple[str, str] | None = None,
                              max_tokens: int | None = None) -> SinglePassResult:
    """经网关客户端调**一次**模型，返回解析好的实体/关系。

    llm 需要 graphiti LLMClient 的 ``generate_response(messages, response_model=, max_tokens=,
    prompt_name=)`` 契约；线上传 graphiti.llm_client（SingleAttemptAnthropicClient）：它只尝试一次，
    验证失败时标记账本里的答案不可用再抛出，这里不捕获、不追问。stage 由本函数绑定为
    ``single_pass``，所以账本按阶段统计时能把它与 graphiti 的五个阶段分开。
    """
    from model_gateway_client import model_stage, reject_model_result

    prompt_text, prompt_version = prompt or load_prompt()
    messages = build_messages(prompt_text, name=name, episode_body=episode_body,
                              source_description=source_description, reference_time=reference_time)
    with model_stage(MODEL_STAGE):
        if hasattr(llm, "generate_text"):
            # 文本 JSON 路径（线上）：不挂工具，自己解析；解析失败先把账本里的答案标记不可用再抛出
            text, sink = await llm.generate_text(messages, max_tokens=max_tokens, prompt_name=PROMPT_NAME)
            try:
                response = parse_json_object(text)
                parsed = SinglePassExtraction.model_validate(response)
            except (ValueError, ValidationError) as exc:
                if await reject_model_result(sink):
                    logger.warning("[model_result:rejected] stage=%s reason=%s; the next retry re-asks",
                                   MODEL_STAGE, type(exc).__name__)
                raise
        else:
            # 工具调用路径（只给没有 generate_text 的客户端替身）：形状错误由客户端按 reject 处理
            response = await llm.generate_response(
                messages, response_model=SinglePassExtraction, max_tokens=max_tokens,
                prompt_name=PROMPT_NAME)
            parsed = SinglePassExtraction.model_validate(response)
    edges, injected = ensure_headline_edge(parsed.entities, parsed.edges, observation_title(episode_body))
    return SinglePassResult(entities=parsed.entities, edges=edges, prompt_version=prompt_version,
                            raw=dict(response), headline_injected=injected)
