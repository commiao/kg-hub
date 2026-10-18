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
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

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
SINGLE_CALL_ADDENDUM = """
## 本次调用的补充约定（网关单次抽取）

- 本次只有**一条**观测，直接返回 `{"entities": [...], "edges": [...]}`：不要 `items` 包装，不要 `id` 字段。
- 没有 `lookahead`、`names-<model>.tsv`、`submit --dry-run`，规则 19、20 不适用；其余规则照常。
- 实体与关系**一次给全**：先列实体，再给关系，关系的 source/target 必须逐字等于某个实体的 name。
- 结果通过提供的工具返回，不要输出任何解释文字。
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
    """返回 (提示词正文, prompt_version)。版本号取自文件首行的 ``prompt_version = agent-vN``。"""
    text = path.read_text(encoding="utf-8").strip()
    match = _VERSION_RE.search(text[:500])
    if match is None:
        raise RuntimeError(f"{path} 首部没有 prompt_version = ... 标记")
    return text, match.group(1)


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


class SinglePassExtraction(BaseModel):
    """一条观测抽出的实体与关系，字段与 kg-hub schema v0.2 的预抽取入库请求一致。"""

    # entities 必填：没有这个键的答案是形状错误（要报 ValidationError 走 reject），不是「空观测」；
    # 空观测按提示词规则 8 是显式的 entities: []。
    entities: list[ExtractedEntity]
    edges: list[ExtractedEdge] = Field(default_factory=list)

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
        edges = data.get("edges", [])
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


@dataclass
class SinglePassResult:
    entities: list[ExtractedEntity]
    edges: list[ExtractedEdge]
    prompt_version: str
    raw: dict = field(default_factory=dict)


async def extract_single_pass(llm, *, name: str, episode_body: str, source_description: str,
                              reference_time: datetime, prompt: tuple[str, str] | None = None,
                              max_tokens: int | None = None) -> SinglePassResult:
    """经网关客户端调**一次**模型，返回解析好的实体/关系。

    llm 需要 graphiti LLMClient 的 ``generate_response(messages, response_model=, max_tokens=,
    prompt_name=)`` 契约；线上传 graphiti.llm_client（SingleAttemptAnthropicClient）：它只尝试一次，
    验证失败时标记账本里的答案不可用再抛出，这里不捕获、不追问。stage 由本函数绑定为
    ``single_pass``，所以账本按阶段统计时能把它与 graphiti 的五个阶段分开。
    """
    from model_gateway_client import model_stage

    prompt_text, prompt_version = prompt or load_prompt()
    messages = build_messages(prompt_text, name=name, episode_body=episode_body,
                              source_description=source_description, reference_time=reference_time)
    with model_stage(MODEL_STAGE):
        response = await llm.generate_response(
            messages, response_model=SinglePassExtraction, max_tokens=max_tokens,
            prompt_name=PROMPT_NAME)
    parsed = SinglePassExtraction.model_validate(response)
    return SinglePassResult(entities=parsed.entities, edges=parsed.edges,
                            prompt_version=prompt_version, raw=dict(response))
