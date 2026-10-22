"""抽取对比战役：定额 × 多模型 × 各抽不同的新批次 × 全部入库 × 每批复盘。与执行工具无关。

10-22 三模型 2000 条实验（docs/three-model-comparison-20261022.md）的做法沉淀成可复用流程：
一个编排者（Claude Code / qoder / codex / cursor 的主会话）给每个模型派一个子代理，或每个模型开一个会话；
子代理只做「领批 → 读观测抽取 → 自检 → dry-run → 正式入库 → 记成本」，编排者每轮看复盘表、改链路。
花的 token 全部变成图谱内容；不同模型抽的是不同观测，比较靠来源标记分组与每批指标（与 10-22 同一口径）。

为什么要「战役」这一层：多个子代理或会话同时跑时，各自用 `agent_extract next` 会取到同一批，
后提交的那份被幂等键挡成 skipped，等于白抽。这里领批在文件锁里推进同一个游标，谁领到的就只属于谁。

链路优化：编排者把本轮复盘得出的新约定写成补充文件，`chain` 登记成新版本；之后领的批次都带这个版本，
`rules` 输出 agent-v4 正文 + 当前补充约定，复盘表按模型 × 约定版本分组。

状态目录：$KG_HUB_AGENT_EXTRACT_DIR/campaigns/<战役 id>/（默认 ~/.kg-hub/agent-extract/），
含 campaign.json、batches/、results/（子代理写这里）、log.jsonl、dry-run 标记、chain-*.md。观测正文不进仓库。

子命令（python -m tools.extract_campaign <cmd> --id <战役>）：
  start   --models <模型 id>... --executor <工具> [--quota 200] [--batch 50] [--after-id N]
  chain   --file <补充约定.md>        登记新的补充约定版本（之后领的批次生效）
  rules                              打印抽取规则：agent-v4 正文 + 当前补充约定（子代理第一步读它）
  brief   --model <模型 id>           打印填好的子代理说明（粘给子代理，或由 agents 写进子代理定义）
  agents  [--format qodercli|md] [--dir 目录]   按战役里的模型生成子代理定义，每个模型一个、带指定模型：
          qodercli 格式给 `qodercli --agents '<json>'`；md 格式写成带 frontmatter 的文件（qoder 的 agents 目录）
  claim   --model <模型 id>           领下一批（加锁、按配额），打印批次文件、结果文件路径与后续命令
  lint    --batch <批次 id>           自检结果文件（悬空边、schema、缺 edges、照抄标题、译文边），不过不许提交
  submit  --batch <批次 id> [--dry-run]   先 dry-run 看误并候选，再正式入库（模型、run_id、执行器从战役取）
  cost    --batch <批次 id> [--credits N] [--tokens N] [--seconds N]   记执行器自报成本
  status                             各模型配额进度与每批状态
  report  [--json 路径]               复盘表：每批一行 + 每个模型 × 约定版本合计
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import agent_extract as ax  # noqa: E402

TOOL = "extract_campaign"
CHAIN_NONE = "c0"


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def slug(model: str) -> str:
    return re.sub(r"[^\w.-]+", "_", model)


def campaign_dir(cid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", cid):
        raise SystemExit(f"战役 id 只能用字母数字 . _ -：{cid!r}")
    return ax.state_dir() / "campaigns" / cid


@contextlib.contextmanager
def locked(d: Path):
    """战役状态的读改写都在这把锁里：多个子代理 / 会话同时领批、提交、记成本不会互相覆盖。"""
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load(d: Path) -> dict:
    p = d / "campaign.json"
    if not p.exists():
        raise SystemExit(f"没有这个战役：{d}（先 start）")
    c = json.loads(p.read_text())
    if c.get("tool") != TOOL:
        raise SystemExit(f"{p} 不是 {TOOL} 的状态文件")
    return c


def save(d: Path, c: dict) -> None:
    tmp = d / "campaign.json.tmp"
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=1))
    tmp.replace(d / "campaign.json")


# ---------- 纯函数（有测试） ----------

def new_campaign(cid: str, models: list[str], *, executor: str, quota: int, batch: int, after_id: int) -> dict:
    if len(set(models)) != len(models):
        raise SystemExit("模型重复")
    for m in models + [executor]:
        if not ax_pattern().fullmatch(m):
            raise SystemExit(f"{m!r} 不能作来源标记（只允许字母数字 . _ : / -，80 字以内）")
    return {"tool": TOOL, "id": cid, "created_at": _now(), "executor": executor, "quota": quota, "batch": batch,
            "prompt_version": ax.PROMPT_VERSION, "cursor": after_id, "chain": CHAIN_NONE, "chains": {},
            "models": {m: {"run_id": f"{cid}-{slug(m)}"} for m in models}, "batches": []}


def ax_pattern():
    from utils.extracted_ingest import EXTRACTOR_MODEL_PATTERN
    return EXTRACTOR_MODEL_PATTERN


def claimed_items(c: dict, model: str) -> int:
    return sum(len(b["ids"]) for b in c["batches"] if b["model"] == model and b["status"] != "released")


def next_batch_id(c: dict, model: str) -> str:
    n = sum(1 for b in c["batches"] if b["model"] == model) + 1
    return f"{slug(model)}-{n:03d}"


def find_batch(c: dict, bid: str) -> dict:
    for b in c["batches"]:
        if b["id"] == bid:
            return b
    raise SystemExit(f"战役 {c['id']} 里没有批次 {bid}")


def rules_text(d: Path, c: dict) -> str:
    text = ax.PROMPT_PATH.read_text().rstrip()
    if c["chain"] != CHAIN_NONE:
        text += "\n\n" + (d / c["chains"][c["chain"]]["file"]).read_text().rstrip()
    return text + "\n"


SUBAGENT_BRIEF = """你是 kg-hub 抽取对比战役的执行者，战役 {cid}，你的模型 id 是 {model}。
在仓库根目录执行命令，解释器用 $HOME/.local/share/kg-hub/venv/bin/python（下称 PY）。

