"""tools/eval_report.py — 把三个测量面合成一张并排表（docs/EXTRACTION-EVAL-FRAMEWORK.md §4）。

行 = 一个 run（--run-id，默认）、一个模型（--model）或一个执行器（--executor）；列顺序固定：
  纪律 → 丰富度 → 消解 → 成本 → 检索
三个测量面各自的来源，缺哪面就留空，不互相顶替：
  图口径    主图按同一分组键聚合（tools.extractor_compare.collect，容器内），或 --graph-json 读
            ``extractor_compare --json --group-by <键>`` 在容器里导出的文件（容器外就靠它）
  提交口径  agent_extract 状态目录的 log.jsonl（tools.agent_extract.summarize_log 按同一键汇总）：
            ok/empty/error、reuse_mismatch/条、服务端写图时延
  成本口径  会话型执行器：运行清单 runs/<run_id>.json 的 self_reported（只在按 run 分组时有）；
            网关路径：--journal 账本 + --sample 样本 + --business-key label=key（复用 tools.ab_score 的
            账本口径）。两者分列，不合并（框架 §9：自报与账本不可直接相加）
  检索      --recall label=value（tools/kg_eval.py 的召回，入图后跑一遍手填）；没给留空

只读；不调模型、不动断路器、不写图。

用法：
  python -m tools.eval_report --run-id 20261019-fable-v3-s50 --run-id 20261019-codex-s50 \
      --graph-json compare-run.json [--recall 20261019-fable-v3-s50=0.82] [--md report.md] [--json out.json]
  python -m tools.eval_report --model claude-fable-5-1 --model claude-opus-5-5 --graph-json compare-model.json
  python -m tools.eval_report --run-id A --run-id B --journal /backup/model-attempts.sqlite3 \
      --sample docs/ab-sample-20261017.json --business-key B=kg_hub.entity_extract   # 容器内，B 是网关路径的 run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import agent_extract as ax  # noqa: E402
from tools.ab_score import journal_route_metrics, parse_pairs  # noqa: E402

# 分组键 → (extractor_compare 的 --group-by, summarize_log 的 key)
GROUPS = {"run_id": ("run_id", "run_id"), "model": ("extractor_model", "model"),
          "executor": ("extractor_tool", "executor")}

# (指标键, 表头, 来源面)。顺序就是报表列顺序：纪律 → 丰富度 → 消解 → 成本 → 检索。
COLUMNS: list[tuple[str, str, str]] = [
    ("episodes", "条（图）", "graph"),
    # 纪律
    ("edge_in_schema", "纪律·边名合规", "graph"),
    ("edge_distinct_names", "纪律·边名种类", "graph"),
    ("untyped_share", "纪律·无类型", "graph"),
    ("dirty_name_share", "纪律·脏命名", "graph"),
    ("dup_triple_share", "纪律·重复三元组", "graph"),
    ("zero_edge_share", "纪律·零边", "graph"),
    # 丰富度
    ("entities_per_episode", "丰富度·实体/条", "graph"),
    ("edges_per_episode", "丰富度·边/条", "graph"),
    ("summary_len_avg", "丰富度·摘要均长", "graph"),
    # 消解（提交口径）
    ("ok", "消解·ok", "submit"),
    ("empty", "消解·empty", "submit"),
    ("error", "消解·error", "submit"),
    ("reuse_mismatch_per_obs", "消解·误并候选/条", "submit"),
    ("latency_med_s", "消解·写图中位 s", "submit"),
    # 成本（自报与账本分列）
    ("input_tokens_per_obs", "成本·输入 token/条（自报）", "self"),
    ("output_tokens_per_obs", "成本·输出 token/条（自报）", "self"),
    ("wall_s_per_obs", "成本·wall s/条（自报）", "self"),
    ("calls_mean", "成本·调用/条（账本）", "journal"),
    ("wall_med_s", "成本·wall 中位 s（账本）", "journal"),
    # 检索
    ("recall", "检索·kg_eval 召回", "recall"),
]
SHARE_KEYS = {"edge_in_schema", "untyped_share", "dirty_name_share", "dup_triple_share",
              "zero_edge_share", "recall"}


# ---------- 三面装配（纯函数，可用假数据测） ----------

def self_cost(manifest: dict | None, ok: int | None) -> dict:
    """运行清单自报成本摊到每条 ok 提交。没有清单或没有 ok 条数就留空，不猜。"""
    if not manifest or not ok:
        return {}
    sr = manifest.get("self_reported") or {}
    per = lambda v: round(float(v) / ok, 1) if v is not None else None  # noqa: E731
    return {"input_tokens_per_obs": per(sr.get("input_tokens")),
            "output_tokens_per_obs": per(sr.get("output_tokens")),
            "wall_s_per_obs": per(sr.get("wall_s"))}


def assemble(labels: list[str], *, graph: dict[str, dict] | None = None,
             submit: dict[str, dict] | None = None, runs: dict[str, dict | None] | None = None,
             journal: dict[str, dict] | None = None, recall: dict[str, float] | None = None) -> dict[str, dict]:
    """把各面按标签拼成 {label: {指标: 值}}。缺面的列留 None。"""
    graph, submit, runs = graph or {}, submit or {}, runs or {}
    journal, recall = journal or {}, recall or {}
    out: dict[str, dict] = {}
    for label in labels:
        row: dict = {}
        g = graph.get(label) or {}
        s = submit.get(label) or {}
        row.update({k: g.get(k) for k, _, src in COLUMNS if src == "graph"})
        row.update({k: s.get(k) for k, _, src in COLUMNS if src == "submit"})
        row.update({k: None for k, _, src in COLUMNS if src in ("self", "journal")})
        row.update(self_cost(runs.get(label), s.get("ok")))
        j = journal.get(label) or {}
        row.update({k: j.get(k) for k, _, src in COLUMNS if src == "journal" and k in j})
        row["recall"] = recall.get(label)
        row["_manifest"] = {k: (runs.get(label) or {}).get(k)
                            for k in ("executor", "model", "prompt_version", "server_commit", "sample")} \
            if runs.get(label) else None
        out[label] = row
    return out


def _fmt(k: str, v) -> str:
    if v is None:
        return "—"
    if k in SHARE_KEYS:
        return f"{v:.1%}"
    return f"{v}"


def render(table: dict[str, dict], group_by: str = "run_id", title: str | None = None) -> str:
    head = {"run_id": "run", "model": "模型", "executor": "执行器"}[group_by]
    lines = [f"# {title or '抽取对比：三面合一'}", "",
             f"| {head} | " + " | ".join(h for _, h, _ in COLUMNS) + " |",
             "|---|" + "---|" * len(COLUMNS)]
    for label, row in table.items():
        lines.append(f"| {label} | " + " | ".join(_fmt(k, row.get(k)) for k, _, _ in COLUMNS) + " |")
    lines += ["",
              "读法：列顺序 纪律 → 丰富度 → 消解 → 成本 → 检索。图口径按主图分组聚合（extractor_compare），"
              "消解列来自 agent_extract 的提交日志，成本「自报」来自运行清单、「账本」来自 model-attempts，两者不可相加；"
              "检索列由 tools/kg_eval.py 跑完后手填。— 表示该面没有数据。", ""]
    manifests = {l: r.get("_manifest") for l, r in table.items() if r.get("_manifest")}
    if manifests:
        lines.append("## 运行清单")
        lines.append("")
        for label, m in manifests.items():
            lines.append(f"- **{label}**：executor {m.get('executor')} · model {m.get('model')} · "
                         f"prompt {m.get('prompt_version')} · server {m.get('server_commit')} · sample {m.get('sample')}")
        lines.append("")
    return "\n".join(lines)


# ---------- 加载（可注入） ----------

def load_submit(labels: list[str], key: str, log_path: Path | None = None) -> dict[str, dict]:
    p = log_path or (ax.state_dir() / "log.jsonl")
    if not p.exists():
        return {}
    recs = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    summary = ax.summarize_log(recs, key)
    return {l: summary[l] for l in labels if l in summary}


def load_runs(labels: list[str]) -> dict[str, dict | None]:
    return {l: ax.load_run(l) for l in labels}


def load_graph_json(path: str | Path) -> dict[str, dict]:
    return json.loads(Path(path).read_text())


def load_journal(labels: list[str], journal: str, sample: str, keys: dict[str, str]) -> dict[str, dict]:
    items = json.loads(Path(sample).read_text())["items"]
    return {l: journal_route_metrics(journal, keys[l], items) for l in labels if l in keys}


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", action="append", default=[], help="按 run 并排，可多次")
    ap.add_argument("--model", action="append", default=[], help="按模型并排，可多次")
    ap.add_argument("--executor", action="append", default=[], help="按执行器并排，可多次")
    ap.add_argument("--graph-json", help="extractor_compare --json 的输出（容器外用它代替直连图库）")
    ap.add_argument("--group", default="kg_hub", help="直连图库时的 group_id")
    ap.add_argument("--journal", help="model-attempts.sqlite3（网关路径成本，容器内）")
    ap.add_argument("--sample", help="与 --journal 搭配的样本 JSON（docs/ab-sample-20261017.json）")
    ap.add_argument("--business-key", action="append", default=[], help="label=business_key，可多次")
    ap.add_argument("--recall", action="append", default=[], help="label=召回率（kg_eval 结果），可多次")
    ap.add_argument("--md", help="把表写到这个文件（默认只打印）")
    ap.add_argument("--json", dest="json_out", help="把各行指标写成 JSON")
    ap.add_argument("--title")
    a = ap.parse_args(argv)

    chosen = [(k, v) for k, v in (("run_id", a.run_id), ("model", a.model), ("executor", a.executor)) if v]
    if len(chosen) != 1:
        raise SystemExit("--run-id / --model / --executor 三选一，至少给一个、不能混用")
    group_by, labels = chosen[0]
    graph_key, submit_key = GROUPS[group_by]
    try:
        keys = parse_pairs(a.business_key, "business-key")
        recall = {k: float(v) for k, v in parse_pairs(a.recall, "recall").items()}
    except ValueError as exc:
        raise SystemExit(str(exc))

    if a.graph_json:
        graph = load_graph_json(a.graph_json)
    else:
        import asyncio
        from graphiti_client import build_graphiti  # 容器内
        from tools.extractor_compare import collect

        async def run():
            g = await build_graphiti()
            try:
                return await collect(g.driver, a.group, graph_key)
            finally:
                await g.driver.close()
        graph = asyncio.run(run())
    submit = load_submit(labels, submit_key)
    runs = load_runs(labels) if group_by == "run_id" else {}
    journal = {}
    if a.journal:
        if not a.sample:
            raise SystemExit("--journal 需要同时给 --sample")
        journal = load_journal(labels, a.journal, a.sample, keys)
    table = assemble(labels, graph=graph, submit=submit, runs=runs, journal=journal, recall=recall)
    md = render(table, group_by, a.title)
    if a.md:
        Path(a.md).write_text(md)
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(table, ensure_ascii=False, indent=2))
    sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
