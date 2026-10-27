"""战役结构门槛：整批「结构过薄」超阈值时拒绝提交（campaign lint / prepare / 正式 submit 共用）。

为什么有这一层：#264 把结构指标做进复盘表、lint 只出「结构过薄」提醒，不拦提交。2026-10-25 的 codex 8000 条战役
登记结构约定 c13 后一度改善，扩到每模型 4 组并发后 gpt-6-sol 回潮（066 批恰 2 实体 40%、071 批 56%、
075 批 100% / 端点对 1.12，完全退回两节点模板），提醒照出、批次照样入库。这里把同一组指标变成可启用的提交门槛。

只增加拒绝条件：lint、v5 质量表、dry-run、命名分裂等原有判定一条不改。门槛按战役显式启用（campaign.json 的
structure_gate，经 `campaign structure-gate` 或 `campaign start` 写入，启用 / 改阈值 / 关闭都记进 structure_gate_log），
只管 chain 版本 ≥ from_chain 的批次：没启用的战役、启用前的 chain 下领的批次行为不变。

口径（utils/extraction_structure 同一份）：分母是非空条目；端点对是不同的有向 (source, target)；relates_to 从第 3 条边起算。

例外（c13 规则 3）：正文确实只讲一个对象、或证据只支持两个实体的条目，审阅者在质量表该行写 structure_note 说明，
可移出分母、单独列出。structure_note 要满足：点名本条至少一个实体名；去掉本条实体名与数字后仍有 ≥10 字的说明；
同批不能有两条去掉实体名与数字后相同（模板句，呼应 c14 禁止整批统一置为通过）——相同的全部不算。
例外条数超过 max_exempt_pct（至少允许 1 条）整批拒绝：例外不能替代结构。
"""
from __future__ import annotations

import re

from utils import extraction_structure as xs

GATE_VERSION = "structure-gate-v1"
# 阈值建议（PR 描述有回放）：c13 批次观察目标是「恰 2 实体 < 30%、端点对 ≥ 3」，门槛取恰 2 实体同线、端点对放宽到 2.5，
# 只拦明显退回两节点模板的批次。relates_to 区分度有限（Claude 战役中位数也在 80%），缺省只提醒。
DEFAULTS = {"max_two_entity_pct": 30.0, "min_pairs_per_obs": 2.5, "max_relates_to_pct": None, "max_exempt_pct": 20.0}
THRESHOLDS = tuple(DEFAULTS)
RELATES_TO_ADVISORY = 90.0   # 没设 max_relates_to_pct 时，超过这个值只出提醒
NOTE_FIELD = "structure_note"
NOTE_MIN_CHARS = 10


def chain_no(v: str) -> int:
    if not isinstance(v, str) or not re.fullmatch(r"c\d+", v):
        raise SystemExit(f"chain 版本要写成 c<数字>：{v!r}")
    return int(v[1:])


def validate_config(cfg: dict) -> dict:
    """阈值合法化：比例在 0–100，端点对 ≥ 0；None 表示不查这一项，但三项不能全是 None。"""
    out = {}
    for k in THRESHOLDS:
        v = cfg.get(k, DEFAULTS[k])
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise SystemExit(f"{k} 要是数字：{v!r}")
            v = float(v)
            if k.endswith("_pct") and not 0 <= v <= 100 or v < 0:
                raise SystemExit(f"{k} 超出范围：{v}")
        out[k] = v
    if all(out[k] is None for k in ("max_two_entity_pct", "min_pairs_per_obs", "max_relates_to_pct")):
        raise SystemExit("结构门槛至少要查一项（恰 2 实体 / 端点对 / relates_to）")
    if out["max_exempt_pct"] is None:
        raise SystemExit("max_exempt_pct 不能为空：例外要有上限")
    return out


def config_for(c: dict, b: dict) -> dict | None:
    """这一批适用的门槛配置；战役没启用、或批次的 chain 早于 from_chain 时 None（行为与启用前相同）。"""
    g = c.get("structure_gate") or {}
    if not g.get("enabled"):
        return None
    if chain_no(b.get("chain") or "c0") < chain_no(g["from_chain"]):
        return None
    return g


def _is_thin(m: dict, cfg: dict) -> bool:
    floor = cfg.get("min_pairs_per_obs")
    return m["two_entity"] or (floor is not None and m["endpoint_pairs"] < floor)


def _note_core(note: str, names: list[str]) -> str:
    """去掉本条实体名、数字、空白与标点后的说明正文：用来判模板句与说明长度。"""
    s = note.casefold()
    for n in sorted((xs.name_key(x) for x in names), key=len, reverse=True):
        if n:
            s = s.replace(n, "")
    return re.sub(r"[\W\d_]+", "", s)


