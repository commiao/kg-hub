"""观测分诊对比套件：冻结一批带标签的观测样本，离线评 utils/obs_triage 的规则——改规则必须跑它。

和 tools/extract_bench 一个套路：样本含观测正文，只放本机 ~/.kg-hub/bench/<bench_id>/（KG_HUB_BENCH_ROOT 可改），
不进仓库；每次评分写一条运行记录（规则版本、配置摘要、三档矩阵、每条规则的命中与误杀）到 <bench>/runs/。

子命令：
  sample  --days 30 --n 200 --per-session 3 --out <jsonl>     从 NAS 拉时间窗内全部观测，本机按项目分层、每会话限额抽样，
          附现行闸判定（score/accept/why）和会话-日规模。第一行是总体统计。NAS 大读取只在 23 点后跑。
  sheet   --sample <jsonl> --out <json>        出盲标表：只有 id/项目/类型/标题/副标题/正文/facts，不带分数。
          标注协议（H/M/L + 一句理由 + topic_mismatch）见 docs/OBSERVATION-TRIAGE.md。
  freeze  --sample <jsonl> --labels <jsonl> --bench-id <id>    冻结：样本 + 标签 + manifest；id 偶数为 dev、奇数为 holdout。
  score   --bench <目录> [--triage-config <json>] [--label <名>] [--json]
          现行闸 vs 分诊：按 全部/dev/holdout 三组给「标签 × 三档」矩阵、误杀率、净丢弃率、review 占比；
          每条规则：命中数按标签分、命中的高价值 id（误杀样本）、命中的低价值 id（命中样本）。
  shadow  --from <jsonl> [--triage-config <json>]   对一批没标签的观测（sample 的输出或任何同构 jsonl）只出三档分布与规则命中，
          看线上影子模式下规则会怎么判。

判读口径：误杀率 = 高价值落 drop 的比例（目标 ≤ 现行闸）；净丢弃 = 全体落 drop 的比例；review 是将来只对这一档
用最便宜模型复核的预算。dev 上调规则，holdout 上确认没过拟合——两组差得多就是在背样本。
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import obs_triage as ot  # noqa: E402
from utils.ingest_filter import evaluate, load_config  # noqa: E402

TOOL = "obs_bench"
LABELS = ("H", "M", "L")
SPLITS = ("all", "dev", "holdout")
_REMOTE = Path(__file__).resolve().parent / "obs_sample_remote.py"


def bench_root() -> Path:
    return Path(os.environ.get("KG_HUB_BENCH_ROOT", str(Path.home() / ".kg-hub" / "bench"))).expanduser()


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def _cfg_digest(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def split_of(obs_id: int) -> str:
    return "dev" if int(obs_id) % 2 == 0 else "holdout"


# ---------- sample ----------

def fetch_window(days: int, cut: int = 0) -> list[dict]:
    from tools.agent_extract import DEFAULT_DB, DEFAULT_NAS
    nas = os.environ.get("KG_HUB_NAS_SSH", DEFAULT_NAS)
    db = os.environ.get("KG_HUB_NAS_CLAUDE_MEM_DB", DEFAULT_DB)
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", nas, "python3", "-", db, str(days), str(cut)],
                         input=_REMOTE.read_text(), text=True, capture_output=True, timeout=900)
    if out.returncode != 0:
        raise RuntimeError(f"ssh/sqlite 读取失败 rc={out.returncode}: {out.stderr.strip()[:200]}")
    return [json.loads(x) for x in out.stdout.splitlines() if x.strip()]


def annotate(rows: list[dict], cfg: dict) -> None:
    """加现行闸判定与会话-日规模（就地改）。"""
    grp = collections.defaultdict(list)
    for r in rows:
        d = evaluate(r, cfg, None)
        r["accept"], r["score"], r["why"] = bool(d.would_accept), round(float(d.score), 1), list(d.reasons)[:2]
        grp[(r.get("memory_session_id"), (r.get("created_at") or "")[:10])].append(r)
    for g in grp.values():
        g.sort(key=lambda x: -x["score"])
        for k, r in enumerate(g):
            r["sess_n"], r["sess_rank"] = len(g), k + 1


def population(rows: list[dict]) -> dict:
    grp = collections.Counter((r.get("memory_session_id"), (r.get("created_at") or "")[:10]) for r in rows)
    sizes = sorted(grp.values()) or [0]

    def pct(p):
        return sizes[min(len(sizes) - 1, int(p * len(sizes)))]
    return {"total": len(rows), "accepted": sum(bool(r.get("accept")) for r in rows),
            "by_project": collections.Counter(r.get("project") for r in rows).most_common(15),
            "by_type": collections.Counter(r.get("type") for r in rows).most_common(),
            "by_platform": collections.Counter(r.get("platform_source") or "_default" for r in rows).most_common(),
            "session_days": len(grp), "session_day_size_p50_p90_p99_max": [pct(.5), pct(.9), pct(.99), sizes[-1]]}


def stratified(rows: list[dict], n: int, per_session: int, seed: int = 20261024) -> list[dict]:
    """按项目占比分配名额（每个项目至少 1），项目内随机、每会话最多 per_session 条。"""
    rnd = random.Random(seed)
    by_proj = collections.defaultdict(list)
    for r in rows:
        by_proj[r.get("project") or "(unknown)"].append(r)
    total = max(1, len(rows))
    quota = {p: max(1, round(n * len(v) / total)) for p, v in by_proj.items()}
    picked = []
    for p, v in sorted(by_proj.items(), key=lambda kv: -len(kv[1])):
        rnd.shuffle(v)
        per_sess = collections.Counter()
        for r in v:
            if len([x for x in picked if (x.get("project") or "(unknown)") == p]) >= quota[p]:
                break
            if per_sess[r.get("memory_session_id")] >= per_session:
                continue
            per_sess[r.get("memory_session_id")] += 1
            picked.append(r)
    return picked[:n]


def cmd_sample(a) -> int:
    rows = fetch_window(a.days, a.cut)
    cfg = load_config()
    annotate(rows, cfg)
    pop = population(rows)
    picked = stratified(rows, a.n, a.per_session, a.seed)
    with open(a.out, "w") as f:
        f.write(json.dumps({"population": pop, "days": a.days, "n": len(picked), "per_session": a.per_session,
                            "sampled_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
        for r in picked:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps({"rows": len(rows), "picked": len(picked), "accepted_pct": round(100 * pop["accepted"] / max(1, pop["total"]), 1)}))
    return 0


# ---------- sheet / freeze ----------

SHEET_FIELDS = ("id", "project", "type", "title", "subtitle", "narrative", "facts")


def sheet_rows(rows: list[dict]) -> list[dict]:
    return [{k: r.get(k) for k in SHEET_FIELDS} for r in rows if "population" not in r]


def cmd_sheet(a) -> int:
    rows = sheet_rows(read_jsonl(Path(a.sample)))
    Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(json.dumps({"items": len(rows), "out": a.out}))
    return 0


def freeze(sample: Path, labels: Path, bench_id: str, root: Path, note: str = "") -> Path:
    rows = read_jsonl(sample)
    pop = rows[0].get("population") if rows and "population" in rows[0] else None
    items = [r for r in rows if "population" not in r]
    labs = read_jsonl(labels)
    bad = [l for l in labs if l.get("value") not in LABELS]
    if bad:
        raise SystemExit(f"标签只能是 H/M/L：{[l.get('id') for l in bad][:5]}")
    ids, lids = {int(r["id"]) for r in items}, {int(l["id"]) for l in labs}
    if ids != lids:
        raise SystemExit(f"样本与标签 id 不一致：缺标签 {sorted(ids - lids)[:5]}，多标签 {sorted(lids - ids)[:5]}")
    out = root / bench_id
    if out.exists():
        raise SystemExit(f"已存在：{out}（换 bench-id，冻结集不覆盖）")
    out.mkdir(parents=True)
    (out / "items.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in items))
    (out / "labels.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in labs))
    if pop:
        (out / "population.json").write_text(json.dumps(pop, ensure_ascii=False, indent=1))
    dist = collections.Counter(l["value"] for l in labs)
    manifest = {"tool": TOOL, "bench_id": bench_id, "created_at": datetime.now(timezone.utc).isoformat(),
                "items": len(items), "labels": dict(dist), "topic_mismatch": sum(bool(l.get("topic_mismatch")) for l in labs),
                "split": "id 偶数 dev / 奇数 holdout",
                "source": {"sample": str(sample), "sample_sha": _sha(sample), "labels": str(labels), "labels_sha": _sha(labels)},
                "note": note}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    return out


def cmd_freeze(a) -> int:
    out = freeze(Path(a.sample), Path(a.labels), a.bench_id, bench_root(), a.note or "")
    print(json.dumps(json.loads((out / "manifest.json").read_text()), ensure_ascii=False, indent=1))
    return 0


# ---------- score ----------

def load_bench(d: str) -> tuple[Path, dict, list[dict], dict[int, dict]]:
    p = Path(d).expanduser()
    if not p.is_absolute() and not p.exists():
        p = bench_root() / d
    m = json.loads((p / "manifest.json").read_text())
    items = read_jsonl(p / "items.jsonl")
    labels = {int(l["id"]): l for l in read_jsonl(p / "labels.jsonl")}
    return p, m, items, labels


def verdicts(items: list[dict], filter_cfg: dict, tcfg: dict) -> list[tuple[dict, ot.Verdict]]:
    out = []
    for r in items:
        base = ot.base_from_record(r, filter_cfg) if "accept" in r else None
        out.append((r, ot.triage(r, filter_cfg, tcfg, base=base)))
    return out


def matrix(pairs: list[tuple[dict, ot.Verdict]], labels: dict[int, dict], split: str) -> dict:
    """标签 × 三档 + 汇总指标；baseline 是现行闸（拒 = drop）。"""
    sel = [(r, v) for r, v in pairs if split == "all" or split_of(r["id"]) == split]
    m = {lab: {t: 0 for t in ot.TIERS} for lab in LABELS}
    base = {lab: {"drop": 0, "keep": 0} for lab in LABELS}
    for r, v in sel:
        lab = labels[int(r["id"])]["value"]
        m[lab][v.tier] += 1
        base[lab]["keep" if v.base.would_accept else "drop"] += 1
    n = max(1, len(sel))
    h = max(1, sum(m["H"].values()))
    low = max(1, sum(m["L"].values()))
    return {"n": len(sel), "matrix": m, "baseline": base,
            "h_kill_pct": round(100 * m["H"]["drop"] / h, 1), "h_kill_pct_baseline": round(100 * base["H"]["drop"] / h, 1),
            "l_drop_pct": round(100 * m["L"]["drop"] / low, 1), "l_drop_pct_baseline": round(100 * base["L"]["drop"] / low, 1),
            "net_drop_pct": round(100 * sum(m[l]["drop"] for l in LABELS) / n, 1),
            "net_drop_pct_baseline": round(100 * sum(base[l]["drop"] for l in LABELS) / n, 1),
            "review_pct": round(100 * sum(m[l]["review"] for l in LABELS) / n, 1)}


def per_rule(pairs: list[tuple[dict, ot.Verdict]], labels: dict[int, dict]) -> dict:
    out = {}
    for r, v in pairs:
        lab = labels[int(r["id"])]["value"]
        for name in v.hits:
            e = out.setdefault(name, {"kind": ot.RULES_BY_NAME[name].kind, "hits": {l: 0 for l in LABELS}, "H_ids": [], "L_ids": []})
            e["hits"][lab] += 1
            if lab == "H":
                e["H_ids"].append(int(r["id"]))
            elif lab == "L":
                e["L_ids"].append(int(r["id"]))
    for e in out.values():
        tot = max(1, sum(e["hits"].values()))
        e["share_L_pct"] = round(100 * e["hits"]["L"] / tot, 1)
        e["share_H_pct"] = round(100 * e["hits"]["H"] / tot, 1)
    return out


def score(p: Path, m: dict, items: list[dict], labels: dict[int, dict], filter_cfg: dict, tcfg: dict, label: str = "") -> dict:
    pairs = verdicts(items, filter_cfg, tcfg)
    res = {"tool": TOOL, "bench_id": m.get("bench_id"), "label": label, "at": datetime.now(timezone.utc).isoformat(),
           "rules_version": ot.rules_version(tcfg), "triage_config_sha": _cfg_digest(tcfg),
           "filter_config_sha": _cfg_digest(filter_cfg),
           "splits": {s: matrix(pairs, labels, s) for s in SPLITS}, "rules": per_rule(pairs, labels),
           "h_dropped": [{"id": int(r["id"]), "reason": v.reason, "title": (r.get("title") or "")[:80]}
                         for r, v in pairs if labels[int(r["id"])]["value"] == "H" and v.tier == "drop"],
           "rescued": [{"id": int(r["id"]), "label": labels[int(r["id"])]["value"], "hits": v.hits}
                       for r, v in pairs if v.tier != "drop" and not v.base.would_accept]}
    return res


def render(res: dict) -> str:
    lines = [f"规则 {res['rules_version']}（配置 {res['triage_config_sha']}）  基线 = 现行闸",
             "| 组 | n | 高价值 keep/review/drop | 中 keep/review/drop | 低 keep/review/drop | 误杀率 分诊/基线 | 低价值丢弃 分诊/基线 | 净丢弃 分诊/基线 | review |",
             "|---|---|---|---|---|---|---|---|---|"]
    for s in SPLITS:
        x = res["splits"][s]
        cell = lambda l: "/".join(str(x["matrix"][l][t]) for t in ot.TIERS)  # noqa: E731
        lines.append(f"| {s} | {x['n']} | {cell('H')} | {cell('M')} | {cell('L')} | {x['h_kill_pct']}% / {x['h_kill_pct_baseline']}% | "
                     f"{x['l_drop_pct']}% / {x['l_drop_pct_baseline']}% | {x['net_drop_pct']}% / {x['net_drop_pct_baseline']}% | {x['review_pct']}% |")
    lines += ["", "| 规则 | 类 | 命中 H/M/L | 低价值占比 | 误杀（H id） |", "|---|---|---|---|---|"]
    for name, e in sorted(res["rules"].items(), key=lambda kv: -sum(kv[1]["hits"].values())):
        lines.append(f"| {name} | {e['kind']} | {e['hits']['H']}/{e['hits']['M']}/{e['hits']['L']} | {e['share_L_pct']}% | "
                     f"{e['H_ids'][:8] if e['kind'] == 'penalty' else '—'} |")
    if res["h_dropped"]:
        lines += ["", "高价值落 drop：" + "；".join(f"{x['id']}（{x['reason']}）" for x in res["h_dropped"])]
    if res["rescued"]:
        lines += ["救回（现行闸拒、分诊放到 review）：" + "；".join(f"{x['id']}[{x['label']}]" for x in res["rescued"])]
    return "\n".join(lines)


def cmd_score(a) -> int:
    p, m, items, labels = load_bench(a.bench)
    tcfg = ot.load_triage_config(Path(a.triage_config)) if a.triage_config else ot.load_triage_config()
    res = score(p, m, items, labels, load_config(), tcfg, a.label or "")
    runs = p / "runs"
    runs.mkdir(exist_ok=True)
    out = runs / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{res['rules_version']}{('-' + a.label) if a.label else ''}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else render(res))
    print(f"\n记录：{out}")
    return 0


def cmd_shadow(a) -> int:
    rows = [r for r in read_jsonl(Path(a.src)) if "population" not in r]
    tcfg = ot.load_triage_config(Path(a.triage_config)) if a.triage_config else ot.load_triage_config()
    fcfg = load_config()
    tiers, hits = collections.Counter(), collections.Counter()
    for r, v in verdicts(rows, fcfg, tcfg):
        tiers[v.tier] += 1
        for n in v.hits:
            hits[n] += 1
    print(json.dumps({"n": len(rows), "rules_version": ot.rules_version(tcfg), "tiers": dict(tiers), "rule_hits": dict(hits)},
                     ensure_ascii=False, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--n", type=int, default=200)
    s.add_argument("--per-session", type=int, default=3)
    s.add_argument("--cut", type=int, default=0, help="正文最长字符，0 不截")
    s.add_argument("--seed", type=int, default=20261024)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_sample)
    s = sub.add_parser("sheet")
    s.add_argument("--sample", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_sheet)
    s = sub.add_parser("freeze")
    s.add_argument("--sample", required=True)
    s.add_argument("--labels", required=True)
    s.add_argument("--bench-id", required=True)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_freeze)
    s = sub.add_parser("score")
    s.add_argument("--bench", required=True)
    s.add_argument("--triage-config")
    s.add_argument("--label")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_score)
    s = sub.add_parser("shadow")
    s.add_argument("--from", dest="src", required=True)
    s.add_argument("--triage-config")
    s.set_defaults(fn=cmd_shadow)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
