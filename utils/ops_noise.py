"""运维自指噪音签名 —— kg-hub 三处（WS-3 过滤器 / WS-4 体检器 / WS-2 curate）的单一真相源。

见 docs/LANDING-PLAN-cognitive-asset.md。

设计要点（两颗钉子）：
  ①「只治理 bugfix」：`is_ops_noise` 前置 `type == "bugfix"` gate。kg-hub 自己的
    decision / security_note 即使 narrative 写满 Docker/FalkorDB 也不命中，从源头
    保证「decision/security 零误杀」。
  ②「分类器与开关解耦」：本函数是**纯分类器**，不读 `enabled`。是否据此惩罚由
    过滤器消费端（utils/ingest_filter.py evaluate）用 `cfg["ops_noise"]["enabled"]`
    单独决定。这样体检器在 enabled=false（未武装）时仍能测 ops_noise_share。

签名参数全部来自 `config/ingest_filter.json` 的 `ops_noise` 块，三处不得各写各的。
"""
from __future__ import annotations

import json


def _decode(field_val) -> str:
    """facts 可能是 list / JSON 字符串 / None，一律拍平成可搜索文本。"""
    if not field_val:
        return ""
    if isinstance(field_val, str):
        return field_val
    try:
        return json.dumps(field_val, ensure_ascii=False)
    except Exception:
        return str(field_val)


def is_ops_noise(obs: dict, cfg: dict) -> bool:
    """判定一条 obs 是否为 kg-hub 自身运维自指的 bugfix。

    Args:
        obs: claude-mem 行 dict，含 type / title / narrative / facts。
        cfg: 完整过滤器配置 dict（读其 "ops_noise" 块）。

    Returns:
        True 仅当三者同时成立：
          - type == "bugfix"（钉子①：type gate，decision/security 一律 false）
          - 文本含自我标记 self_markers（如 "kg-hub"）—— 判「是否关于 kg-hub 自身」
          - 文本命中 >= min_keyword_hits 个运维关键词

    为何用文本标记而非 project：WS-4 实测发现这些运维 obs 的 project 全是
    `workspace_claudeCode`（与其它 Claude Code 工作同一个 project），project
    字段无法区分「kg-hub 自身维护」。改用「正文/标题提到 kg-hub」作判据：
    实测命中 26 条真运维、正确排除 3 条非 kg-hub 的 infra bugfix。
    """
    # 钉子①：type gate 前置。非 bugfix 一律 false。
    if (obs.get("type") or "") != "bugfix":
        return False

    oc = cfg.get("ops_noise") or {}
    # 检索文本 = 标题 + 叙事 + facts。摄入期 title 独立存在；图谱期 narrative
    # 传入的是完整 content（首行即标题），两条路径都能覆盖到标题。
    text = " ".join([
        (obs.get("title") or ""),
        (obs.get("narrative") or ""),
        _decode(obs.get("facts")),
    ]).lower()

    # 自我标记：必须提到 kg-hub 本身，才算「自指」。fail-closed：未配则不判噪音。
    markers = [m.lower() for m in oc.get("self_markers", [])]
    if not markers or not any(m in text for m in markers):
        return False

    keywords = [k.lower() for k in oc.get("keywords", [])]
    if not keywords:
        return False
    hits = sum(1 for kw in keywords if kw in text)
    return hits >= int(oc.get("min_keyword_hits", 2))
