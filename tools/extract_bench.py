"""抽取对比套件：换工具 / 换模型时，在同一份冻结样本上离线评分，与三模型基线逐项并排。

为什么要离线：对比用的观测已经入过主图，再提交会被幂等键挡成 skipped；`agent_extract next` 只给新观测，
难度不同、跨样本不能比密度。所以对比模式**不提交、不写图**：执行器照常读 batch、写 results，
本工具按与服务端同一套归一函数算纪律与密度，按换说法问句算可检索率，基线是同一批观测上
opus / sonnet / haiku 5.5 在 chain-v8（≈ agent-v4）下的结果（docs/three-model-comparison-20261022.md）。

样本与基线含观测正文，只放在本机 bench 目录（默认 ~/.kg-hub/bench/<bench_id>/），不进仓库。

子命令：
  freeze  --from <实验目录> --chain c8 --bench-id <id> --corpus <minilm 语料目录>
          从实验目录冻结样本：按约定版本取整批（保留原 lookahead），复制 batch、基线结果、换说法问句，
          预算「背景事实」（实验里其余观测的 fact）向量。一次性，换样本时才重做。
  parts   --bench <目录>              列出各批次文件与执行器要跑的命令。
  lint    --bench <目录> --results <文件>...   写完结果后的自检（悬空边、schema 外类型/边名、缺 edges 键、
          第一条边是否照抄标题、第二条边是否另一语言）。
  score   --bench <目录> --results <文件或目录>... --label <名字> [--model --executor --tokens --wall-s --json]
          候选与基线并排：全样本一行，再按基线模型各自那部分观测逐对比较。

可检索率口径（与报告一致）：问句由子代理只看标题写成（中文、英文各一句）；候选池 = 10-19 全图在役 fact
（minilm 语料）+ 背景事实 + 被评那一方在全部样本观测上的 fact；问句 top-10 里有该观测自己的 fact 即命中。
候选与基线各自换入自己的 fact，背景不变，所以两边面对的干扰项相同。
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOOL = "extract_bench"
DEFAULT_ROOT = Path.home() / ".kg-hub" / "bench"
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_CACHE = Path.home() / ".kg-hub" / "reembed" / "fastembed-cache"
TOP_K = 10


# ---------- 纯函数（有测试） ----------

def cjk_share(s: str) -> float:
    s = s or ""
    return sum(1 for c in s if "一" <= c <= "鿿") / max(1, len(s))


def is_cjk(s: str) -> bool:
    return cjk_share(s) >= 0.1


def item_metrics(item: dict | None, title: str, entity_types: set, edge_types: set) -> dict:
    """一条观测的结果 → 纪律、密度、链路遵从。item 为 None 表示候选没交这条。"""
    from utils.extracted_ingest import ExtractedEdge, ExtractedEntity, normalize_extraction
    m = {"present": item is not None, "nonempty": False}
    if not item or not item.get("entities"):
        return m
    m["nonempty"] = True
    raw_edges = item.get("edges")
    m["missing_edges_key"] = raw_edges is None
    raw_edges = raw_edges or []
    ents = [ExtractedEntity(name=(e.get("name") or "x")[:200], type=e.get("type") or "Concept",
                            summary=(e.get("summary") or "")[:2000]) for e in item["entities"]]
    # 缺端点或边名的边服务端在校验阶段就拒掉整条请求；这里按悬空边计，不让一条坏边拖垮整条观测的评分
    bad = [g for g in raw_edges if not (g.get("source") and g.get("target") and g.get("name"))]
    eds = [ExtractedEdge(source=g["source"][:200], target=g["target"][:200], name=g["name"][:80],
                         fact=(g.get("fact") or "-")[:1000]) for g in raw_edges if g not in bad]
    n = normalize_extraction(ents, eds, set(entity_types), set(edge_types))
    st = n.stats
    m.update(entities=len(n.entities), edges=len(n.edges), edges_in=st["edges_in"] + len(bad),
             offschema_edge=st["offschema_edge"], dangling_edge=st["dangling_edge"] + len(bad),
             unknown_type=st["unknown_type"], dirty_name=st["dirty_name"], self_edge=st["self_edge"])
    facts = [" ".join((g.get("fact") or "").split()) for g in raw_edges]
    t = " ".join((title or "").split())
    m["headline_verbatim"] = bool(t) and bool(facts) and facts[0].startswith(t)
    m["second_other_lang"] = bool(t) and len(facts) > 1 and is_cjk(facts[1]) != is_cjk(t)
    rest = facts[2:]
    m["rest_facts"] = len(rest)
    m["rest_en"] = sum(1 for f in rest if not is_cjk(f))
    m["rest_len"] = sum(len(f) for f in rest)
    return m


def aggregate(ms: list[dict]) -> dict:
    """item_metrics 的列表 → 一行。比例都以「有实体的观测」或「边」为分母。"""
    ne = [m for m in ms if m.get("nonempty")]
    k = max(1, len(ne))
    edges_in = sum(m["edges_in"] for m in ne)
    rest = sum(m["rest_facts"] for m in ne)
    return {
        "items": len(ms), "missing": sum(1 for m in ms if not m["present"]),
        "empty": sum(1 for m in ms if m["present"] and not m["nonempty"]),
        "ent_per_obs": round(sum(m["entities"] for m in ne) / k, 2),
        "edge_per_obs": round(sum(m["edges"] for m in ne) / k, 2),
        "zero_edge": sum(1 for m in ne if m["edges"] == 0),
        "missing_edges_key": sum(1 for m in ne if m["missing_edges_key"]),
        "offschema_edge_pct": round(100 * sum(m["offschema_edge"] for m in ne) / max(1, edges_in), 1),
        "dangling_edge_pct": round(100 * sum(m["dangling_edge"] for m in ne) / max(1, edges_in), 1),
        "unknown_type": sum(m["unknown_type"] for m in ne), "dirty_name": sum(m["dirty_name"] for m in ne),
        "headline_verbatim_pct": round(100 * sum(m["headline_verbatim"] for m in ne) / k, 1),
        "second_other_lang_pct": round(100 * sum(m["second_other_lang"] for m in ne) / k, 1),
        "rest_fact_en_pct": round(100 * sum(m["rest_en"] for m in ne) / max(1, rest), 1),
        "rest_fact_len": round(sum(m["rest_len"] for m in ne) / max(1, rest)),
    }


def hit_at_k(q, own, pools, k: int = TOP_K) -> bool:
    """q 是问句向量（已归一）；own 是这条观测自己的 fact 向量矩阵；pools 是其余候选矩阵的列表
    （语料、背景、被评方其他观测的 fact），不含自己的。命中 = 自己最好的那条 fact 排进前 k；没有 fact 记未命中。
    分块计数而不拼接：语料有十几万行，每条问句拼一次太慢。"""
    import numpy as np
    if own is None or len(own) == 0:
        return False
    best = float(np.max(own @ q))
    ahead = 0
    for P in pools:
        if len(P):
            ahead += int((P @ q > best).sum())
            if ahead >= k:
                return False
    return ahead < k


def load_results(paths: list[str]) -> dict[int, dict]:
    """结果文件或目录（取其中 results*.json）→ {观测 id: item}；后读到的覆盖先读到的。"""
    files = []
    for p in paths:
        pp = Path(p).expanduser()
        files += sorted(pp.glob("results*.json")) if pp.is_dir() else [pp]
    out = {}
    for f in files:
        d = json.loads(f.read_text())
        for it in d.get("items", [d] if "entities" in d else []):
            out[int(it["id"])] = it
    return out


# ---------- bench 目录 ----------

def _norm(v):
    import numpy as np
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-12)


_embedder = None


def embed(texts: list[str], cache_dir: Path | None = None):
    global _embedder
    import numpy as np
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    if _embedder is None:
        from fastembed import TextEmbedding
        cache = cache_dir or (DEFAULT_CACHE if DEFAULT_CACHE.exists() else None)
        kw = {"cache_dir": str(cache), "local_files_only": True} if cache else {}
        _embedder = TextEmbedding(EMBED_MODEL, **kw)
    return _norm(list(_embedder.embed(texts, batch_size=128)))


def load_corpus(corpus_dir: Path):
    """10-19 全图在役唯一 fact 的向量（minilm 语料目录，与 baseline_retrieval / para_eval 同一口径）。"""
    import numpy as np
    archived = set(json.loads((corpus_dir / "archived.json").read_text()))
    facts = set()
    with open(corpus_dir / "edges.jsonl", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    for r in rows:
        if r["e"] and all(e in archived for e in r["e"]):
            continue
        facts.add(r["f"])
    V = np.load(corpus_dir / "vec-minilm.npy", mmap_mode="r")
    if V.shape[0] != len(facts):
        raise SystemExit(f"语料向量 {V.shape[0]} 行与在役 fact {len(facts)} 条对不上：{corpus_dir}")
    return V


def freeze(src: Path, chain: str, bench_id: str, corpus: Path, root: Path) -> Path:
    import numpy as np
    out = root / bench_id
    if out.exists():
        raise SystemExit(f"{out} 已存在：冻结样本不覆盖，换个 bench-id")
    rounds = json.loads((src / "rounds.json").read_text())
    parts = sorted(b for b, (_, c, _) in rounds.items() if c == chain)
    if not parts:
        raise SystemExit(f"{src}/rounds.json 里没有约定版本 {chain} 的批次")
    qs = {}
    for f in sorted(glob.glob(str(src / "titles-chunk-*.q.tsv"))):
        for line in Path(f).read_text(encoding="utf-8").splitlines():
            p = line.split("\t")
            if len(p) == 3 and p[0].startswith("obs:"):
                qs[p[0]] = (p[1], p[2])
    out.mkdir(parents=True)
    bench_ids, manifest_parts = set(), []
    for b in parts:
        model, _, rnd = rounds[b]
        batch = json.loads((src / f"batch-{b}.json").read_text())
        res = json.loads((src / f"results-{b}.json").read_text())
        shutil.copy(src / f"batch-{b}.json", out / f"batch-{b}.json")
        (out / f"baseline-{b}.json").write_text(json.dumps({"model": model, "chain": chain, "round": rnd, **res},
                                                           ensure_ascii=False))
        ids = [int(i["id"]) for i in batch["items"]]
        bench_ids.update(ids)
        manifest_parts.append({"part": b, "baseline_model": model, "items": len(ids)})
    with open(out / "questions.tsv", "w", encoding="utf-8") as fh:
        n_q = 0
        for i in sorted(bench_ids):
            if f"obs:{i}" in qs:
                fh.write(f"{i}\t{qs[f'obs:{i}'][0]}\t{qs[f'obs:{i}'][1]}\n")
                n_q += 1
    bg = []
    for b in rounds:
        p = src / f"results-{b}.json"
        if not p.exists():
            continue
        for it in json.loads(p.read_text()).get("items", []):
            if int(it["id"]) in bench_ids:
                continue
            bg += [g["fact"] for g in it.get("edges") or [] if g.get("fact")]
    bg = sorted(set(bg))
    (out / "background.json").write_text(json.dumps(bg, ensure_ascii=False))
    np.save(out / "background-vec.npy", embed(bg))
    manifest = {"tool": TOOL, "bench_id": bench_id, "created_at": datetime.now(timezone.utc).isoformat(),
                "source": str(src), "chain": chain, "corpus": str(corpus), "embed_model": EMBED_MODEL,
                "items": len(bench_ids), "questions": n_q, "background_facts": len(bg), "parts": manifest_parts,
                "baseline_note": "基线 = agent-v3 + chain-v8（规则已并入 agent-v4 21–28），每批一个模型"}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    return out


def _bench(d: str) -> tuple[Path, dict]:
    p = Path(d).expanduser()
    m = json.loads((p / "manifest.json").read_text())
    if m.get("tool") != TOOL:
        raise SystemExit(f"{p} 不是 {TOOL} 的样本目录")
    return p, m


def _bench_items(p: Path, m: dict):
    """[(观测 id, 标题, 所属批次, 基线模型)]，以及 {id: 基线 item}。"""
    from utils.single_pass_extract import observation_title
    rows, base = [], {}
    for part in m["parts"]:
        b = part["part"]
        for it in json.loads((p / f"batch-{b}.json").read_text())["items"]:
            rows.append((int(it["id"]), observation_title(it["episode_body"]), b, part["baseline_model"]))
        for it in json.loads((p / f"baseline-{b}.json").read_text()).get("items", []):
            base[int(it["id"])] = it
    return rows, base


def _questions(p: Path) -> dict[int, tuple[str, str]]:
    out = {}
    for line in (p / "questions.tsv").read_text(encoding="utf-8").splitlines():
        i, zq, eq = line.split("\t")
        out[int(i)] = (zq, eq)
    return out


def retrieval(p: Path, m: dict, results: dict[int, dict], ids: list[int], cache_dir=None) -> dict[int, tuple[bool, bool]]:
    """{观测 id: (中文问句命中, 英文问句命中)}，被评方的 fact 换入候选池。"""
    import numpy as np
    qs = _questions(p)
    facts, owner = [], []
    for i in ids:
        for g in (results.get(i) or {}).get("edges") or []:
            if g.get("fact"):
                facts.append(g["fact"])
                owner.append(i)
    FV = embed(facts, cache_dir)
    owner = np.array(owner, dtype=np.int64)
    V, B = _corpus(Path(m["corpus"])), np.load(p / "background-vec.npy")
    targets = [i for i in ids if i in qs]
    QZ, QE = embed([qs[i][0] for i in targets], cache_dir), embed([qs[i][1] for i in targets], cache_dir)
    out = {}
    for j, i in enumerate(targets):
        own, others = FV[owner == i], FV[owner != i]
        out[i] = (hit_at_k(QZ[j], own, [others, B, V]), hit_at_k(QE[j], own, [others, B, V]))
    return out


_corpus_cache: dict = {}


def _corpus(d: Path):
    import numpy as np
    if d not in _corpus_cache:
        _corpus_cache[d] = np.asarray(load_corpus(d))
    return _corpus_cache[d]


def _row(label: str, ms: list[dict], hits: dict, ids: list[int], extra: dict | None = None) -> dict:
    r = {"label": label, **aggregate(ms)}
    hz = [hits[i][0] for i in ids if i in hits]
    he = [hits[i][1] for i in ids if i in hits]
    r["hit_zh_pct"] = round(100 * sum(hz) / max(1, len(hz)), 1)
    r["hit_en_pct"] = round(100 * sum(he) / max(1, len(he)), 1)
    r["questions"] = len(hz)
    r.update(extra or {})
    return r


COLS = [("items", "条数"), ("missing", "未交"), ("empty", "空"), ("ent_per_obs", "实体/条"), ("edge_per_obs", "边/条"),
        ("zero_edge", "零边"), ("missing_edges_key", "缺 edges"), ("offschema_edge_pct", "边名越界%"),
        ("dangling_edge_pct", "悬空边%"), ("unknown_type", "未知类型"), ("dirty_name", "脏命名"),
        ("headline_verbatim_pct", "照抄标题%"), ("second_other_lang_pct", "译文边%"), ("rest_fact_en_pct", "其余英文%"),
        ("rest_fact_len", "其余均长"), ("hit_zh_pct", "中文问句命中%"), ("hit_en_pct", "英文问句命中%"),
        ("tokens_per_obs", "token/条"), ("credits_per_obs", "credits/条"), ("sec_per_obs", "秒/条")]


def render(rows: list[dict]) -> str:
    head = "| 组 | " + " | ".join(c[1] for c in COLS) + " |\n|" + "---|" * (len(COLS) + 1)
    body = ["| " + r["label"] + " | " + " | ".join(str(r.get(c[0], "—")) for c in COLS) + " |" for r in rows]
    return "\n".join([head, *body])


def score(p: Path, m: dict, results: dict[int, dict], label: str, *, tokens=None, wall_s=None, cache_dir=None,
          ids=None) -> list[dict]:
    """单个候选的评分；多个候选用 score_many。"""
    n = len(ids) if ids else m["items"]
    cost = {}
    if tokens:
        cost["tokens_per_obs"] = round(tokens / n)
    if wall_s:
        cost["sec_per_obs"] = round(wall_s / n, 1)
    return score_many(p, m, [(label, results, cost)], ids=ids, cache_dir=cache_dir)


def score_many(p: Path, m: dict, cands: list[tuple[str, dict, dict]], *, ids=None, cache_dir=None) -> list[dict]:
    """cands: [(标签, 结果, 成本列)]。ids 给定时只评这些观测（冒烟子集），候选与基线用同一组。
    行序：各候选全样本 → 基线合计 → 按基线模型分组，每组里各候选 → 该模型基线。"""
    from schema import EDGE_TYPES, ENTITY_TYPES
    rows, base = _bench_items(p, m)
    if ids is not None:
        keep = set(ids)
        rows = [r for r in rows if r[0] in keep]
    ids = [r[0] for r in rows]
    titles = {r[0]: r[1] for r in rows}
    bm = {i: item_metrics(base.get(i), titles[i], ENTITY_TYPES, EDGE_TYPES) for i in ids}
    bh = retrieval(p, m, base, ids, cache_dir)
    scored = []
    for label, results, cost in cands:
        cm = {i: item_metrics(results.get(i), titles[i], ENTITY_TYPES, EDGE_TYPES) for i in ids}
        scored.append((label, cm, retrieval(p, m, results, ids, cache_dir), cost))
    out = [_row(f"{label}（全样本）", [cm[i] for i in ids], ch, ids, cost) for label, cm, ch, cost in scored]
    out.append(_row("基线：三模型各自那部分", [bm[i] for i in ids], bh, ids))
    for model in sorted({r[3] for r in rows}):
        sub = [r[0] for r in rows if r[3] == model]
        out += [_row(f"{label}｜{model} 那部分", [cm[i] for i in sub], ch, sub) for label, cm, ch, _ in scored]
        out.append(_row(f"{model}（基线）", [bm[i] for i in sub], bh, sub))
    return out


def lint(p: Path, m: dict, results: dict[int, dict], ids=None) -> list[str]:
    rows, _ = _bench_items(p, m)
    if ids is not None:
        rows = [r for r in rows if r[0] in set(ids)]
    return lint_items([(r[0], r[1], r[2]) for r in rows], results)


def lint_items(rows: list[tuple[int, str, str]], results: dict[int, dict]) -> list[str]:
    """rows: [(观测 id, 标题, 批次)]。对比样本与生产战役（tools/extract_campaign）共用这一份自检。"""
    from schema import EDGE_TYPES, ENTITY_TYPES
    probs = []
    for i, title, part in rows:
        it = results.get(i)
        if it is None:
            probs.append(f"{part} {i}: 没有结果")
            continue
        if not it.get("entities"):
            continue
        names = {e.get("name") for e in it["entities"]}
        for e in it["entities"]:
            if e.get("type") not in ENTITY_TYPES:
                probs.append(f"{part} {i}: 实体类型不在 schema 里：{e.get('name')} / {e.get('type')}")
        if "edges" not in it:
            probs.append(f"{part} {i}: 缺 edges 键")
            continue
        for g in it["edges"]:
            if g.get("source") not in names or g.get("target") not in names:
                probs.append(f"{part} {i}: 悬空边 {g.get('source')} → {g.get('target')}")
            if (g.get("name") or "").lower() not in {x.lower() for x in EDGE_TYPES}:
                probs.append(f"{part} {i}: 边名不在 schema 里：{g.get('name')}")
        mm = item_metrics(it, title, ENTITY_TYPES, EDGE_TYPES)
        if not mm["headline_verbatim"]:
            probs.append(f"{part} {i}: 第一条边没有照抄标题（规则 21）")
        if not mm["second_other_lang"]:
            probs.append(f"{part} {i}: 第二条边不是标题的另一语言译文（规则 22）")
    return probs


# ---------- 多模型运行器：会话型 CLI 只当模型通道用 ----------
# 运行器拼规则 + 观测发给 CLI、解析回复、写结果；CLI 不开任何工具。这样不同模型在同一套外壳下比，
# 差异只来自模型（工具会不会用不在比较范围里，要比工具就在工具里按说明手动跑）。

BENCH_ADDENDUM_VERSION = "bench1"
BENCH_ADDENDUM = """
## 本次调用的补充约定（离线对比，bench1）

