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
        obs: claude-mem 行 dict，至少含 type / project / narrative / facts。
        cfg: 完整过滤器配置 dict（读其 "ops_noise" 块）。

    Returns:
        True 仅当：type==bugfix 且 project 属自项目 且 命中 >=N 个运维关键词。
    """
    # 钉子①：type gate 前置。非 bugfix 一律 false。
    if (obs.get("type") or "") != "bugfix":
        return False

    oc = cfg.get("ops_noise") or {}
    self_projects = [p.lower() for p in oc.get("self_projects", [])]
    if not self_projects:
        return False
    project = (obs.get("project") or "").lower()
    if not any(sp in project for sp in self_projects):
        return False

    keywords = [k.lower() for k in oc.get("keywords", [])]
    if not keywords:
        return False
    text = ((obs.get("narrative") or "") + " " + _decode(obs.get("facts"))).lower()
    hits = sum(1 for kw in keywords if kw in text)
    return hits >= int(oc.get("min_keyword_hits", 2))
