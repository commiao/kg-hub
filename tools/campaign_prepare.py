"""campaign prepare：结果文件写好之后，一条命令做完正式提交前的全部准备，只把要人判断的列出来。

为什么有这一步：2026-10-24 opus2000 战役每批成本 ≈ 27.7 万 token + 1.3 千 × 工具调用数，工具调用从 c1 的 48 次
涨到 c10 的 113 次以上，多出来的几乎都是流程步骤——lint、namesync check/apply、quality-sheet → 手填 →
quality-check、整批 dry-run（常常 2 次），每步都要子代理自己读输出、写脚本比对。这里按固定顺序串起来：

  1. lint（离线）
  2. 命名同步：只写回确定性的 auto（锚点键：同仓库全路径 / 短名、本战役与图里已定的写法）；review / server 只列出
     改过名就再 lint 一次
  3. 质量表（v5）：生成模板并预填引文候选（utils/quality_candidates.py，标 candidate，不碰 verdict 与核对标志）；
     表已存在且内容没变就不覆盖；审阅者填完（有审阅人、每行有结论、候选都确认过）才跑 quality-check
  4. 整批 dry-run：每次至多一次，经 NAS 闸门（命令行传 tools.nas_gate.hold）；输入摘要没变、且 dry-run 标记
     仍覆盖当前每条结果时直接复用上次的预览，不再排闸门
  5. dry-run 之后：再 lint（新建实体 summary）、按 new_entities 判命名分裂、列误并候选

任何一步要人处理就停在那一步，报告里写「下一步该做什么」。正式提交仍是 submit，门禁一条不少。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tools import agent_extract as ax
from tools import campaign_namesync as ns
from tools import extract_campaign as ec
from utils import extraction_quality as quality
from utils import quality_candidates as qc

EXIT_READY = 0
EXIT_ACTION = 1      # 要人处理（lint、命名、质量表、dry-run 出错、新建实体 summary）
# 2 命名分裂、75/76 闸门、73/74 回执状态沿用 submit 的退出码


def sheet_path(d: Path, bid: str) -> Path:
    """审阅者填写的质量表（工作副本）；quality-check 通过后另存到 reviews/<批次>.json 供提交用。"""
    return d / "reviews" / f"{bid}.sheet.json"


def _compact_namesync(rep: dict) -> dict:
    keep = ("item", "from", "to", "to_type", "kind", "source")
    out = {"applied": [{k: a.get(k) for k in ("item", "from", "to", "kind")} for a in rep.get("applied") or []],
           "residual": [{k: a.get(k) for k in keep} for a in rep.get("residual") or []],
           "skipped": rep.get("skipped") or [],
           "review": [{k: a.get(k) for k in keep} for a in rep.get("review") or []],
           "server": len(rep.get("server") or [])}
    if "graph_hints" in rep:
        out["graph_hints"] = rep["graph_hints"]
    if "applied" not in rep and rep.get("auto"):
        out["residual"] = [{k: a.get(k) for k in keep} for a in rep["auto"]]
    return out


def _age_hours(ts: str | None) -> float | None:
    try:
        return round((datetime.now(tz=timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def _sheet_current(existing, template: dict) -> bool:
    """已有的表和当前结果对得上（每行 context_digest 一致）：审阅者的填写保留，不覆盖。"""
    if not isinstance(existing, dict) or not isinstance(existing.get("items"), list):
        return False
    old = {str(r.get("id")): r.get("context_digest") for r in existing["items"] if isinstance(r, dict)}
    new = {str(r["id"]): r["context_digest"] for r in template["items"]}
    return old == new and existing.get("identity") == template.get("identity")


def prepare_sheet(d: Path, c: dict, b: dict, path: Path) -> dict:
    """生成或沿用质量表，返回 {path, written, candidates, status}。结果改过（表过期）时旧表移到 .prev.json，
    新表沿用旧表里审阅者确认过的引文作候选（同一条、边数没变的）。"""
    bid = b["id"]
    batch, _ = ec._batch_rows(d, bid)
    results = ec._results(d, bid)
    template = quality.review_sheet(batch, results, ec.batch_rules_text(d, c, b), ec.batch_identity(c, b))
    existing = ns.load_json(path) if path.exists() else None
    out = {"path": str(path), "written": False}
    if _sheet_current(existing, template):
        sheet = existing
    else:
        previous = existing if isinstance(existing, dict) else None
        out["candidates"] = qc.add_candidates(template, batch, results, previous=previous)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            prev = path.with_name(path.name.replace(".json", "") + ".prev.json")
            path.replace(prev)
            out["previous"] = str(prev)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(template, ensure_ascii=False, indent=2))
        tmp.replace(path)
        sheet, out["written"] = template, True
    out["status"] = qc.sheet_status(sheet)
    return out


def cmd_prepare(cid: str, bid: str, *, url: str, token: str | None, post=None, gate=None, lookup=None,
                layout=None, sheet: str | None = None, fresh: bool = False) -> tuple[dict, int]:
    """返回 (报告, 退出码)。gate / post / lookup / layout 同 submit、namesync（测试注入假的；命令行给真的）。"""
    d = ec.campaign_dir(cid)
    c = ec.load(d)
    b = ec.find_batch(c, bid)
    ec.assert_worker_batch(c, b)
    if b["status"] in ("submitted", "released"):
        raise SystemExit(f"{bid} 已{'正式提交' if b['status'] == 'submitted' else '回收'}，不用再准备")
    rules = ec.batch_rules_text(d, c, b)      # 冻结规则校验不过就在这里停，什么都不写
    version = ec.episode_prompt_version(c, b["chain"])
    cli = ec.cli_command()
    again = f"{cli} prepare --id {cid} --batch {bid}"
    rep: dict = {"batch": bid, "prompt_version": version}

    def stop(stage: str, code: int, *steps: str) -> tuple[dict, int]:
        rep.update(stage=stage, ready=code == EXIT_READY, next=list(steps))
        return rep, code

    # 1–2. lint 与命名同步（只写回确定性的 auto）
    lint = ec.cmd_lint(cid, bid)
    names, _ = ec.cmd_namesync(cid, bid, apply=True, lookup=lookup, layout=layout)
    rep["namesync"] = _compact_namesync(names)
    if rep["namesync"]["applied"]:
        lint = ec.cmd_lint(cid, bid)
    rep["lint"] = lint
    blocked = []
    if lint:
        blocked.append(f"按 lint 列出的 {len(lint)} 处改结果文件")
    if rep["namesync"]["residual"]:
        blocked.append(f"namesync 有 {len(rep['namesync']['residual'])} 个没能自动改的名字（看 residual / skipped），手动改")
    if blocked:
        return stop("lint" if lint else "namesync", EXIT_ACTION, *blocked, f"改完重跑 {again}")
    if rep["namesync"]["review"]:
        rep["namesync_note"] = "review 列的是要你判断的同物异名提示（不自动改）；要改就改结果文件后重跑 prepare"

    # 3. 质量表（v5 起必须）
    path = Path(sheet).expanduser() if sheet else sheet_path(d, bid)
    if quality.requires_quality(version):
        sh = prepare_sheet(d, c, b, path)
        rep["quality_sheet"] = sh
        st = sh["status"]
        if st["error_rows"]:
            return stop("quality_sheet", EXIT_ACTION,
                        f"质量表里判 error 的 {len(st['error_rows'])} 条（{st['error_rows'][:10]}）：改结果文件，再重跑 {again}")
        if not st["complete"]:
            todo = []
            if not st["reviewer"]:
                todo.append("填 reviewer")
            if st["pending_rows"]:
                todo.append(f"{len(st['pending_rows'])} 条逐条读全文、下 verdict 与四个核对标志")
            if st["unconfirmed_candidates"]:
                todo.append(f"{st['unconfirmed_candidates']} 处预填引文逐条核对，确认后删掉 candidate 键（不对就改 quote）")
            if st["missing_quotes"]:
                todo.append(f"{st['missing_quotes']} 处空引文自己找原文填")
            return stop("quality_sheet", EXIT_ACTION, f"审阅质量表 {path}：" + "；".join(todo), f"填完重跑 {again}")
        check = ec.cmd_quality_check(cid, bid, str(path))
        rep["quality_check"] = {"ok": check["ok"], "errors": check["errors"][:30], "error_count": len(check["errors"])}
        if not check["ok"]:
            return stop("quality_check", EXIT_ACTION, f"按 quality_check.errors 改质量表 {path}（或结果文件），再重跑 {again}")

    # 4. 整批 dry-run：输入没变、标记仍覆盖当前结果就复用
    batch, _ = ec._batch_rows(d, bid)
    results = ec._results(d, bid)
    review = ns.load_json(ax.quality_file(d, bid)) if quality.requires_quality(version) else None
    digest = ec.preview_input_digest(batch, results, rules, review, ec.batch_identity(c, b))
    cached = ec.load_preview(d, bid)
    state, _ = ec.dryrun_state(d, c, b)
    if not fresh and cached and cached.get("input_sha256") == digest and state == "ok":
        dry = cached["out"]
        rep["dryrun"] = {"cached": True, "at": cached.get("at"), "age_hours": _age_hours(cached.get("at"))}
    else:
        try:
            dry = ec.cmd_submit(cid, bid, dry_run=True, url=url, token=token, post=post, gate=gate, layout=layout)
        except SystemExit as exc:
            if getattr(exc, "message", None) is None:
                raise
            rep["dryrun"] = {"refused": exc.code, "message": exc.message}
            return stop("dryrun", exc.code, "dry-run 没跑成（闸门没放行或拒绝），停下汇报；不要绕过闸门")
        rep["dryrun"] = {"cached": False}
    rep["dryrun"].update(by_status=dry["by_status"], errors=dry["errors"])
    rep["merge_candidates"] = {"reuse_mismatch": dry.get("reuse_mismatch", []),
                               "reuse_cross_type": dry.get("reuse_cross_type", []),
                               "reused": dry.get("reused", [])}
    if dry["errors"]:
        return stop("dryrun", EXIT_ACTION, f"dry-run 有 {len(dry['errors'])} 条报错（看 dryrun.errors），修好后重跑 {again}")

    # 5. dry-run 之后：新建实体 summary、命名分裂
    post_lint = ec.cmd_lint(cid, bid)
    rep["post_lint"] = post_lint
    after, _ = ec.cmd_namesync(cid, bid, apply=False, layout=layout)
    rep["splits"] = after.get("splits", [])
    if post_lint:
        return stop("post_lint", EXIT_ACTION, f"补全 post_lint 列出的新建实体 summary（≥30 字），再重跑 {again}"
                    "（结果改了，质量表相应行与 dry-run 要重做）")
    if rep["splits"]:
        return stop("splits", ec.EXIT_SPLIT,
                    f"{len(rep['splits'])} 个名字提交后会在图里分裂：改成 splits 里图中已有的写法后重跑 {again}；"
                    "确属不同事物就在 submit 时加 --allow-split \"原因\"")
    mm = rep["merge_candidates"]
    steps = []
    if mm["reuse_mismatch"] or mm["reuse_cross_type"]:
        steps.append(f"逐条看 merge_candidates：reuse_mismatch {len(mm['reuse_mismatch'])} 条、"
                     f"reuse_cross_type {len(mm['reuse_cross_type'])} 条；并错的按规则 14 改名后重跑 {again}")
    steps.append("扫一遍 merge_candidates.reused（同名也看 summary 是否同一仓库 / 同一件事）")
    steps.append(f"都没问题就正式提交：{cli} submit --id {cid} --batch {bid}（复用本次预览，不再 dry-run）")
    return stop("ready", EXIT_READY, *steps)

