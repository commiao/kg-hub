"""utils/origin.py — 确定性派生一等来源/内容元数据(北极星 §2 契约)。

纯函数,无 LLM、无 IO。server(do_extract)与回填工具(tools/backfill_schema.py)
共用同一套派生,保证新摄入与存量口径一致。

产出字段:
  origin_device   mac | oc-vps | nas | unknown        （知识产生的设备)
  origin_tool     claude-code|cursor|codex|qoder|openclaw|claude-mem|kg-hub|unknown
  origin_project  项目/工作区名，取 project= 或按前缀推，取不到=unknown
  durability      evergreen | time-bound              （时效,给"当前真相"类查询过滤)
  kind + conf     方法论/手册/决策/事故/项目事实/生命周期事件/素材/公开故事/unclassified
                  —— 仅确定性可判的在此定;判不了返回 (unclassified, 0.0) 交给 LLM 分类器 pass

北极星铁律:自报不可信,确定性派生优先。此模块只做"能确定性算出来的",
含糊的一律 unclassified(交 ④ 分类器或人工),不猜。
"""

from __future__ import annotations

import re

KINDS = ("方法论", "手册", "决策", "事故", "项目事实", "生命周期事件", "素材", "公开故事", "unclassified")
DURABILITY = ("evergreen", "time-bound")

_PROJECT_RE = re.compile(r"project=(\S+)")
_TYPE_RE = re.compile(r"type=(\S+)")

# project=workspace_<tool> 的工具映射(claude-mem 用工作区名编码工具)
_WORKSPACE_TOOL = {
    "workspace_claudecode": "claude-code",
    "workspace_cursor": "cursor",
    "workspace_codex": "codex",
    "workspace_qoder": "qoder",
}

# claude-mem/codex type= → kind(真实词表见 2026-07-23 统计:discovery/feature/
# bugfix/decision/change/refactor/security_note)。粗粒度分面用,coding 产物多为
# 项目事实;S1 召回主要靠 project+相关性,kind 只做粗过滤,故这里从宽不留大量 unclassified。
_TYPE_KIND = {
    "decision": ("决策", 0.85),
    "bugfix": ("事故", 0.82),
    "security_note": ("事故", 0.72),
    "feature": ("项目事实", 0.8),
    "refactor": ("项目事实", 0.8),
    "change": ("项目事实", 0.78),
    "discovery": ("项目事实", 0.7),   # 最含糊:多为工作中的发现/事实,粗归项目事实
}

# time-bound 强信号:周期性报告/行情/快照。**只看标题(name)**——标题是可靠信号
# (报告就叫"金价晚报""daily-extract");正文提到"黄金法则""写日报的方法"是长期方法论,
# 不该被误判 time-bound(审查发现#3)。宁可漏判 evergreen(不排除),不误判 time-bound(会排除)。
_TIMEBOUND = re.compile(
    r"行情|快照|日报|晚报|早报|金价|黄金|大盘|收盘|盘前|盘后|阅读量|流量|"
    r"investment-watch|gold-market|daily-extract|advisory")


def derive_origin(name: str, source_description: str) -> dict:
    name = name or ""
    sd = source_description or ""
    sd_l = sd.lower()

    # device
    if name.startswith("openclaw-capsule-") or "openclaw" in sd_l:
        device = "oc-vps"
    else:
        device = "mac"  # claude-mem / codex / canonical / case-pack 皆产于 Mac

    # tool
    if name.startswith("openclaw-capsule-") or sd_l.startswith("openclaw"):
        tool = "openclaw"
    elif sd_l.startswith("codex"):
        tool = "codex"
    elif name.startswith("kg-hub-canonical") or sd_l.startswith("kg-hub-canonical") or sd_l.startswith("case-pack"):
        tool = "kg-hub"
    elif "claude-code" in sd_l:
        tool = "claude-code"
    elif sd_l.startswith("claude-mem"):
        m = _PROJECT_RE.search(sd)
        ws = (m.group(1).lower() if m else "")
        tool = _WORKSPACE_TOOL.get(ws, "claude-mem")  # 工作区未编码工具时=capture 工具本身
    else:
        tool = "unknown"

    # project
    m = _PROJECT_RE.search(sd)
    if m:
        project = m.group(1)
    elif tool == "openclaw":
        project = "openclaw"
    elif tool == "kg-hub":
        project = "kg-hub"
    else:
        project = "unknown"

    return {"origin_device": device, "origin_tool": tool, "origin_project": project}


def derive_durability(name: str, content: str) -> str:
    # 只扫标题:周期性报告/快照的标题可靠地含这些词;正文提及是噪音(审查#3)。
    return "time-bound" if _TIMEBOUND.search(name or "") else "evergreen"


def derive_kind(name: str, source_description: str) -> tuple[str, float]:
    """确定性可判的 kind + 置信度;判不了返回 ('unclassified', 0.0)。
    openclaw 胶囊(无 type=)一律走这里返回 unclassified → 交 LLM 分类器 pass。"""
    name = name or ""
    sd = source_description or ""
    if name.startswith("kg-hub-canonical"):
        return ("手册", 0.9)
    m = _TYPE_RE.search(sd)
    if m:
        return _TYPE_KIND.get(m.group(1).lower(), ("unclassified", 0.0))
    return ("unclassified", 0.0)


# ④ LLM 分类器 pass 的 prompt + 解析(纯,不发 LLM)。server 与回填共用,单一事实源。
KIND_PROMPT = """给下面这条知识胶囊分类。只输出 JSON,形如 {{"kind":"方法论","confidence":0.82}}。
kind 必须是以下之一:
- 方法论: 可复用的做法/框架/原则("怎么做"类)
- 手册: 操作指南/配置步骤/接入文档
- 决策: 一次性技术或业务选择及其理由
- 事故: 故障/bug/事后复盘
- 项目事实: 某项目的状态/配置/进展等事实陈述
- 生命周期事件: 项目的阶段变更/里程碑/演进节点
- 素材: 供内容创作引用的原材料(数据/观点/案例片段)
- 公开故事: 已成文、可对外的叙事
判定要点(校准发现的坑):**整篇是每日/系统运行日报、流水账式汇总 → 项目事实**,
即使正文顺带提到某个故障/超时/错误也不算"事故";只有整篇聚焦单一故障的复盘才算"事故"。
confidence 是你对该判定的把握(0~1)。拿不准就给低分。只输出 JSON,不要解释。

胶囊内容:
{body}"""


def parse_kind_json(raw: str) -> tuple[str, float]:
    """从 LLM 回复里解析 (kind, confidence);非法/越界 → ('unclassified', 0.0)。纯函数。"""
    import json as _json
    import re as _re
    try:
        m = _re.search(r"\{.*\}", raw or "", _re.S)
        if not m:
            return ("unclassified", 0.0)
        obj = _json.loads(m.group(0))
        kind = str(obj.get("kind", "")).strip()
        conf = float(obj.get("confidence", 0.0))
        if kind not in KINDS or kind == "unclassified":
            return ("unclassified", 0.0)
        return (kind, max(0.0, min(1.0, conf)))
    except Exception:
        return ("unclassified", 0.0)


def derive_all(name: str, source_description: str, content: str) -> dict:
    d = derive_origin(name, source_description)
    d["durability"] = derive_durability(name, content)
    kind, conf = derive_kind(name, source_description)
    d["kind"] = kind
    d["kind_confidence"] = conf
    return d
