"""抽取结果的「结构」指标与结构过薄提醒：战役复盘表（tools/extract_campaign）与自检（战役 lint、bench lint）共用。

为什么要有：2026-10-25 起的一场 codex 8000 条战役，事实文字准确，但 78% 的观测恰好 2 个实体、
每条只有 1.7–1.8 个不同端点对、第 3 条边起 92–94% 是 relates_to、跨观测复用的实体出现只占 36–44%
（Claude 战役 75–85%）——正文里点名的文件、提交、配置键没有成节点，图里几乎没有结构。
v5 质量门与 lint 只查对错与格式，12 版补充约定都没看出来，只能靠人工统计。这里把它变成复盘表的列和自检提醒。

只用标准库：tools/build_eval_adapter.py 把本文件原样复制进评测适配包闭包，bench lint 用同一份。
口径见 docs/executors/campaign.md「复盘表的结构指标」。
"""
from __future__ import annotations

import re

HEADLINE_EDGES = 2   # 前两条边是照抄标题与译文（规则 21/22），结构从第 3 条边起看


def name_key(name) -> str:
    """实体名的比较键：空白归一、忽略大小写。"""
    return " ".join(str(name or "").split()).casefold()


def entity_keys(item: dict | None) -> set[str]:
    """一条结果里不同的实体名（比较键）。"""
    return {k for k in (name_key(e.get("name")) for e in (item or {}).get("entities") or []) if k}


def structure_metrics(item: dict | None) -> dict:
    """一条结果的结构计数；没有实体的（空 / 没交）返回 nonempty=False，不进分母。"""
    keys = entity_keys(item)
    if not keys:
        return {"nonempty": False}
    edges = [g for g in (item.get("edges") or []) if isinstance(g, dict)]
    pairs = {(name_key(g.get("source")), name_key(g.get("target"))) for g in edges
             if name_key(g.get("source")) and name_key(g.get("target"))}
    rest = edges[HEADLINE_EDGES:]
    return {"nonempty": True, "entities": len(keys), "two_entity": len(keys) == 2, "endpoint_pairs": len(pairs),
            "rest_edges": len(rest),
            "rest_relates_to": sum((g.get("name") or "").strip().lower() == "relates_to" for g in rest)}


def structure_aggregate(ms: list[dict]) -> dict:
    """structure_metrics 的列表 → 计数与比例。比例的分母：恰 2 实体、端点对/条按非空条目，relates_to 按第 3 条边起的边数。
    计数（_struct_*）留给按条数加权合并用。"""
    ne = [m for m in ms if m.get("nonempty")]
    n, rest = len(ne), sum(m["rest_edges"] for m in ne)
    two, pairs, rel = (sum(m["two_entity"] for m in ne), sum(m["endpoint_pairs"] for m in ne),
                       sum(m["rest_relates_to"] for m in ne))
    return {"_struct_n": n, "_struct_two": two, "_struct_pairs": pairs, "_struct_rest": rest, "_struct_rel": rel,
            **structure_pcts(n, two, pairs, rest, rel)}


def structure_pcts(n: int, two: int, pairs: int, rest: int, rel: int) -> dict:
    """分母为 0 时给 None（表里显示「—」）。"""
    return {"two_entity_pct": round(100 * two / n, 1) if n else None,
            "pairs_per_obs": round(pairs / n, 2) if n else None,
            "rest_relates_to_pct": round(100 * rel / rest, 1) if rest else None}


def merge_structure(rows: list[dict]) -> dict:
    """若干行（structure_aggregate 的输出）合并：计数相加后重算比例，等价于按非空条数 / 边数加权。"""
    keys = ("_struct_n", "_struct_two", "_struct_pairs", "_struct_rest", "_struct_rel")
    tot = {k: sum(r.get(k) or 0 for r in rows) for k in keys}
    return structure_pcts(*(tot[k] for k in keys))


def reuse_counts(keysets: list[set[str]], obs_counts: dict[str, int]) -> tuple[int, int]:
    """跨观测复用：keysets 是被统计的那些观测各自的实体名集合，obs_counts 是参照池里每个名字出现在几条观测里。
    返回 (复用的实体出现次数, 实体出现总次数)；复用 = 该名字在参照池里出现在 ≥2 条观测。"""
    total = sum(len(s) for s in keysets)
    reused = sum(1 for s in keysets for k in s if obs_counts.get(k, 0) >= 2)
    return reused, total


