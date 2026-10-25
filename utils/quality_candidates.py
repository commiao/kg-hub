"""质量表引文候选：给审阅者预填「最像出处」的原句，省掉逐条翻原文找引文的工具调用。

只做确定性的字符串 / 句子匹配，不调模型。约束（tests/test_campaign_prepare.py 守着）：
- 候选必须是冻结输入（本条正文、lookahead、同批其他条目正文）里真实存在的子串，写进表之前用
  utils.extraction_quality 的同一套取证与 _quote_ok 再核一遍，核不过就不填；
- 只填 quote / source / source_id，并标 candidate；verdict 与四个核对标志、判空理由一律不碰；
- 候选不改变 validate_review 的任何判定：审阅者逐条确认后删掉 candidate 键，campaign prepare 见到还留着的
  candidate 就不往 quality-check / dry-run 走（这是 prepare 自己的停点，不是提交门）。
"""
from __future__ import annotations

import re

from utils import extraction_quality as quality

PREVIOUS = "previous_review"   # 从上一版质量表沿用的引文（审阅者当时确认过，内容又改了，仍要重新确认）
MATCH = True                   # 按字词重合找到的原句

_STOP = frozenset("""the and for with that this from are was were has have had not but into when then than its
which will been also only after before because while where what who how per via can could should would may might
does did done use used uses using one two all any each other same such more most less same there their they them
these those over under between about against""".split())
_CUT = "。！？；"


def _tokens(text: str) -> dict[str, int]:
    """词元 → 权重：含数字或 _ . / : # - 的标识符 3，其余英文词 1，中文按二字组 1。"""
    low = text.lower()
    out: dict[str, int] = {}
    for w in re.findall(r"[a-z0-9_][a-z0-9_.:/#-]*[a-z0-9_]|[a-z0-9]", low):
        if w in _STOP or (len(w) < 3 and not w.isdigit()):
            continue
        out[w] = 3 if re.search(r"[\d_./:#-]", w) else 1
    for run in re.findall(r"[一-鿿]+", low):
        for gram in ({run[i:i + 2] for i in range(len(run) - 1)} or {run}):
            out.setdefault(gram, 1)
    return out


def segments(text: str) -> list[str]:
    """原文切成句子与整行（都是 text 的子串，去掉首尾空白）。"""
    out: list[str] = []
    for line in text.split("\n"):
        start = 0
        for i, ch in enumerate(line):
            nxt = line[i + 1] if i + 1 < len(line) else " "
            if ch in _CUT or (ch in ".!?;" and nxt.isspace()):
                out.append(line[start:i + 1])
                start = i + 1
        out.append(line[start:])
        out.append(line)
    seen, keep = set(), []
    for s in (x.strip() for x in out):
        if s and s not in seen:
            seen.add(s)
            keep.append(s)
    return keep


def best_segment(claim: str, text: str, *, must_contain: str | None = None) -> tuple[str, float] | None:
    """text 里与 claim 字词重合最多的句子：重合权重 ≥ 2 且覆盖 claim 词元权重的 25% 以上才算。"""
    want = _tokens(claim)
    total = sum(want.values())
    if not total:
        return None
    best = None
    for seg in segments(text):
        if must_contain and must_contain.lower() not in seg.lower():
            continue
        got = _tokens(seg)
        shared = sum(w for t, w in want.items() if t in got)
        score = shared / total
        if shared < 2 or score < 0.25:
            continue
        rank = (score, shared, -len(seg))
        if best is None or rank > best[0]:
            best = (rank, seg)
    return (best[1], best[0][0]) if best else None


def _places(item: dict, batch: dict) -> list[tuple[dict, str]]:
    """候选出处，按优先级：本条正文 → lookahead → 同批其他条目正文。"""
    out = [({"source": "body"}, quality.body_evidence(item)),
           ({"source": "lookahead"}, quality.lookahead_evidence(batch))]
    for other in batch.get("items", []):
        if type(other.get("id")) is int and other["id"] != item["id"]:
            out.append(({"source": "batch_item", "source_id": other["id"]}, quality.body_evidence(other)))
    return out


def _verified(ev: dict, item: dict, sources: dict[str, dict], future: str) -> bool:
    return quality._quote_ok(ev.get("quote"), quality.evidence_text(ev, item, sources, future))


def _edge_candidate(n: int, edge: dict, item: dict, batch: dict, title: str) -> dict | None:
    body = quality.body_evidence(item)
    if n < 2 and title and title in body:
        # 第 1 条边照抄标题、第 2 条是标题译文：出处都是标题这一行
        return {"source": "body", "quote": title}
    if edge.get("name") == "belongs_to":
        # 归属只认本条正文、且引文要含目标名（validate_review 同一条件）
        hit = best_segment(edge.get("fact", "") + " " + edge.get("target", ""), body, must_contain=edge.get("target") or None)
        return {"source": "body", "quote": hit[0]} if hit else None
    for where, text in _places(item, batch):
        hit = best_segment(edge.get("fact", ""), text)
        if hit:
            return {**where, "quote": hit[0]}
    return None