1. 读规则：$PY -m tools.extract_campaign rules --id {cid}（agent-v4 正文 + 战役补充约定），尤其末尾规则 21–28。
2. 领一批：$PY -m tools.extract_campaign claim --id {cid} --model {model}
   返回 batch_file（要抽的观测）与 results_file（你要写的结果）。返回 done 就结束并汇报。
3. 抽取：每次读 batch_file 里 10 条，**读全文再抽**；10 条一组写分片（放在 results_file 同目录、文件名带批次号），
   全部写完用 python 合并成 results_file：{{"items": [{{"id", "entities": [{{"name","type","summary"}}],
   "edges": [{{"source","name","target","fact"}}]}}]}}。没有知识的观测给 "entities": []。
4. 自检：$PY -m tools.extract_campaign lint --id {cid} --batch <批次>，改到「自检通过」。
5. 预览：$PY -m tools.extract_campaign submit --id {cid} --batch <批次> --dry-run
   逐条看 reuse_mismatch（抽出名 → 会并到的旧名）：并错的（不同的东西、更泛的旧名）按规则 14 改名，再自检、再预览；
   标「（同批新建）」的是本批前面条目会新建的实体，判法相同。
6. 入库：$PY -m tools.extract_campaign submit --id {cid} --batch <批次>（逐条写图，可能超过 2 分钟，给足超时）。
7. 汇报（只输出这个 JSON）：
   {{"batch": "<批次>", "model": "{model}", "submitted": {{"ok": 0, "empty": 0, "error": 0}},
    "dryrun_mismatch_seen": 0, "renamed_after_dryrun": 0,
    "lessons": ["最多 4 条：规则哪里不好用、哪类观测难抽、你做了什么取舍"]}}

