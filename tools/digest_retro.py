"""tools/digest_retro.py — 消化执行的学习闭环：每 N 条（默认 500）复盘一次。

用户要求（2026-10-17）：消化 live 积压时，每执行 500 条任务就要自行总结入库过程的经验
教训，并把教训运用到下一轮；最终对比时还要给出实践过的优化意见。

本工具把这个要求做成可执行、可核验的机制，而不是靠执行者记得：

  gate        账本里自上次复盘以来 succeeded 已达阈值 → 退出码 2，执行者必须先复盘再派发
  checkpoint  关一轮：取窗口指标（账本 + 图库）、按规则给出「观察→建议→旋钮」、
              记录执行者写的经验教训与下一轮调参（from→to），落盘 round 报告
  status      当前轮次 / 距下次复盘还差多少 / 当前生效调参
  final       全程对比：各轮指标表 + 实践过的优化（旋钮、前后指标差）+ 未实践的建议

指标与 docs/gateway-model-quality-20261017.md 第 3 节同一口径：边名合规率（schema 15 个
名，大小写不敏感）、不同边名数、重复三元组、实体有类型、脏命名（commit/绝对路径/任务号）、
摘要均长、零边 episode 占比、每条调用数、每条 wall time、终态分布。

只读：不写图、不发模型调用、不动断路器。状态文件与报告是本工具唯一的写入。
调参本身不由本工具执行——旋钮注册表（KNOBS）写明每个旋钮在哪里生效、是否已实现，
报告里只把「执行者声明已应用」的调参记为实践过，没实现的旋钮只能记为建议。

用法（账本在 NAS 容器 /backup/model-attempts.sqlite3；图库指标需在 kg-hub-server 容器内跑）：
  python -m tools.digest_retro status     --state /state/digest-retro.json --journal /backup/model-attempts.sqlite3
  python -m tools.digest_retro gate       --state ... --journal ...            # rc 2 = 该复盘了
  python -m tools.digest_retro checkpoint --state ... --journal ... --lessons-file round.md \
          --adjust live_per_cycle=20 --adjust normalize_edge_names=true [--no-graph]
  python -m tools.digest_retro final      --state ... > final.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_EVERY = 500

# schema.py 会把 graphiti_core 拉进来；本工具要能在只有 sqlite 的环境里跑 status/gate，
# 所以这里保留一份与 schema.EDGE_TYPES 同步的清单，并在能 import 时以 schema 为准。
_EDGE_FALLBACK = (
    "caused_by", "fixed_by", "verified_by", "references", "modified", "depends_on",
    "supersedes", "derived_from", "occurred_in", "belongs_to", "extracted_from",
    "relates_to", "documented_in", "diagnosed_by", "implemented_as",
)
_ENTITY_FALLBACK = (
    "Person", "Project", "File", "Tool", "Concept", "Issue", "Fix", "Config",
    "Session", "Observation", "Capsule", "KnowledgeDoc", "Lesson",
)


def schema_names() -> tuple[set[str], set[str]]:
    try:
        import schema  # noqa: WPS433
        return {n.lower() for n in schema.EDGE_TYPES}, set(schema.ENTITY_TYPES)
    except Exception:  # pragma: no cover - 容器外没有 graphiti_core
        return set(_EDGE_FALLBACK), set(_ENTITY_FALLBACK)


# 旋钮注册表：复盘能「调」的东西必须在这里登记，写明在哪生效、是否已实现。
# 没实现的旋钮不能被 --adjust 接受（除非 --force），报告里只记为建议。
KNOBS: dict[str, dict] = {
    "live_per_cycle": {
        "applies": "refinery 环境变量 KG_HUB_REFINERY_LIVE_PER_CYCLE（改后需重启 refinery）",
        "implemented": True, "metric": "wall_p90_s"},
    "backlog_per_cycle": {
        "applies": "refinery 环境变量 KG_HUB_REFINERY_BACKLOG_PER_CYCLE（改后需重启 refinery）",
        "implemented": True, "metric": "wall_p90_s"},
    "normalize_edge_names": {
        "applies": "轮末执行 python -m tools.normalize_edge_names --apply（大小写归一）",
        "implemented": True, "metric": "edge_in_schema"},
    "route_model": {
        "applies": "credvault 生产路由表 kg_hub.entity_extract 的 provider/model（网关侧由用户改）",
        "implemented": True, "metric": "edge_in_schema"},
    "parallel_tasks": {
        "applies": "服务端环境变量 KG_HUB_PARALLEL_TASKS",
        "implemented": True, "metric": "uncertain_share"},
    "prompt_version": {
        "applies": "config/agent_extract_prompt.md 的规则版本（tools/agent_extract.PROMPT_VERSION），只在模型切换边界改",
        "implemented": True, "metric": "edges_per_episode"},
    "batch_size": {
        "applies": "执行器 tools/agent_extract next --n（每批观测数）",
        "implemented": True, "metric": "wall_p90_s"},
    "type_guard_on_resolution": {
        "applies": "服务端 utils/extracted_ingest：向量召回的候选必须带抽出的实体类型标签才复用（精确同名不受限）",
        "implemented": True, "metric": "untyped_share"},
    "resolution_strictness": {
        "applies": "服务端 utils/extracted_ingest 的词面校验：代码类名字只认完全相等、只差数字不复用、"
                   "包含占比 CONTAIN_MIN_SHARE、difflib LEXICAL_MIN_RATIO（需发布）",
        "implemented": True, "metric": "dup_triple_share"},
    "submit_dry_run": {
        "applies": "执行器 tools/agent_extract submit --dry-run：写入前预览每个实体会并到谁",
        "implemented": True, "metric": "dup_triple_share"},
    "edge_schema_map_on_ingest": {
        "applies": "入图时把 schema 外边名映射为 relates_to 并保留原名（待实现）",
        "implemented": False, "metric": "edge_in_schema"},
    "entity_name_filter": {
        "applies": "入图时把 commit 哈希/绝对路径/任务号降级为属性而非节点（待实现）",
        "implemented": False, "metric": "dirty_name_share"},
    # 网关侧四个旋钮（docs/EXTRACTION-EVAL-FRAMEWORK.md §3）：全是 kg-hub-server 的环境变量，
    # 改后需发布服务端才生效；运行清单 runs/<run_id>.json 的 knobs 里记「本次实验假定的值」。
    "gateway.post_normalize": {
        "applies": "服务端环境变量 KG_HUB_POST_NORMALIZE（默认 1；网关路径入图后边名归 schema、无类型兜底、"
                   "脏命名降级，见 utils/gateway_post_normalize），改后需发布 kg-hub-server",
        "implemented": True, "metric": "edge_in_schema"},
    "gateway.custom_extraction": {
        "applies": "服务端环境变量 KG_HUB_CUSTOM_EXTRACTION（默认 1；把 config/gateway_extraction_instructions.md "
                   "注入 graphiti 抽取提示词），改后需发布 kg-hub-server；改指令正文也需发布",
        "implemented": True, "metric": "edge_in_schema"},
    "gateway.max_tokens": {
        "applies": "服务端环境变量 KG_HUB_LLM_MAX_TOKENS（默认 8192；graphiti_client 单次模型调用的输出上限，"
                   "长观测截断看这里），改后需发布 kg-hub-server",
        "implemented": True, "metric": "zero_edge_share"},
    "gateway.edge_empty_retry": {
        "applies": "抽边阶段空结果守卫：ENTITIES ≥ 4 且 edges 为空时追加提示重问一次（待实现，用户决定；"
                   "成本上限 +7% 调用），落地后为服务端环境变量，需发布",
        "implemented": False, "metric": "zero_edge_share"},
}

THRESHOLDS = {
    "edge_in_schema_min": 0.50,
    "dup_triple_max": 0.05,
    "dirty_name_max": 0.03,
    "zero_edge_max": 0.15,
    "uncertain_max": 0.20,
    "wall_p90_max_s": 600.0,
    "untyped_max": 0.20,
}

DIRTY_PREFIXES = ("commit ", "/Users/", "/volume", "T-0")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _q(xs: list[float], f: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * f))]


# ---------- 纯函数：指标 ----------

def graph_metrics(episodes: list[dict], edges: list[dict], entities: list[dict]) -> dict:
    """episodes: {entities:int, edges:int}; edges: {name, count, dups}（按小写名聚合）;
    entities: {name, summary_len:int, typed:bool}。"""
    edge_names, _ = schema_names()
    n_eps = len(episodes)
    total_edges = sum(e["count"] for e in edges)
    in_schema = sum(e["count"] for e in edges if (e["name"] or "").lower() in edge_names)
    dups = sum(e.get("dups", 0) for e in edges)
    n_ent = len(entities)
    dirty = sum(1 for x in entities if (x.get("name") or "").startswith(DIRTY_PREFIXES))
    typed = sum(1 for x in entities if x.get("typed"))
    return {
        "episodes": n_eps,
        "entities_per_episode": round(sum(e["entities"] for e in episodes) / n_eps, 2) if n_eps else None,
        "edges_per_episode": round(sum(e["edges"] for e in episodes) / n_eps, 2) if n_eps else None,
        "zero_edge_share": round(sum(1 for e in episodes if e["edges"] == 0) / n_eps, 4) if n_eps else None,
        "edges": total_edges,
        "edge_in_schema": round(in_schema / total_edges, 4) if total_edges else None,
        "edge_distinct_names": len({(e["name"] or "").lower() for e in edges}),
        "dup_triple_share": round(dups / total_edges, 4) if total_edges else None,
        "entities": n_ent,
        "untyped_share": round((n_ent - typed) / n_ent, 4) if n_ent else None,
        "dirty_name_share": round(dirty / n_ent, 4) if n_ent else None,
        "summary_len_avg": round(sum(x.get("summary_len", 0) for x in entities) / n_ent, 1) if n_ent else None,
    }


def task_metrics(tasks: list[dict]) -> dict:
    """tasks: {state, started_at, finished_at, calls:int}。"""
    states: dict[str, int] = {}
    walls, calls = [], []
    for t in tasks:
        states[t["state"]] = states.get(t["state"], 0) + 1
        if t["state"] == "succeeded" and t.get("started_at") and t.get("finished_at"):
            a = datetime.fromisoformat(t["started_at"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(t["finished_at"].replace("Z", "+00:00"))
            walls.append((b - a).total_seconds())
            if t.get("calls") is not None:
                calls.append(t["calls"])
    terminal = sum(v for k, v in states.items() if k in ("succeeded", "failed", "uncertain"))
    return {
        "tasks": len(tasks),
        "states": states,
        "succeeded": states.get("succeeded", 0),
        "uncertain_share": round(states.get("uncertain", 0) / terminal, 4) if terminal else None,
        "failed_share": round(states.get("failed", 0) / terminal, 4) if terminal else None,
        "wall_med_s": round(statistics.median(walls), 1) if walls else None,
        "wall_p90_s": round(_q(walls, 0.9), 1) if walls else None,
        "calls_med": statistics.median(calls) if calls else None,
        "calls_mean": round(sum(calls) / len(calls), 2) if calls else None,
    }


def derive_findings(metrics: dict, thresholds: dict | None = None) -> list[dict]:
    """规则化的「观察 → 建议 → 旋钮」。执行者在此之上写自己的经验教训。"""
    t = {**THRESHOLDS, **(thresholds or {})}
    out: list[dict] = []

    # (指标, 阈值键, 方向, 观察文案, 建议, 旋钮)。文案只在越线时才格式化，缺指标直接跳过。
    rules = [
        ("edge_in_schema", "edge_in_schema_min", "<", "边名合规率 {v:.0%} 低于 {thr:.0%}",
         "轮末跑 normalize_edge_names 归一大小写；长期靠入图时映射到 schema 名", "normalize_edge_names"),
        ("dup_triple_share", "dup_triple_max", ">", "重复三元组 {v:.1%} 高于 {thr:.0%}",
         "检查 edge dedupe 的向量阈值与批量去重；考虑降低并行任务数", "parallel_tasks"),
        ("dirty_name_share", "dirty_name_max", ">", "commit/绝对路径/任务号当实体名 {v:.1%} 高于 {thr:.0%}",
         "入图时把这类名字降级为属性", "entity_name_filter"),
        ("zero_edge_share", "zero_edge_max", ">", "零边 episode {v:.1%} 高于 {thr:.0%}",
         "检查 edge_phase 是否被 max_tokens/超时截断；换模型或缩小单次输入", "route_model"),
        ("untyped_share", "untyped_max", ">", "无类型实体 {v:.1%} 高于 {thr:.0%}",
         "实体类型提示词或换模型", "route_model"),
        ("uncertain_share", "uncertain_max", ">", "结果未知（uncertain）占终态 {v:.1%} 高于 {thr:.0%}",
         "这是终态可靠性不是模型质量：降低并行/每轮名额，查网关 150s 路由超时与供应商中断", "live_per_cycle"),
        ("wall_p90_s", "wall_p90_max_s", ">", "每条 wall p90 {v:.0f}s 高于 {thr:.0f}s",
         "减少每条调用数（关 attribute_phase）或换更快的模型", "route_model"),
    ]
    for key, tkey, op, text, suggestion, knob in rules:
        v, thr = metrics.get(key), t[tkey]
        if v is None:
            continue
        if (op == "<" and v < thr) or (op == ">" and v > thr):
            out.append({"observation": text.format(v=v, thr=thr), "suggestion": suggestion, "knob": knob,
                        "knob_implemented": KNOBS.get(knob, {}).get("implemented", False)})
    return out


# ---------- 状态 ----------

class RetroState:
    def __init__(self, path: Path, every: int = DEFAULT_EVERY):
        self.path = Path(path)
        self.data = {"version": 1, "every": every, "started_at": _now(),
                     "rounds": [], "tuning": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            self.data.setdefault("rounds", [])
            self.data.setdefault("tuning", {})
            if every != DEFAULT_EVERY:
                self.data["every"] = every

    @property
    def every(self) -> int:
        return int(self.data.get("every", DEFAULT_EVERY))

    @property
    def rounds(self) -> list[dict]:
        return self.data["rounds"]

    @property
    def tuning(self) -> dict:
        return self.data["tuning"]

    def window_since(self) -> str:
        return self.rounds[-1]["window"]["until"] if self.rounds else self.data["started_at"]

    def due(self, succeeded_since: int) -> bool:
        return succeeded_since >= self.every

    def checkpoint(self, *, succeeded_since: int, metrics: dict, lessons: str,
                   adjustments: dict, applied: list[str] | None = None,
                   now: str | None = None, force: bool = False) -> dict:
        until = now or _now()
        unknown = [k for k in adjustments if k not in KNOBS]
        unimplemented = [k for k in adjustments if k in KNOBS and not KNOBS[k]["implemented"]]
        if unknown and not force:
            raise ValueError(f"未登记的旋钮：{unknown}（先在 KNOBS 登记，或 --force）")
        if unimplemented and not force:
            raise ValueError(f"旋钮尚未实现，不能记为已应用：{unimplemented}")
        changes = {k: {"from": self.tuning.get(k), "to": v} for k, v in adjustments.items()}
        self.tuning.update(adjustments)
        rec = {
            "round": len(self.rounds) + 1,
            "closed_at": until,
            "window": {"since": self.window_since(), "until": until},
            "succeeded": succeeded_since,
            "metrics": metrics,
            "findings": derive_findings(metrics),
            "lessons": lessons.strip(),
            "adjustments": changes,
            "applied_from_previous": list(applied or []),
            "tuning_after": dict(self.tuning),
        }
        self.rounds.append(rec)
        return rec

    def tune(self, adjustments: dict, force: bool = False) -> dict:
        """把调参补登记到最后一轮（checkpoint 之后决定的调参）。"""
        unknown = [k for k in adjustments if k not in KNOBS]
        unimplemented = [k for k in adjustments if k in KNOBS and not KNOBS[k]["implemented"]]
        if unknown and not force:
            raise ValueError(f"未登记的旋钮：{unknown}")
        if unimplemented and not force:
            raise ValueError(f"旋钮尚未实现，不能记为已应用：{unimplemented}")
        last = self.rounds[-1]
        changes = {k: {"from": self.tuning.get(k), "to": v} for k, v in adjustments.items()}
        self.tuning.update(adjustments)
        last.setdefault("adjustments", {}).update(changes)
        last["tuning_after"] = dict(self.tuning)
        return changes

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        os.replace(tmp, self.path)


# ---------- 报告 ----------

_COLS = [("succeeded", "条"), ("wall_med_s", "每条中位 s"), ("wall_p90_s", "p90 s"),
         ("calls_mean", "调用/条"), ("uncertain_share", "未知终态"), ("edge_in_schema", "边名合规"),
         ("edge_distinct_names", "边名种类"), ("dup_triple_share", "重复三元组"),
         ("dirty_name_share", "脏命名"), ("zero_edge_share", "零边"), ("summary_len_avg", "摘要均长")]


def _fmt(k: str, v) -> str:
    if v is None:
        return "—"
    if k.endswith("_share") or k == "edge_in_schema":
        return f"{v:.1%}"
    return f"{v}"


def render_round(rec: dict) -> str:
    m = rec["metrics"]
    lines = [f"# 消化复盘 · 第 {rec['round']} 轮", "",
             f"窗口：{rec['window']['since']} → {rec['window']['until']}；本轮 succeeded {rec['succeeded']} 条", "",
             "## 指标", "", "| 指标 | 值 |", "|---|---|"]
    lines += [f"| {label} | {_fmt(k, m.get(k))} |" for k, label in _COLS]
    lines += ["", "## 规则发现（观察 → 建议 → 旋钮）", ""]
    if rec["findings"]:
        for f in rec["findings"]:
            tag = "" if f["knob_implemented"] else "（旋钮待实现，只能记为建议）"
            lines.append(f"- {f['observation']} → {f['suggestion']} → `{f['knob']}`{tag}")
    else:
        lines.append("- 无越线项")
    lines += ["", "## 执行者经验教训", "", rec["lessons"] or "（空）", ""]
    if rec["applied_from_previous"]:
        lines += ["## 本轮已应用上一轮的调参", ""] + [f"- {x}" for x in rec["applied_from_previous"]] + [""]
    lines += ["## 下一轮调参（from → to）", ""]
    if rec["adjustments"]:
        for k, c in rec["adjustments"].items():
            lines.append(f"- `{k}`: {c['from']!r} → {c['to']!r}（{KNOBS.get(k, {}).get('applies', '未登记')}）")
    else:
        lines.append("- 无")
    return "\n".join(lines) + "\n"


def render_final(state: RetroState, title: str = "消化全程对比") -> str:
    rounds = state.rounds
    lines = [f"# {title}", "", f"复盘周期：每 {state.every} 条；共 {len(rounds)} 轮。", "",
             "## 各轮指标", "", "| 轮 | " + " | ".join(l for _, l in _COLS) + " |",
             "|---|" + "---|" * len(_COLS)]
    for r in rounds:
        m = r["metrics"]
        lines.append(f"| {r['round']} | " + " | ".join(_fmt(k, m.get(k)) for k, _ in _COLS) + " |")
    lines += ["", "## 实践过的优化意见（含前后指标变化）", ""]
    practiced = 0
    for i, r in enumerate(rounds):
        nxt = rounds[i + 1]["metrics"] if i + 1 < len(rounds) else None
        for k, c in r["adjustments"].items():
            practiced += 1
            metric = KNOBS.get(k, {}).get("metric")
            before = r["metrics"].get(metric) if metric else None
            after = nxt.get(metric) if (nxt and metric) else None
            delta = (f"{metric}: {_fmt(metric, before)} → {_fmt(metric, after)}"
                     if metric and nxt else f"{metric}: 下一轮未发生，无法评估" if metric else "")
            lines.append(f"- 第 {r['round']} 轮后调 `{k}` {c['from']!r} → {c['to']!r}；依据：{_short(r['lessons'])}；{delta}")
    if not practiced:
        lines.append("- 无（没有任何一轮登记过调参）")
    lines += ["", "## 提出但未实践的建议", ""]
    pending = {}
    for r in rounds:
        for f in r["findings"]:
            if not f["knob_implemented"] or f["knob"] not in r["adjustments"]:
                pending.setdefault(f["knob"], f["suggestion"])
    lines += [f"- `{k}`：{v}（{KNOBS.get(k, {}).get('applies', '')}）" for k, v in pending.items()] or ["- 无"]
    return "\n".join(lines) + "\n"


def _short(s: str, n: int = 80) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------- 加载器（账本 / 图库） ----------

def load_tasks(journal_path: str | Path, since: str, with_calls: bool = True) -> list[dict]:
    """task_executions 自 since 之后结束/开始的任务；calls 走 (source_description, source_obs_id)
    索引逐任务数，不扫 model_attempts。"""
    conn = sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=1")
    rows = conn.execute(
        "SELECT source_description, source_obs_id, state, started_at, finished_at "
        "FROM task_executions WHERE coalesce(finished_at, started_at) > ?", (since,)).fetchall()
    out = []
    for sd, sid, st, a, b in rows:
        calls = None
        if with_calls and st == "succeeded":
            calls = conn.execute(
                "SELECT count(*) FROM model_attempts WHERE source_description=? AND source_obs_id=? "
                "AND phase='completed'", (sd, sid)).fetchone()[0]
        out.append({"state": st, "started_at": a, "finished_at": b, "calls": calls})
    conn.close()
    return out


def succeeded_since(journal_path: str | Path, since: str) -> int:
    conn = sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True)
    n = conn.execute("SELECT count(*) FROM task_executions WHERE state='succeeded' AND finished_at > ?",
                     (since,)).fetchone()[0]
    conn.close()
    return int(n)


# 会话自抽取路径（/api/ingest_extracted）：观测如果早先失败过，账本里已有它的执行记录，
# 重跑不会再记一行（utils/task_execution 的既有契约，#163 查实），按账本数会严重漏计。
# 图里 extraction_mode='agent' 的 episode 才是这条路「成功入库」的正本。
async def graph_succeeded_since(driver, since: str, until: str | None = None) -> int:
    rows, _, _ = await driver.execute_query(
        "MATCH (e:Episodic) WHERE e.extraction_mode = 'agent' AND toString(e.created_at) > $s "
        "AND ($u IS NULL OR toString(e.created_at) <= $u) RETURN count(e) AS c", s=since, u=until)
    return int(rows[0]["c"]) if rows else 0


async def load_graph(driver, since: str, until: str) -> tuple[list[dict], list[dict], list[dict]]:
    """在 kg-hub-server 容器内用 graphiti driver 跑三条只读聚合（FalkorDB 不支持 =~）。"""
    _, entity_types = schema_names()

    async def q(cy, **p):
        rows, _, _ = await driver.execute_query(cy, **p)
        return rows
    # 边数用 episode 自带的 entity_edges（graphiti add_episode 与预抽取路径都写它）：
    # 「扫全部边看 r.episodes 包含」在 16 万条边上每条 episode 都要全表扫，500 条就撞 30s 查询上限。
    eps = await q(
        "MATCH (e:Episodic) WHERE toString(e.created_at) >= $s AND toString(e.created_at) < $u "
        "OPTIONAL MATCH (e)-[:MENTIONS]->(n) WITH e, count(n) AS k "
        "RETURN e.uuid AS u, k AS entities, size(coalesce(e.entity_edges, [])) AS edges", s=since, u=until)
    edges = await q(
        "MATCH (a)-[r:RELATES_TO]->(b) WHERE toString(r.created_at) >= $s AND toString(r.created_at) < $u "
        "WITH a.uuid+'|'+toLower(r.name)+'|'+b.uuid AS k, toLower(r.name) AS n, count(r) AS c "
        "RETURN n AS name, sum(c) AS count, sum(CASE WHEN c>1 THEN c-1 ELSE 0 END) AS dups", s=since, u=until)
    ents = await q(
        "MATCH (n:Entity) WHERE toString(n.created_at) >= $s AND toString(n.created_at) < $u "
        "RETURN n.name AS name, size(coalesce(n.summary,'')) AS summary_len, labels(n) AS l", s=since, u=until)
    return (
        [{"entities": r["entities"], "edges": r["edges"]} for r in eps],
        [{"name": r["name"], "count": r["count"], "dups": r["dups"]} for r in edges],
        [{"name": r["name"], "summary_len": r["summary_len"],
          "typed": any(x in entity_types for x in (r["l"] or []))} for r in ents],
    )


# ---------- CLI ----------

def _parse_adjust(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--adjust 需要 key=value：{it}")
        k, v = it.split("=", 1)
        try:
            out[k.strip()] = json.loads(v)
        except json.JSONDecodeError:
            out[k.strip()] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "gate", "checkpoint", "final", "tune"])
    ap.add_argument("--state", required=True, help="digest-retro.json 路径（状态卷）")
    ap.add_argument("--journal", help="model-attempts.sqlite3 路径")
    ap.add_argument("--every", type=int, default=DEFAULT_EVERY)
    ap.add_argument("--lessons-file", help="checkpoint：执行者写的经验教训 markdown")
    ap.add_argument("--adjust", action="append", default=[], help="checkpoint：key=value，可多次")
    ap.add_argument("--applied", action="append", default=[], help="checkpoint：本轮已应用的上一轮调参说明")
    ap.add_argument("--no-graph", action="store_true", help="checkpoint：不查图库（容器外）")
    ap.add_argument("--graph-count", action="store_true",
                    help="succeeded 按图里 extraction_mode=agent 的 episode 数，而不是账本（会话自抽取路径用）")
    ap.add_argument("--report-dir", default=os.environ.get("KG_HUB_RETRO_REPORTS", "~/.kg-hub/reports/retro"))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    st = RetroState(a.state, every=a.every)
    # 首次创建就落盘：窗口起点（started_at）必须固定，否则每次 gate 都从「现在」起数，
    # succeeded_since 永远到不了阈值，门禁形同虚设。
    if not st.path.exists():
        st.save()
    if a.cmd == "final":
        sys.stdout.write(render_final(st))
        return 0
    if a.cmd == "tune":
        # 复盘之后、下一轮开始之前补登记调参：执行者在 checkpoint 时只记录建议，由主会话决定后用本命令
        # 落到上一轮的 adjustments（from→to）与当前 tuning，最终对比仍能把它算作「实践过的优化」。
        if not st.rounds:
            raise SystemExit("还没有任何一轮，先 checkpoint")
        changes = st.tune(_parse_adjust(a.adjust), force=a.force)
        st.save()
        print(json.dumps({"round": st.rounds[-1]["round"], "adjustments": changes, "tuning": st.tuning},
                         ensure_ascii=False, indent=2))
        return 0
    if not a.journal:
        raise SystemExit("status/gate/checkpoint 需要 --journal")
    since = st.window_since()
    until = _now()

    async def graph_phase(need_metrics: bool):
        """图库相关的读全部放在同一个事件循环里：build → 查 → close。
        2026-10-17 第 1 轮实测：分两次 asyncio.run 时驱动的 Redis 连接绑在已关闭的第一个循环上，
        close 抛 Event loop is closed，checkpoint 连续 13 次失败、gate 偶发 rc=1。"""
        from graphiti_client import build_graphiti  # 容器内
        g = await build_graphiti()
        try:
            count = await graph_succeeded_since(g.driver, since) if a.graph_count else None
            triple = await load_graph(g.driver, since, until) if need_metrics else None
        finally:
            await g.driver.close()
        return count, triple

    graph_triple = None
    if a.graph_count or (a.cmd == "checkpoint" and not a.no_graph):
        count, graph_triple = asyncio.run(graph_phase(a.cmd == "checkpoint" and not a.no_graph))
        n = count if a.graph_count else succeeded_since(a.journal, since)
    else:
        n = succeeded_since(a.journal, since)
    if a.cmd == "status":
        print(json.dumps({"round_next": len(st.rounds) + 1, "every": st.every, "since": since,
                          "succeeded_since": n, "due": st.due(n), "tuning": st.tuning},
                         ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "gate":
        print(f"succeeded_since={n} every={st.every} due={st.due(n)}")
        return 2 if st.due(n) else 0

    lessons = Path(a.lessons_file).read_text() if a.lessons_file else ""
    if not lessons.strip() and not a.force:
        raise SystemExit("checkpoint 必须带 --lessons-file（经验教训不能为空）；确需空复盘用 --force")
    metrics = task_metrics(load_tasks(a.journal, since))
    if a.graph_count:
        metrics["succeeded"] = n
    if graph_triple is not None:
        eps, edges, ents = graph_triple
        metrics.update(graph_metrics(eps, edges, ents))
    rec = st.checkpoint(succeeded_since=n, metrics=metrics, lessons=lessons,
                        adjustments=_parse_adjust(a.adjust), applied=a.applied, now=until, force=a.force)
    st.save()
    rd = Path(os.path.expanduser(a.report_dir))
    rd.mkdir(parents=True, exist_ok=True)
    out = rd / f"round-{rec['round']:03d}.md"
    out.write_text(render_round(rec))
    print(f"round {rec['round']} closed: succeeded={n}; report {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
