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

# `- **来源**: 公众号文章《…》` — capsule metadata line (fullwidth or ASCII colon)
_SRC_LINE = re.compile(r"\*\*来源\*\*[::]\s*(.+)")

# 文章《…》 covers 公众号文章《, 掘金文章《, 博客文章《 etc. The 《 is the key
# discriminator: "用户查询公众号近5篇文章数据" (ops log) has 文章 but no 《.
_ARTICLE = re.compile(r"文章《")

_COMMUNITY_KEYWORDS = ("小红书", "知乎", "微博", "B站", "bilibili", "reddit", "推特", "twitter")


def classify_provenance(content: str) -> str:
    """Classify a capsule body → provenance tag. Never raises."""
    sources = [m.group(1) for m in _SRC_LINE.finditer(content or "")]
    if not sources:
        return PROV_FIRSTHAND
    if any(_ARTICLE.search(s) for s in sources):
        return PROV_ARTICLE
    if any(any(k in s for k in _COMMUNITY_KEYWORDS) for s in sources):
        return PROV_COMMUNITY
    return PROV_FIRSTHAND