红线：不碰断路器、不直连 FalkorDB、不改仓库文件、不 git 提交、不读或打印 .env；提交报错只重试一次，仍失败就停下汇报。"""


def brief(c: dict, model: str) -> str:
    if model not in c["models"]:
        raise SystemExit(f"战役 {c['id']} 没有模型 {model}；有：{', '.join(c['models'])}")
    return SUBAGENT_BRIEF.format(cid=c["id"], model=model)


def agent_defs(c: dict) -> dict[str, dict]:
    """每个模型一个子代理定义，带指定模型（qoder 已验证：qodercli --agents 的 model 字段生效，
    modelUsage 里按模型分开记 credits；2026-10-22 用 DeepSeek-Flash / Kimi-K3 两次对照确认）。"""
    return {f"kg-extract-{slug(m).lower()}": {
        "description": f"kg-hub 抽取对比战役 {c['id']} 的执行者（模型 {m}）：领批、抽取、自检、dry-run、入库、汇报",
        "prompt": brief(c, m), "model": m} for m in c["models"]}


def agent_markdown(name: str, d: dict) -> str:
    return (f"---\nname: {name}\ndescription: {d['description']}\nmodel: {d['model']}\n"
            f"permissionMode: default\n---\n\n{d['prompt']}\n")


# ---------- 子命令 ----------

def cmd_start(cid: str, models: list[str], *, executor: str, quota: int, batch: int, after_id: int | None) -> dict:
    d = campaign_dir(cid)
    with locked(d):
        if (d / "campaign.json").exists():
            raise SystemExit(f"战役 {cid} 已存在")
        if after_id is None:   # 默认从会话自抽取的游标接着取，不回头扫已经看过的
            st = ax._load_state(ax.state_dir() / "state.json")
            after_id = int(st.get("cursor", 0))
        c = new_campaign(cid, models, executor=executor, quota=quota, batch=batch, after_id=after_id)
        for sub in ("batches", "results"):
            (d / sub).mkdir(exist_ok=True)
        save(d, c)
    return c


def cmd_chain(cid: str, file: str) -> dict:
    d = campaign_dir(cid)
    src = Path(file).expanduser()
    with locked(d):
        c = load(d)
        ver = f"c{len(c['chains']) + 1}"
        dst = d / f"chain-{ver}.md"
        dst.write_text(src.read_text())
        c["chains"][ver] = {"file": dst.name, "registered_at": _now(), "source": str(src)}
        c["chain"] = ver
        save(d, c)
    return {"chain": ver, "file": str(dst)}


def cmd_claim(cid: str, model: str, *, url: str, token: str | None, fetch=None, check=None) -> dict:
    d = campaign_dir(cid)
    fetch = fetch or ax.fetch_remote_pending
    check = check or (lambda idents: ax.check_existing(idents, url, token))
    with locked(d):
        c = load(d)
        if model not in c["models"]:
            raise SystemExit(f"战役 {cid} 没有模型 {model}；有：{', '.join(c['models'])}")
        left = c["quota"] - claimed_items(c, model)
        if left <= 0:
            return {"done": True, "model": model, "message": f"{model} 已领满配额 {c['quota']} 条"}
        n = min(c["batch"], left)
        remote = fetch(c["cursor"], max(n * 4, 40))
        rows = remote["rows"]
        if not rows:
            return {"done": True, "model": model, "message": "游标之后没有待处理观测了"}
        picked, stats = ax.select_batch(rows, ax.load_config(), n, check([ax.to_identity(r) for r in rows]))
        last = picked[-1]["id"] if len(picked) >= n else rows[-1]["id"]
        if not picked:   # 这一段全被质量闸拒或已入库：游标前移，让调用方再领一次
            c["cursor"] = last
            save(d, c)
            return {"done": False, "batch": None, "model": model, "message": "这一段没有可抽的观测，游标已前移，再领一次",
                    "stats": stats}
        lookahead = [{"id": r["id"], "type": r.get("type"), "title": (r.get("title") or "")[:120]}
                     for r in rows if r["id"] > last][:n]
        bid = next_batch_id(c, model)
        bpath, rpath = d / "batches" / f"{bid}.json", d / "results" / f"{bid}.json"
        bpath.write_text(json.dumps({"batch_id": bid, "created_at": _now(), "campaign": cid, "model": model,
                                     "chain": c["chain"], "prompt_version": c["prompt_version"],
                                     "after_id": c["cursor"], "last_considered_id": last, "stats": stats,
                                     "items": picked, "lookahead": lookahead}, ensure_ascii=False, indent=1))
        c["cursor"] = last
        c["batches"].append({"id": bid, "model": model, "chain": c["chain"], "ids": [i["id"] for i in picked],
                             "status": "claimed", "claimed_at": _now(), "cost": {}})
        save(d, c)
    run_id = c["models"][model]["run_id"]
    return {"done": False, "batch": bid, "model": model, "run_id": run_id, "chain": c["chain"],
            "items": len(picked), "batch_file": str(bpath), "results_file": str(rpath),
            "next": [f"python -m tools.extract_campaign lint   --id {cid} --batch {bid}",
                     f"python -m tools.extract_campaign submit --id {cid} --batch {bid} --dry-run",
                     f"python -m tools.extract_campaign submit --id {cid} --batch {bid}",
                     f"python -m tools.extract_campaign cost   --id {cid} --batch {bid} --credits <N> --seconds <N>"]}


def _batch_rows(d: Path, bid: str) -> tuple[dict, list[tuple[int, str, str]]]:
    from utils.single_pass_extract import observation_title
    batch = json.loads((d / "batches" / f"{bid}.json").read_text())
    return batch, [(int(i["id"]), observation_title(i["episode_body"]), bid) for i in batch["items"]]


def _results(d: Path, bid: str) -> dict:
    p = d / "results" / f"{bid}.json"
    if not p.exists():
        raise SystemExit(f"还没有结果文件：{p}")
    return json.loads(p.read_text())


def cmd_lint(cid: str, bid: str) -> list[str]:
    from tools.extract_bench import lint_items
    d = campaign_dir(cid)
    _, rows = _batch_rows(d, bid)
    res = _results(d, bid)
    by_id = {int(it["id"]): it for it in res.get("items", [])}
    return lint_items(rows, by_id)


def cmd_submit(cid: str, bid: str, *, dry_run: bool, url: str, token: str | None, post=None) -> dict:
    d = campaign_dir(cid)
    probs = cmd_lint(cid, bid)
    if probs:
        raise SystemExit(f"自检没过（{len(probs)} 处），先 lint 修好：\n" + "\n".join(probs[:20]))
    with locked(d):
        c = load(d)
        b = find_batch(c, bid)
    if b["status"] == "submitted":
        raise SystemExit(f"{bid} 已正式提交过")
    batch, results = json.loads((d / "batches" / f"{bid}.json").read_text()), _results(d, bid)
    if not dry_run:
        missing = ax.dryrun_missing(d, bid, results)
        if missing:
            raise SystemExit(f"{bid} 有 {len(missing)} 条还没 dry-run 过（id {missing[:10]}），先 --dry-run")
    model, run_id = b["model"], c["models"][b["model"]]["run_id"]
    recs = ax.submit_results(batch, results, model=model, session=cid, url=url, token=token,
                             post=post or ax._http, log_path=None if dry_run else d / "log.jsonl",
                             dry_run=dry_run, executor=c["executor"], run_id=run_id)
    counts: dict[str, int] = {}
    for r in recs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    mismatch = [m for r in recs for m in (r.get("reuse_mismatch") or [])]
    with locked(d):
        c = load(d)
        b = find_batch(c, bid)
        if dry_run:
            ax.record_dryrun(d, bid, recs)
            b.update(status="dry_run" if b["status"] == "claimed" else b["status"], dry_run_at=_now(),
                     dry_run_mismatch=len(mismatch))
        else:
            b.update(status="submitted", submitted_at=_now(), counts=counts, mismatch=len(mismatch))
        save(d, c)
    return {"batch": bid, "dry_run": dry_run, "by_status": counts, "reuse_mismatch": mismatch,
            "errors": [{"id": r["id"], "error": r.get("error")} for r in recs if r.get("error")]}


def cmd_cost(cid: str, bid: str, *, credits=None, tokens=None, seconds=None) -> dict:
    d = campaign_dir(cid)
    with locked(d):
        c = load(d)
        b = find_batch(c, bid)
        for k, v in (("credits", credits), ("tokens", tokens), ("seconds", seconds)):
            if v is not None:
                b["cost"][k] = v
        save(d, c)
    return {"batch": bid, "cost": b["cost"]}


def batch_row(d: Path, b: dict) -> dict:
    """一批的复盘行：提交口径（log.jsonl）+ 离线指标（结果文件，与对比套件同一套 item_metrics）+ 自报成本。"""
    from schema import EDGE_TYPES, ENTITY_TYPES
    from tools.extract_bench import aggregate, item_metrics
    _, rows = _batch_rows(d, b["id"])
    rp = d / "results" / f"{b['id']}.json"
    res = {int(it["id"]): it for it in json.loads(rp.read_text()).get("items", [])} if rp.exists() else {}
    r = {"label": f"{b['id']}（{b['chain']}）", "model": b["model"], "chain": b["chain"], "status": b["status"],
         **aggregate([item_metrics(res.get(i), t, ENTITY_TYPES, EDGE_TYPES) for i, t, _ in rows])}
    counts = b.get("counts") or {}
    # 预览误并候选（dry-run，执行者据此改名）与入库时实际发生的不同名复用分两列
    r.update(ok=counts.get("ok", 0), error=sum(v for k, v in counts.items() if k not in ("ok", "empty")),
             dry_mismatch=b.get("dry_run_mismatch"), mismatch=b.get("mismatch"))
    n = max(1, len(rows))
    cost = b.get("cost") or {}
    if cost.get("credits") is not None:
        r["credits_per_obs"] = round(cost["credits"] / n, 2)
    if cost.get("tokens") is not None:
        r["tokens_per_obs"] = round(cost["tokens"] / n)
    if cost.get("seconds") is not None:
        r["sec_per_obs"] = round(cost["seconds"] / n, 1)
    r["_n"], r["_cost"] = len(rows), cost
    return r


def merge_rows(label: str, rows: list[dict]) -> dict:
    """若干批的行按观测数加权合并（比例列加权平均，计数列相加）。"""
    n = sum(r["_n"] for r in rows) or 1
    out = {"label": label, "items": sum(r["items"] for r in rows)}
    for k in ("missing", "empty", "zero_edge", "missing_edges_key", "unknown_type", "dirty_name", "ok", "error"):
        out[k] = sum(r.get(k) or 0 for r in rows)
    out["dry_mismatch"] = sum(r.get("dry_mismatch") or 0 for r in rows)
    out["mismatch"] = sum(r.get("mismatch") or 0 for r in rows)
    for k in ("ent_per_obs", "edge_per_obs", "offschema_edge_pct", "dangling_edge_pct", "headline_verbatim_pct",
              "second_other_lang_pct", "rest_fact_en_pct", "rest_fact_len"):
        out[k] = round(sum((r.get(k) or 0) * r["_n"] for r in rows) / n, 2)
    for k, col, nd in (("credits", "credits_per_obs", 2), ("tokens", "tokens_per_obs", 0), ("seconds", "sec_per_obs", 1)):
        have = [r for r in rows if r["_cost"].get(k) is not None]
        if have:
            out[col] = round(sum(r["_cost"][k] for r in have) / max(1, sum(r["_n"] for r in have)), nd)
    return out


REPORT_COLS = [("items", "条数"), ("ok", "入库"), ("empty", "空"), ("error", "错误"), ("dry_mismatch", "预览误并候选"),
               ("mismatch", "入库不同名复用"),
               ("ent_per_obs", "实体/条"), ("edge_per_obs", "边/条"), ("zero_edge", "零边"),
               ("offschema_edge_pct", "边名越界%"), ("dangling_edge_pct", "悬空边%"), ("unknown_type", "未知类型"),
               ("dirty_name", "脏命名"), ("headline_verbatim_pct", "照抄标题%"), ("second_other_lang_pct", "译文边%"),
               ("rest_fact_en_pct", "其余英文%"), ("rest_fact_len", "其余均长"), ("credits_per_obs", "credits/条"),
               ("tokens_per_obs", "token/条"), ("sec_per_obs", "秒/条")]


def render(rows: list[dict]) -> str:
    head = "| 组 | " + " | ".join(c[1] for c in REPORT_COLS) + " |\n|" + "---|" * (len(REPORT_COLS) + 1)
    body = ["| " + r["label"] + " | " + " | ".join(str(r.get(k, "—")) for k, _ in REPORT_COLS) + " |" for r in rows]
    return "\n".join([head, *body])


def cmd_report(cid: str) -> dict:
    d = campaign_dir(cid)
    c = load(d)
    per = [batch_row(d, b) for b in c["batches"] if (d / "batches" / f"{b['id']}.json").exists()]
    groups = {}
    for r in per:
        groups.setdefault((r["model"], r["chain"]), []).append(r)
    totals = [merge_rows(f"{m} × {ch}（{len(rs)} 批）", rs) for (m, ch), rs in sorted(groups.items())]
    by_model = {}
    for r in per:
        by_model.setdefault(r["model"], []).append(r)
    models = [merge_rows(f"{m}（合计 {len(rs)} 批）", rs) for m, rs in sorted(by_model.items())]
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in per]
    return {"campaign": cid, "models": models, "by_chain": totals, "batches": clean}


def cmd_status(cid: str) -> dict:
    d = campaign_dir(cid)
    c = load(d)
    return {"campaign": cid, "executor": c["executor"], "chain": c["chain"], "quota": c["quota"],
            "batch": c["batch"], "prompt_version": c["prompt_version"],
            "models": {m: {"run_id": v["run_id"], "claimed": claimed_items(c, m),
                           "submitted": sum(len(b["ids"]) for b in c["batches"]
                                            if b["model"] == m and b["status"] == "submitted")}
                       for m, v in c["models"].items()},
            "batches": [{k: b.get(k) for k in ("id", "model", "chain", "status", "counts", "mismatch", "cost")}
                        | {"items": len(b["ids"])} for b in c["batches"]]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="抽取对比战役：定额多模型、各抽不同新批次、全部入库、每批复盘")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "chain", "rules", "brief", "agents", "claim", "lint", "submit", "cost", "status", "report"):
        s = sub.add_parser(name)
        s.add_argument("--id", required=True)
        if name == "start":
            s.add_argument("--models", nargs="+", required=True)
            s.add_argument("--executor", required=True, help="执行工具：claude-code / qoder / codex / cursor ...")
            s.add_argument("--quota", type=int, default=200, help="每个模型抽多少条")
            s.add_argument("--batch", type=int, default=50, help="每批条数（每批复盘一次）")
            s.add_argument("--after-id", type=int, help="游标起点；缺省接着会话自抽取的游标")
        if name == "chain":
            s.add_argument("--file", required=True)
        if name in ("claim", "brief"):
            s.add_argument("--model", required=True)
        if name == "agents":
            s.add_argument("--format", choices=("qodercli", "md"), default="qodercli")
            s.add_argument("--dir", help="md 格式写到这个目录（每个模型一个文件）；不给就打印")
        if name in ("lint", "submit", "cost"):
            s.add_argument("--batch", required=True)
        if name == "submit":
            s.add_argument("--dry-run", action="store_true")
        if name == "cost":
            s.add_argument("--credits", type=float)
            s.add_argument("--tokens", type=int)
            s.add_argument("--seconds", type=float)
        if name == "report":
            s.add_argument("--json")
        if name in ("claim", "submit"):
            s.add_argument("--url", default=os.environ.get("KG_HUB_URL", ax.DEFAULT_URL))
    a = ap.parse_args(argv)
    if a.cmd == "start":
        out = cmd_start(a.id, a.models, executor=a.executor, quota=a.quota, batch=a.batch, after_id=a.after_id)
    elif a.cmd == "chain":
        out = cmd_chain(a.id, a.file)
    elif a.cmd == "rules":
        d = campaign_dir(a.id)
        sys.stdout.write(rules_text(d, load(d)))
        return 0
    elif a.cmd == "brief":
        d = campaign_dir(a.id)
        print(brief(load(d), a.model))
        return 0
    elif a.cmd == "agents":
        d = campaign_dir(a.id)
        defs = agent_defs(load(d))
        if a.format == "qodercli":
            print(json.dumps(defs, ensure_ascii=False))
            return 0
        if not a.dir:
            print("\n\n".join(agent_markdown(n, v) for n, v in defs.items()))
            return 0
        out_dir = Path(a.dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        for n, v in defs.items():
            (out_dir / f"{n}.md").write_text(agent_markdown(n, v))
        out = {"written": sorted(str(out_dir / f"{n}.md") for n in defs)}
    elif a.cmd == "claim":
        out = cmd_claim(a.id, a.model, url=a.url, token=ax.api_token())
    elif a.cmd == "lint":
        probs = cmd_lint(a.id, a.batch)
        print("\n".join(probs) if probs else "自检通过")
        return 1 if probs else 0
    elif a.cmd == "submit":
        out = cmd_submit(a.id, a.batch, dry_run=a.dry_run, url=a.url, token=ax.api_token())
    elif a.cmd == "cost":
        out = cmd_cost(a.id, a.batch, credits=a.credits, tokens=a.tokens, seconds=a.seconds)
    elif a.cmd == "status":
        out = cmd_status(a.id)
    else:
        rep = cmd_report(a.id)
        print("### 按模型\n\n" + render(rep["models"]) + "\n\n### 按模型 × 约定版本\n\n" + render(rep["by_chain"])
              + "\n\n### 每批\n\n" + render(rep["batches"]))
        if a.json:
            Path(a.json).expanduser().write_text(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