- 本次给你若干条观测（JSON 数组），只输出**一个** JSON 对象 `{"items": [...]}`：每条观测一个 item，带原样的 `id`；
  不要 Markdown 代码围栏，不要解释文字。
- 没有 `names-<model>.tsv`、`submit --dry-run`，规则 20、28 不适用；`lookahead` 随请求给出（规则 19）。
- 每条都读全文再抽；没有可抽取知识的观测给 `"entities": []`，仍要列出它的 id。
""".strip()
PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "agent_extract_prompt.md"


def bench_system_prompt(path: Path = PROMPT_PATH) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8").strip()
    mt = re.search(r"prompt_version\s*=\s*(agent-v\d+)", text[:500])
    if not mt:
        raise SystemExit(f"{path} 首部没有 prompt_version 标记")
    return text + "\n\n" + BENCH_ADDENDUM, f"{mt.group(1)}+{BENCH_ADDENDUM_VERSION}"


def select_ids(rows: list[tuple], limit: int | None) -> list[int]:
    """冒烟子集：按基线模型轮流取，各模型那部分都覆盖到；同一个 limit 每次取到同一组。limit 为空取全部。"""
    if not limit:
        return [r[0] for r in rows]
    queues: dict[str, list[int]] = {}
    for r in rows:
        queues.setdefault(r[3], []).append(r[0])
    order, out, k = sorted(queues), [], 0
    while len(out) < limit and any(queues.values()):
        q = queues[order[k % len(order)]]
        if q:
            out.append(q.pop(0))
        k += 1
    return out


def build_query(items: list[dict], lookahead: list) -> str:
    obs = [{"id": i["id"], "name": i.get("name"), "source_description": i.get("source_description"),
            "episode_body": i["episode_body"]} for i in items]
    return ("按系统提示里的规则抽取下面每一条观测，只输出一个 JSON 对象 {\"items\": [...]}，每条带原样的 id。\n\n"
            f"lookahead（之后观测的标题，用于规则 19）：{json.dumps(lookahead, ensure_ascii=False)}\n\n"
            f"观测：{json.dumps(obs, ensure_ascii=False)}")


def parse_items(text: str, want: list[int]) -> dict[int, dict]:
    """模型回复 → {id: item}，只收 want 里的 id；容忍代码围栏与前后杂字。没有可用 JSON 抛 ValueError。"""
    t = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", (text or "").strip())
    starts = [x for x in (t.find("{"), t.find("[")) if x != -1]
    if not starts:
        raise ValueError("回复里没有 JSON")
    obj, _ = json.JSONDecoder().raw_decode(t[min(starts):])
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict) and isinstance(obj.get("items"), list):
        items = obj["items"]
    elif isinstance(obj, dict) and "entities" in obj:
        items = [obj]
    else:
        raise ValueError("JSON 里没有 items 数组")
    out, want_set = {}, set(want)
    for it in items:
        try:
            i = int(it.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if i in want_set:
            out[i] = {**it, "id": i}
    return out


def qodercli_call(model: str, system: str, query: str, *, cwd: Path, timeout: int = 900) -> dict:
    """qodercli 非交互单轮调用。--setting-sources local：不加载用户级设置——那里挂着 claude-mem 钩子，
    会把评测请求记成观测、在会话开始注入无关上下文；--tools ""：不给工具，只当模型用。"""
    import subprocess
    cmd = ["qodercli", "-p", "-m", model, "-o", "json", "--no-session-persistence", "--setting-sources", "local",
           "--tools", "", "--system-prompt", system, "--", query]
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"text": "", "error": "timeout", "stop": None, "credits": 0.0, "ms": timeout * 1000}
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return {"text": r.stdout[-2000:], "error": f"rc={r.returncode} 非 JSON 输出：{r.stderr[-300:]}",
                "stop": None, "credits": 0.0, "ms": 0}
    return {"text": d.get("result") or "", "error": d.get("is_error") and (d.get("result") or "is_error"),
            "stop": d.get("stop_reason"), "credits": float(d.get("total_credits") or 0),
            "ms": int(d.get("duration_ms") or 0)}


ADAPTERS = {"qodercli": qodercli_call}


def run_chunk(call, items: list[dict], lookahead: list, log, depth: int = 0) -> tuple[dict, dict]:
    """一组观测调一次；漏掉或解析失败的再拆半重试（最多两层），仍拿不到的记 failed。"""
    want = [i["id"] for i in items]
    res = call(build_query(items, lookahead))
    got, err = {}, res["error"]
    if not err:
        try:
            got = parse_items(res["text"], want)
        except ValueError as exc:
            err = str(exc)
    log(want, res, err)
    st = {"calls": 1, "credits": res["credits"], "ms": res["ms"], "failed": []}
    missing = [i for i in items if i["id"] not in got]
    if missing and depth < 2:
        halves = [missing[:len(missing) // 2], missing[len(missing) // 2:]] if len(missing) > 1 else [missing]
        for h in (x for x in halves if x):
            g2, s2 = run_chunk(call, h, lookahead, log, depth + 1)
            got.update(g2)
            for k in ("calls", "credits", "ms"):
                st[k] += s2[k]
            st["failed"] += s2["failed"]
    elif missing:
        st["failed"] = [i["id"] for i in missing]
    return got, st


def run_models(p: Path, m: dict, models: list[str], *, executor: str, out_root: Path, tag: str,
               limit: int | None, chunk: int, parallel: int, call_factory=None) -> list[Path]:
    """每个模型一个目录 <out_root>/<tag>-<executor>-<模型>/：results-<批次>.json、run.json、work/（原始回复）。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock
    rows, _ = _bench_items(p, m)
    ids = select_ids(rows, limit)
    keep = set(ids)
    system, version = bench_system_prompt()
    parts = {}
    for part in m["parts"]:
        b = part["part"]
        batch = json.loads((p / f"batch-{b}.json").read_text())
        its = [i for i in batch["items"] if int(i["id"]) in keep]
        if its:
            parts[b] = (its, batch.get("lookahead") or [])
    dirs, tasks, lock = {}, [], Lock()
    for model in models:
        d = out_root / f"{tag}-{executor}-{re.sub(r'[^\w.-]+', '_', model)}"
        if d.exists():
            raise SystemExit(f"{d} 已存在：换个 --tag，不覆盖已有结果")
        (d / "work").mkdir(parents=True)
        dirs[model] = d
        for b, (its, la) in parts.items():
            for k in range(0, len(its), chunk):
                tasks.append((model, b, k // chunk, its[k:k + chunk], la))
    adapter = ADAPTERS[executor]

    def make_call(model, d):
        if call_factory:
            return call_factory(model, system)
        return lambda q: adapter(model, system, q, cwd=d / "work")

    def do(task):
        model, b, k, its, la = task
        d, n = dirs[model], [0]

        def log(want, res, err):
            n[0] += 1
            (d / "work" / f"{b}.c{k}.a{n[0]}.json").write_text(json.dumps(
                {"ids": want, "error": err, "stop": res["stop"], "credits": res["credits"], "ms": res["ms"],
                 "text": res["text"]}, ensure_ascii=False))
        got, st = run_chunk(make_call(model, d), its, la, log)
        with lock:
            print(f"  {model} {b} 第 {k + 1} 组：{len(got)}/{len(its)} 条，调用 {st['calls']} 次，"
                  f"{st['credits']:.2f} credits，{st['ms'] / 1000:.0f}s", flush=True)
        return model, b, its, got, st

    started = datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        done = list(ex.map(do, tasks))
    for model, d in dirs.items():
        mine = [x for x in done if x[0] == model]
        by_part = {}
        for _, b, its, got, _ in mine:
            by_part.setdefault(b, []).extend(got[i["id"]] for i in its if i["id"] in got)
        for b, items in by_part.items():
            (d / f"results-{b}.json").write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1))
        tot = {k: sum(x[4][k] for x in mine) for k in ("calls", "credits", "ms")}
        (d / "run.json").write_text(json.dumps({
            "tool": TOOL, "label": f"{executor}-{model}", "executor": executor, "model": model, "tag": tag,
            "bench": m["bench_id"], "prompt_version": version, "ids": ids, "chunk": chunk,
            "failed": [i for x in mine for i in x[4]["failed"]], **tot,
            "started_at": started, "ended_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=1))
    return [dirs[x] for x in models]


def load_run(d: Path) -> dict:
    r = json.loads((d / "run.json").read_text())
    if r.get("tool") != TOOL:
        raise SystemExit(f"{d} 不是 {TOOL} 的运行目录")
    n = max(1, len(r["ids"]))
    r["results"] = load_results([str(d)])
    r["cost"] = {"credits_per_obs": round(r["credits"] / n, 2), "sec_per_obs": round(r["ms"] / 1000 / n, 1)}
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="抽取对比套件：冻结样本上的离线评分")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--from", dest="src", required=True)
    f.add_argument("--chain", required=True)
    f.add_argument("--bench-id", required=True)
    f.add_argument("--corpus", required=True)
    f.add_argument("--root", default=str(DEFAULT_ROOT))
    for name in ("parts", "lint", "score", "run"):
        s = sub.add_parser(name)
        s.add_argument("--bench", required=True)
        if name in ("lint", "score"):
            s.add_argument("--results", nargs="+", help="结果文件或目录（单个候选）")
            s.add_argument("--run", nargs="+", help="run 子命令产出的运行目录（可多个，合成一张表）")
        if name in ("score", "run"):
            s.add_argument("--json", help="另存 JSON")
            s.add_argument("--cache-dir", help="fastembed 模型缓存目录")
        if name == "score":
            s.add_argument("--label")
            s.add_argument("--model")
            s.add_argument("--executor")
            s.add_argument("--tokens", type=int, help="执行器自报的总 token（输入 + 输出）")
            s.add_argument("--wall-s", type=float, help="执行器总耗时（秒）")
        if name == "run":
            s.add_argument("--executor", default="qodercli", choices=sorted(ADAPTERS))
            s.add_argument("--models", nargs="+", required=True)
            s.add_argument("--tag", required=True, help="运行标签，如 smoke10 / full")
            s.add_argument("--limit", type=int, help="冒烟：只取这么多条（按基线模型轮流取）")
            s.add_argument("--chunk", type=int, default=10, help="每次调用的观测数")
            s.add_argument("--parallel", type=int, default=3, help="同时在跑的调用数")
            s.add_argument("--out-root", default=str(DEFAULT_ROOT / "runs"))
    a = ap.parse_args(argv)
    if a.cmd == "freeze":
        out = freeze(Path(a.src).expanduser(), a.chain, a.bench_id, Path(a.corpus).expanduser(),
                     Path(a.root).expanduser())
        print(json.dumps(json.loads((out / "manifest.json").read_text()), ensure_ascii=False, indent=1))
        return 0
    p, m = _bench(a.bench)
    cache = Path(a.cache_dir) if getattr(a, "cache_dir", None) else None
    if a.cmd == "parts":
        print(f"样本 {m['bench_id']}：{m['items']} 条，{len(m['parts'])} 批（基线约定 {m['chain']}）")
        for part in m["parts"]:
            print(f"  {p / ('batch-' + part['part'] + '.json')}  {part['items']} 条  基线 {part['baseline_model']}")
        print("每批写一个 results-<批次>.json（格式同 agent_extract），全部写完后：")
        print(f"  python -m tools.extract_bench lint  --bench {p} --results <结果目录>")
        print(f"  python -m tools.extract_bench score --bench {p} --results <结果目录> --label <工具-模型>")
        print("或一次跑多个模型：python -m tools.extract_bench run --bench <样本> --models <模型...> --tag <标签> [--limit 10]")
        return 0
    if a.cmd == "run":
        dirs = run_models(p, m, a.models, executor=a.executor, out_root=Path(a.out_root).expanduser(), tag=a.tag,
                          limit=a.limit, chunk=a.chunk, parallel=a.parallel)
        runs = [load_run(d) for d in dirs]
    elif a.run:
        runs = [load_run(Path(d).expanduser()) for d in a.run]
    else:
        runs = None
        if not a.results:
            raise SystemExit("要给 --results 或 --run")
    if a.cmd == "lint" or a.cmd == "run":
        bad = 0
        for r in runs or [{"label": "结果", "results": load_results(a.results), "ids": None}]:
            probs = lint(p, m, r["results"], ids=r["ids"])
            bad += len(probs)
            print(f"[{r['label']}] " + (f"{len(probs)} 处问题" if probs else "自检通过"))
            for x in probs[:40]:
                print("  " + x)
        if a.cmd == "lint":
            return 1 if bad else 0
    if runs:
        ids = sorted(set.intersection(*(set(r["ids"]) for r in runs)))
        rows = score_many(p, m, [(r["label"], r["results"], r["cost"]) for r in runs], ids=ids, cache_dir=cache)
        print(f"样本 {m['bench_id']}：比较 {len(ids)} 条（问句口径同报告）；"
              + "；".join(f"{r['label']} 调用 {r['calls']} 次、失败 {len(r['failed'])} 条" for r in runs))
        meta = {"runs": [{k: r[k] for k in ("label", "model", "executor", "tag", "prompt_version", "calls",
                                            "credits", "ms", "failed")} for r in runs]}
    else:
        if not a.label:
            raise SystemExit("单个候选要给 --label")
        rows = score(p, m, load_results(a.results), a.label, tokens=a.tokens, wall_s=a.wall_s, cache_dir=cache)
        print(f"样本 {m['bench_id']}（{m['items']} 条，问句 {m['questions']} 条）；被评：{a.label}"
              + (f"，模型 {a.model}" if a.model else "") + (f"，执行器 {a.executor}" if a.executor else ""))
        meta = {"label": a.label, "model": a.model, "executor": a.executor}
    print(render(rows))
    if a.json:
        Path(a.json).expanduser().write_text(json.dumps({"bench": m["bench_id"], **meta, "rows": rows},
                                                        ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