def count_obs(keysets: list[set[str]], into: dict[str, int] | None = None) -> dict[str, int]:
    """把若干观测的实体名集合累加进「名字 → 出现在几条观测」。"""
    out = into if into is not None else {}
    for s in keysets:
        for k in s:
            out[k] = out.get(k, 0) + 1
    return out


def reuse_pct(reused: int, total: int) -> float | None:
    return round(100 * reused / total, 1) if total else None


# ---------- 结构过薄提醒（lint，只提醒、不阻断） ----------
# 保守识别正文点名的具体对象：宁可漏报，不让普通词句触发。只认三种——
#   路径 / 文件名：带斜杠且以扩展名结尾，或常见代码 / 配置扩展名的裸文件名；
#   提交 SHA：7–40 位小写十六进制，且同时含数字和 a–f 字母（排除纯数字 id 与 deadbeef 这类单词；UUID 的分段不算）；
#   反引号里的标识符：`...`，2–80 个非空白字符（带空格的代码片段不算）。
# 「Node.js」这类框架名长得像文件名，单列排除。
_EXT = (r"py|pyi|md|json|jsonl|ya?ml|toml|ini|cfg|conf|env|sh|zsh|bash|sql|js|mjs|cjs|ts|tsx|jsx|go|rs|java|kt|"
        r"swift|rb|php|c|h|cc|cpp|hpp|cs|html|css|scss|txt|lock|plist|xml|gradle|proto|ipynb")
_PATH_RE = re.compile(r"(?<![\w/.~-])(?:~?/)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z]\w{0,7}(?![\w/])")
_FILE_RE = re.compile(r"(?<![\w/.-])[\w-]+(?:\.[\w-]+)*\.(?:" + _EXT + r")(?![\w/])", re.IGNORECASE)
_SHA_RE = re.compile(r"(?<![0-9A-Za-z-])(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![0-9A-Za-z-])")
_TICK_RE = re.compile(r"`([^`\s]{2,80})`")
_NOT_FILES = {"node.js", "next.js", "nuxt.js", "vue.js", "react.js", "express.js", "three.js", "d3.js", "chart.js"}
THIN_MIN_OBJECTS = 2   # 正文点名至少这么多个不同对象才提醒


def named_objects(text: str) -> list[str]:
    """正文里点名的具体对象（去重、保持首次出现顺序）。"""
    found: list[tuple[int, str]] = []
    for rx, grp in ((_TICK_RE, 1), (_PATH_RE, 0), (_FILE_RE, 0), (_SHA_RE, 0)):
        found += [(m.start(), m.group(grp).strip()) for m in rx.finditer(text or "")]
    seen, out = set(), []
    for _, s in sorted(found):
        k = name_key(s)
        # 反引号里的路径与裸路径是同一个对象；路径与它末尾的文件名（a/b.py 与 b.py）也不重复算
        if not s or k in _NOT_FILES or k in seen or any(x.endswith("/" + k) or k.endswith("/" + x) for x in seen):
            continue
        seen.add(k)
        out.append(s)
    return out


def thin_structure(body: str, item: dict | None) -> dict | None:
    """正文点名 ≥2 个具体对象，而结果只有 ≤1 个端点对或 ≤2 个实体时，返回提醒要的信息；否则 None。
    空结果（没有实体）不提醒：判空是另一件事。"""
    m = structure_metrics(item)
    if not m["nonempty"] or (m["endpoint_pairs"] > 1 and m["entities"] > 2):
        return None
    objs = named_objects(body)
    if len(objs) < THIN_MIN_OBJECTS:
        return None
    return {"objects": objs, "entities": m["entities"], "endpoint_pairs": m["endpoint_pairs"]}


def thin_structure_warnings(rows: list[tuple[int, str, str]], results: dict[int, dict], bodies: dict[int, str],
                            *, samples: int = 3) -> list[str]:
    """rows: [(观测 id, 标题, 批次)]，与 lint_items 同形；bodies: {观测 id: 正文}。返回提醒行（不计入自检问题数）。"""
    out = []
    for i, _title, part in rows:
        hit = thin_structure(bodies.get(i, ""), results.get(i))
        if hit:
            objs = hit["objects"]
            more = f" 等 {len(objs)} 个" if len(objs) > samples else ""
            out.append(f"{part} {i}: 结构过薄（提醒，不阻断）：正文点名 {len(objs)} 个具体对象（"
                       + "、".join(objs[:samples]) + more + f"），结果只有 {hit['entities']} 个实体、"
                       f"{hit['endpoint_pairs']} 个端点对；点名的文件 / 提交 / 配置键能成节点的拆成实体与边")
    return out