def _summary_candidate(entity: dict, item: dict, batch: dict) -> dict | None:
    claim = f"{entity.get('name', '')} {entity.get('summary', '')}"
    for where, text in _places(item, batch):
        hit = best_segment(claim, text)
        if hit:
            # 本条正文与 lookahead 不写 source（validate_review 对无 source 的 summary 查 正文+lookahead）
            return ({"quote": hit[0]} if where["source"] != "batch_item" else {**where, "quote": hit[0]})
    return None


def add_candidates(sheet: dict, batch: dict, results: dict, previous: dict | None = None) -> dict:
    """就地给质量表模板填引文候选，返回计数。previous 是同一批上一版质量表（结果改过后重新生成时）：
    同一条、边数没变的，沿用审阅者当时填的引文（仍核实是子串），标 candidate=previous_review。"""
    from utils.single_pass_extract import observation_title
    sources = {str(i["id"]): i for i in batch.get("items", []) if isinstance(i, dict) and "id" in i}
    future = quality.lookahead_evidence(batch)
    by_result = {str(r["id"]): r for r in quality.result_items(results)}
    old_rows = {str(r.get("id")): r for r in (previous or {}).get("items", []) if isinstance(r, dict)}
    stats = {"edges": 0, "edge_candidates": 0, "summaries": 0, "summary_candidates": 0,
             "empty_candidates": 0, "from_previous": 0}
    for row in sheet.get("items", []):
        oid = str(row["id"])
        item, result = sources.get(oid), by_result.get(oid)
        if item is None or result is None:
            continue
        title = observation_title(item.get("episode_body", ""))
        old = old_rows.get(oid) or {}
        old_edges = old.get("edges") if isinstance(old.get("edges"), list) else []
        reuse_edges = len(old_edges) == len(row["edges"])
        old_summaries = {s.get("name"): s for s in old.get("summaries") or [] if isinstance(s, dict)}
        for ev, edge in zip(row["edges"], result.get("edges", [])):
            stats["edges"] += 1
            prev = old_edges[ev["index"]] if reuse_edges and isinstance(old_edges[ev["index"]], dict) else None
            cand, mark = None, MATCH
            if prev and prev.get("quote") and not prev.get("candidate"):
                cand = {k: prev[k] for k in ("source", "source_id", "quote") if k in prev}
                mark = PREVIOUS
                if not _verified(cand, item, sources, future):
                    cand, mark = None, MATCH
            if cand is None:
                cand = _edge_candidate(ev["index"], edge, item, batch, title)
            if cand and _verified(cand, item, sources, future):
                ev.pop("source_id", None)
                ev.update(cand, candidate=mark)
                stats["edge_candidates"] += 1
                stats["from_previous"] += mark == PREVIOUS
        entities = {e["name"]: e for e in result.get("entities", []) if isinstance(e, dict)}
        for ev in row["summaries"]:
            stats["summaries"] += 1
            prev = old_summaries.get(ev["name"])
            cand, mark = None, MATCH
            if prev and prev.get("quote") and not prev.get("candidate"):
                cand = {k: prev[k] for k in ("source", "source_id", "quote") if k in prev}
                mark = PREVIOUS
                text_ok = (_verified(cand, item, sources, future) if "source" in cand
                           else quality._quote_ok(cand["quote"], quality.body_evidence(item) + "\n" + future))
                if not text_ok:
                    cand, mark = None, MATCH
            if cand is None and ev["name"] in entities:
                cand = _summary_candidate(entities[ev["name"]], item, batch)
            if cand:
                text_ok = (_verified(cand, item, sources, future) if "source" in cand
                           else quality._quote_ok(cand["quote"], quality.body_evidence(item) + "\n" + future))
                if text_ok:
                    ev.update(cand, candidate=mark)
                    stats["summary_candidates"] += 1
                    stats["from_previous"] += mark == PREVIOUS
        empty = row.get("empty")
        body = quality.body_evidence(item)
        if isinstance(empty, dict) and title and quality._quote_ok(title, body):
            # 判空只预填出处（标题行）；理由与 no_new_information 由审阅者判断
            empty.update(quote=title, candidate=MATCH)
            stats["empty_candidates"] += 1
    return stats


def sheet_status(sheet: dict) -> dict:
    """质量表还差什么：审阅人、没下结论的行、判了 error 的行、没确认的候选、空引文。"""
    pending, errors, unconfirmed, missing = [], [], 0, 0
    for row in sheet.get("items", []):
        if not isinstance(row, dict):
            continue
        verdict = row.get("verdict")
        flags = all(row.get(f) is True for f in ("translation_complete", "facts_complete", "core_covered",
                                                    "ownership_checked"))
        if verdict == "error":
            errors.append(row.get("id"))
        elif verdict != "ok" or not flags:
            pending.append(row.get("id"))
        evs = [e for k in ("edges", "summaries") for e in row.get(k) or [] if isinstance(e, dict)]
        if isinstance(row.get("empty"), dict):
            evs.append(row["empty"])
        unconfirmed += sum(1 for e in evs if "candidate" in e)
        missing += sum(1 for e in evs if not str(e.get("quote") or "").strip())
    reviewer = isinstance(sheet.get("reviewer"), str) and bool(sheet["reviewer"].strip())
    return {"rows": len(sheet.get("items", [])), "reviewer": reviewer, "pending_rows": pending,
            "error_rows": errors, "unconfirmed_candidates": unconfirmed, "missing_quotes": missing,
            "complete": reviewer and not pending and not errors and not unconfirmed}