def exemptions(results: dict, review: dict | None, cfg: dict) -> tuple[dict[int, str], list[dict]]:
    """返回 (有效例外 {id: note}, 不算数的例外 [{id, why}])。只看确实偏薄的非空条目上的 structure_note。"""
    rows = {}
    for r in (review or {}).get("items") or []:
        if isinstance(r, dict) and type(r.get("id")) is int and isinstance(r.get(NOTE_FIELD), str) \
                and r[NOTE_FIELD].strip():
            rows[r["id"]] = r[NOTE_FIELD].strip()
    items = {int(it["id"]): it for it in results.get("items", [])}
    cand, bad = {}, []
    for i, note in rows.items():
        it = items.get(i)
        m = xs.structure_metrics(it)
        if not m["nonempty"] or not _is_thin(m, cfg):
            continue                      # 结构达标或空结果：note 无需也不计
        names = [e.get("name") for e in it.get("entities") or []]
        core = _note_core(note, names)
        if not any(xs.name_key(n) and xs.name_key(n) in note.casefold() for n in names):
            bad.append({"id": i, "why": "structure_note 没点名本条任何实体"})
        elif len(core) < NOTE_MIN_CHARS:
            bad.append({"id": i, "why": f"structure_note 去掉实体名与数字后不足 {NOTE_MIN_CHARS} 字"})
        else:
            cand[i] = (note, core)
    seen: dict[str, list[int]] = {}
    for i, (_, core) in cand.items():
        seen.setdefault(core, []).append(i)
    ok = {}
    for i, (note, core) in cand.items():
        if len(seen[core]) > 1:
            bad.append({"id": i, "why": f"structure_note 与同批 {len(seen[core]) - 1} 条是同一模板句"})
        else:
            ok[i] = note
    return ok, sorted(bad, key=lambda x: x["id"])


def evaluate(cfg: dict, results: dict, review: dict | None = None) -> dict:
    """整批评估。ok=False 时 violations 是拒绝理由，thin 是逐条清单（不含有效例外）。"""
    items = results.get("items", [])
    ms = {int(it["id"]): xs.structure_metrics(it) for it in items}
    nonempty = [i for i, m in ms.items() if m["nonempty"]]
    exempt, bad = exemptions(results, review, cfg)
    counted = [i for i in nonempty if i not in exempt]
    agg = xs.structure_aggregate([ms[i] for i in counted])
    metrics = {k: agg[k] for k in ("two_entity_pct", "pairs_per_obs", "rest_relates_to_pct")}
    violations, warnings = [], []
    two, pairs, rel = metrics["two_entity_pct"], metrics["pairs_per_obs"], metrics["rest_relates_to_pct"]
    if cfg.get("max_two_entity_pct") is not None and two is not None and two > cfg["max_two_entity_pct"]:
        violations.append(f"恰 2 实体占 {two}%（上限 {cfg['max_two_entity_pct']:g}%）")
    if cfg.get("min_pairs_per_obs") is not None and pairs is not None and pairs < cfg["min_pairs_per_obs"]:
        violations.append(f"平均不同端点对 {pairs}（下限 {cfg['min_pairs_per_obs']:g}）")
    if rel is not None:
        if cfg.get("max_relates_to_pct") is not None:
            if rel > cfg["max_relates_to_pct"]:
                violations.append(f"第 3 条边起 relates_to 占 {rel}%（上限 {cfg['max_relates_to_pct']:g}%）")
        elif rel > RELATES_TO_ADVISORY:
            warnings.append(f"第 3 条边起 relates_to 占 {rel}%（提醒，不阻断）：能判定具体关系的换成具体关系")
    cap = max(1, int(len(nonempty) * cfg["max_exempt_pct"] / 100))
    if len(exempt) > cap:
        violations.append(f"structure_note 例外 {len(exempt)} 条，超过上限 {cap} 条（非空 {len(nonempty)} 条的 "
                          f"{cfg['max_exempt_pct']:g}%）：例外不能替代结构")
    thin = [{"id": i, "entities": ms[i]["entities"], "endpoint_pairs": ms[i]["endpoint_pairs"]}
            for i in counted if _is_thin(ms[i], cfg)]
    return {"version": GATE_VERSION, "ok": not violations, "nonempty": len(nonempty), "counted": len(counted),
            "metrics": metrics, "violations": violations, "warnings": warnings, "thin": thin,
            "exempt": [{"id": i, "note": n} for i, n in sorted(exempt.items())], "invalid_exempt": bad,
            "config": {k: cfg.get(k) for k in THRESHOLDS} | {"from_chain": cfg.get("from_chain")}}


def refusal_lines(bid: str, ev: dict, *, limit: int = 30) -> list[str]:
    """拒绝时给子代理看的逐条清单与修法。"""
    out = [f"{bid} 结构门槛没过（{GATE_VERSION}，chain ≥ {ev['config']['from_chain']} 的批次适用）："
           + "；".join(ev["violations"])]
    for t in ev["thin"][:limit]:
        out.append(f"  {bid} {t['id']}: {t['entities']} 个实体、{t['endpoint_pairs']} 个不同端点对")
    if len(ev["thin"]) > limit:
        out.append(f"  …… 另有 {len(ev['thin']) - limit} 条")
    for x in ev["invalid_exempt"]:
        out.append(f"  {bid} {x['id']}: 例外不算数：{x['why']}")
    out.append("修法：按 c13 把正文点名、承载事实的具体对象（文件、函数、配置键、命令、问题 / 修复）建成实体，"
               "事实挂到具体对象之间、用具体关系；原文确实只支持两个对象的，在质量表该行写本条特有的 "
               f"{NOTE_FIELD}（点名实体、说明证据限制，不用同一句模板）。不要为凑数编实体或拆同一对象。")
    return out
