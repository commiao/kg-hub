"""utils/predigest.py — 长文档预拆 + catalog 检测(fact 层的 owner)。

REFINERY-DESIGN §3'(2026-07-28 评审采纳):fact 质量由**输入粒度**决定——
claude-mem obs(原子、带显式 facts)经 graphiti 抽出好 fact;数千字中文
markdown 整篇进抽取,洞察抽成 0 条 fact,只剩文献元数据;目录/注册表类
量产 trivial fact 扫席召回(实测《断崖衰减》洞察 fact=0,全图文献元数据
fact 611 条)。

本模块只做纯函数(路由判定 / prompt / 解析 / 拼装),LLM 调用留在宿主
(kg_hub_server._llm_complete 或独立回填工具),与 utils/origin.py 的
KIND_PROMPT 分工惯例一致。

三类路由:
  None      — 短文档/claude-mem obs 等,照旧整篇进 graphiti(粒度本来就对)
  "catalog" — 目录/注册表/清单/审计类:仅存 Episodic 节点(全文可搜),
              kind=registry,**跳过抽取**(不产 fact)
  "split"   — 长知识文档:父节点仅存(全文可搜,不抽取)+ LLM 预拆成
              ≤MAX_OBS 条原子 observation,逐条进 graphiti 产好 fact
"""

from __future__ import annotations

import json
import re

# 阈值按 UTF-8 **字节**——与 vps_push 的 MIN_SIZE=1500 字节口径对齐。中文 1500
# 字节≈500 字符,按字符卡 1500 会漏掉大量中文胶囊(《断崖衰减》873 字符≈2.5KB,
# 部署实测被字符口径误放行)。凡是过了推送门的胶囊,预拆都该接得住。
PREDIGEST_MIN_BYTES = 1500
MAX_OBS = 8                    # 单文档最多拆出的 observation 数(LLM 预算上限)
PREDIGEST_PREFIXES = ("openclaw-capsule-", "openclaw-kb-")

# 审查校准(2026-07-28,191 篇真实文档实测):registry/目录/索引/清单已覆盖全部
# 真 catalog;"审计|audit" 命中的是诊断报告(结论型分析,真知识),贡献 3 篇误杀
# 零真阳性 → 从名字门移除。inventory 保留(能力清单实测偏 catalog)。
_CATALOG_NAME = re.compile(
    r"registry|catalog|inventory|注册表|清单|索引|目录", re.IGNORECASE)
_CONCLUSION_HINT = re.compile(r"一句话|结论|洞察|关键认知|教训|原则|规律|为什么")


def is_catalog(name: str, body: str) -> bool:
    """目录/注册表类:是"目录"不是"知识",不该进语义索引。

    判定(保守,精确优先):name/首标题命中目录词;或正文以枚举行
    (表格/列表)为主且没有任何结论性段落。"""
    if _CATALOG_NAME.search(name or ""):
        return True
    lines = (body or "").splitlines()
    if not lines:
        return False
    first_heading = next((ln for ln in lines if ln.lstrip().startswith("#")), "")
    if _CATALOG_NAME.search(first_heading):
        return True
    if len(lines) >= 20:
        enum = sum(1 for ln in lines
                   if ln.lstrip().startswith("|") or re.match(r"\s*[-*•]\s", ln))
        if enum / len(lines) > 0.6 and not _CONCLUSION_HINT.search(body):
            return True
    return False


def predigest_route(name: str, body: str) -> str | None:
    """返回 None / 'catalog' / 'split'。只对 OpenClaw 长文档线生效——
    claude-mem obs 与 canonical 文档粒度已合适,不碰。"""
    if not (name or "").startswith(PREDIGEST_PREFIXES):
        return None
    if is_catalog(name, body):
        return "catalog"
    if len((body or "").encode("utf-8", "ignore")) >= PREDIGEST_MIN_BYTES:
        return "split"
    return None


PREDIGEST_PROMPT = """把下面的长文档拆成若干条**原子知识观察**,每条自包含、只讲一个主题。

只保留有复用价值的知识:洞察、结论、方法、数据规律、决策及理由、教训。
跳过:目录、流水账、寒暄、纯元数据(提取日期/价值等级等)。没有可拆的返回 []。

每条观察输出这些字段:
- "type": "discovery"|"decision"|"feature"|"bugfix"|"change"|"refactor"|"security_note" 之一
- "title": ≤30字中文标题(讲结论,不是讲主题)
- "facts": 2-6 条独立事实句。**每句必须脱离上下文也成立**:主语用具体名词
  (不用"它/该系统"),包含具体数字/名称/条件。这是最重要的字段。
- "narrative": 80-300字叙述(背景+结论+为什么)
- "concepts": 2-5 个概念标签

严格输出 JSON 数组,不要 markdown 代码块,不要任何解释文字。最多 {max_obs} 条。

文档:
{body}"""


_OBS_TYPES = {"discovery", "decision", "feature", "bugfix", "change",
              "refactor", "security_note"}


def _first_json_array(text: str) -> list | None:
    """从每个 '[' 起点用 raw_decode 试解析,取第一个成功的 list。
    (贪婪正则在"数组后跟含 ] 的尾注 / 数组前散文含 [" 两种真实 LLM 输出上会整篇丢弃)"""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "[":
            continue
        try:
            v, _ = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(v, list):
            return v
    return None


def parse_observations(llm_text: str) -> list[dict]:
    """容错解析 LLM 输出 → observation 列表。解析不出返回 [](调用方回退整篇路径)。"""
    t = (llm_text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)  # 剥代码围栏
    arr = _first_json_array(t)
    if arr is None:
        return []
    out: list[dict] = []
    for o in arr:
        if not isinstance(o, dict):
            continue
        raw_facts = o.get("facts")
        if not isinstance(raw_facts, list):   # qwen 偶发返回字符串,逐字符迭代会炸成垃圾 fact
            raw_facts = []
        facts = [str(f).strip() for f in raw_facts if str(f).strip()]
        title = str(o.get("title") or "").strip()
        if not title or not facts:
            continue  # 没标题或没 facts 的片段没有抽取价值
        typ = str(o.get("type") or "").strip()
        raw_concepts = o.get("concepts")
        if not isinstance(raw_concepts, list):
            raw_concepts = []
        out.append({
            "type": typ if typ in _OBS_TYPES else "discovery",
            "title": title[:60],
            "facts": facts[:6],
            "narrative": str(o.get("narrative") or "").strip()[:600],
            "concepts": [str(c).strip() for c in raw_concepts][:5],
        })
    return out[:MAX_OBS]


def obs_to_episode_body(obs: dict, parent_name: str) -> str:
    """observation → 子 episode 正文(结构对齐 claude-mem obs:显式 facts 在前,
    graphiti 抽取输入粒度与之一致)。"""
    L = [f"[{obs['type']}] {obs['title']}", "", "Facts:"]
    L += [f"- {f}" for f in obs["facts"]]
    if obs.get("narrative"):
        L += ["", f"Narrative: {obs['narrative']}"]
    if obs.get("concepts"):
        L += ["", "Concepts: " + ", ".join(obs["concepts"])]
    L += ["", f"来源文档: {parent_name}"]
    return "\n".join(L)
