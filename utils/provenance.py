"""Provenance classification for OpenClaw knowledge capsules.

OpenClaw's daily capsule extraction pulls from two very different kinds of
sources, and they must not carry equal weight in the shared graph:

  firsthand           — OpenClaw's own ops: MEMORY.md distillation, cron run
                        logs, investment reports, session retros. 亲历、可信。
  external-article    — 二手内容:公众号/掘金/博客文章派生的"知识"。未验证,
                        进图可搜但降权(见 kg_hub_server /api/search)。
  external-community  — 社区采集(小红书/知乎/微博等),同样二手降权。

Classification reads **all** `**来源**:` metadata lines in the capsule —
daily-extract capsules are multi-item documents where each knowledge item has
its own 来源 line, and one node carries one provenance, so any external hit
downgrades the whole capsule (conservative). Per-line rules are precision-first:
only clearly-external patterns count; an ambiguous line stays firsthand
(a false "external" hides real knowledge, a false "firsthand" merely skips
a downweight).

Shared by ingesters/openclaw_capsule.py 之后的补标步骤 (tools/tag_provenance.py,
invoked by sync_openclaw.py after each ingest). 政策见 docs/KNOWLEDGE-GOVERNANCE.md.
"""

from __future__ import annotations

import re

PROV_FIRSTHAND = "firsthand"
PROV_ARTICLE = "external-article"
PROV_COMMUNITY = "external-community"

# `- **来源**: 公众号文章《…》` — capsule metadata line (fullwidth or ASCII colon).
# 也吃 **数据来源**: / **来源会话**: 等变体(kb-001 的 LLM 逐日漂移的写法)。
_SRC_LINE = re.compile(r"\*\*[^*\n]*来源[^*\n]*\*\*\s*[::]\s*(.+)")

# `## 来源` / `### 🔗 来源` 标题式(2026-07-23 起 kb-001 改用) — 取标题后到下一个
# 标题之前的整段,逐行作为来源行参与分类。
_SRC_HEADING = re.compile(r"^#{2,4}[ \t]*[^\n#]*来源[^\n]*$", re.M)
_NEXT_HEADING = re.compile(r"^#{1,6}[ \t]", re.M)

# 文章《…》 covers 公众号文章《, 掘金文章《, 博客文章《 etc. The 《 is the key
# discriminator: "用户查询公众号近5篇文章数据" (ops log) has 文章 but no 《.
_ARTICLE = re.compile(r"文章《")

# xhs / social-search: kb-001 有时把来源写成文件路径别名或 cron 名
# (notes/social-search/topic-runs/xhs_analysis_*.md),不点名平台——按别名兜住。
_COMMUNITY_KEYWORDS = ("小红书", "知乎", "微博", "B站", "bilibili", "reddit", "推特", "twitter",
                       "xhs", "social-search")


def _heading_sources(content: str) -> list[str]:
    """Source lines from `## 来源` style sections (bullets stripped)."""
    out: list[str] = []
    for m in _SRC_HEADING.finditer(content):
        rest = content[m.end():]
        nxt = _NEXT_HEADING.search(rest)
        block = rest[: nxt.start()] if nxt else rest
        for line in block.splitlines():
            line = line.strip().lstrip("-*").strip()
            if line:
                out.append(line)
    return out


def has_recognizable_source(content: str) -> bool:
    """Format gate: does the capsule carry any recognizable 来源 metadata?
    Used by /api/ingest to quarantine non-conforming OpenClaw capsules
    instead of letting them into the graph as full-weight firsthand."""
    text = content or ""
    return bool(_SRC_LINE.search(text)) or bool(_heading_sources(text))


def classify_provenance(content: str) -> str:
    """Classify a capsule body → provenance tag. Never raises."""
    text = content or ""
    sources = [m.group(1) for m in _SRC_LINE.finditer(text)]
    sources += _heading_sources(text)
    if not sources:
        return PROV_FIRSTHAND
    if any(_ARTICLE.search(s) for s in sources):
        return PROV_ARTICLE
    low = [s.lower() for s in sources]
    if any(k.lower() in s for k in _COMMUNITY_KEYWORDS for s in low):
        return PROV_COMMUNITY
    return PROV_FIRSTHAND
