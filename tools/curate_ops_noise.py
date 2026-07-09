"""G3 精确清理 —— 治理噪音的 dry-run / apply / restore（只清 0.4 层②，绝不碰操作日志）。

见 docs/LANDING-PLAN-cognitive-asset.md（0.4 三层定义 · G3）。

三类候选**分开列**（不混成黑盒数字）：
  A. ops_noise    ：kg-hub 自身运维自指 bugfix —— **复用 utils.ops_noise.is_ops_noise，不另写规则**
  B. dup          ：同 (project, 标题首行) 的近重复 episode 的**多余份**（每簇保留内容最长的一份）
  C. incident_retro：canonical 胶囊 kg-hub-canonical-INCIDENT-RETRO（若存在）

隔离方式 = 打属性 `archived=true`（+ `archived_at`），**不物理删除**（0.4 铁律：软处理）。
⚠️ `archived` 只是标记；要让 episode_search / dashboard / canonical_context 默认不显示归档项，
   **apply 前必须**给这些读路径加 `WHERE NOT coalesce(n.archived,false)`（server 改动+rebuild=生产 gate）。
   故本工具的 --apply 前置检查会提醒；dry-run 无此依赖。

模式：
  --dry-run            默认。列三类候选 + 重叠，不写图。
  --apply --manifest P 打 archived=true 并写 manifest（需先备份 + 读路径已就绪，属生产 gate）。
  --restore P          按 manifest 逐条清除 archived。
纯 NAS 上跑（FalkorDB 仅 NAS-local）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.ops_noise import is_ops_noise  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "ingest_filter.json"
CANON = "kg-hub-canonical"
INCIDENT_RETRO = "kg-hub-canonical-INCIDENT-RETRO"
_SD_TYPE = re.compile(r"type=(\S+)")


def _driver():
    from graphiti_core.driver.falkordb_driver import FalkorDriver  # type: ignore
    return FalkorDriver(
        host=os.environ.get("KG_HUB_FALKORDB_HOST", "falkordb"), port=6379,
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
        database=os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"),
    )


async def _q(d, cy, **p):
    rows, _, _ = await d.execute_query(cy, **p)
    return rows


def _cfg():
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {"ops_noise": {}}


async def _candidates(d, cfg) -> dict:
    # 非 canonical episode（含 archived 标记，便于跳过已归档）
    rows = await _q(
        d,
        "MATCH (n:Episodic) WHERE NOT n.name STARTS WITH $c "
        "RETURN n.name AS name, n.content AS content, n.source_description AS sd, "
        "       coalesce(n.archived,false) AS archived, n.created_at AS created_at",
        c=CANON,
    )
    ops, non_archived = [], []
    dup_map = defaultdict(list)
    for r in rows:
        if r.get("archived"):
            continue  # 已归档的不再作候选
        name = r.get("name") or ""
        content = r.get("content") or ""
        sd = r.get("sd") or ""
        typ = (_SD_TYPE.search(sd).group(1) if _SD_TYPE.search(sd) else "?")
        obs = {"type": typ, "narrative": content, "title": "", "facts": ""}
        rec = {"name": name, "type": typ, "clen": len(content),
               "created_at": r.get("created_at"),
               "title": content.splitlines()[0][:72] if content else ""}
        non_archived.append(rec)
        if is_ops_noise(obs, cfg):
            ops.append(rec)
        # dup key：project(从 sd) + 标题首行
        proj = ""
        mp = re.search(r"project=(.+?)\s+type=", sd)
        if mp:
            proj = mp.group(1)
        dup_map[(proj, rec["title"])].append(rec)

    # dup 多余份：每簇保留 clen 最长一份，其余为候选
    dup_extras, dup_clusters = [], []
    for (proj, title), members in dup_map.items():
        if len(members) > 1 and title:
            keep = max(members, key=lambda m: m["clen"])
            extras = [m for m in members if m is not keep]
            dup_clusters.append({"project": proj, "title": title,
                                 "keep": keep["name"], "archive": [m["name"] for m in extras]})
            dup_extras.extend(extras)

    # INCIDENT-RETRO canonical
    ir = await _q(
        d,
        "MATCH (n:Episodic) WHERE n.name = $n RETURN n.name AS name, "
        "coalesce(n.archived,false) AS archived",
        n=INCIDENT_RETRO,
    )
    incident = [x.get("name") for x in ir if not x.get("archived")]

    return {"ops": ops, "dup_extras": dup_extras, "dup_clusters": dup_clusters,
            "incident": incident, "n_active_noncanon": len(non_archived)}


def _report(c: dict) -> dict:
    ops_names = {r["name"] for r in c["ops"]}
    dup_names = {r["name"] for r in c["dup_extras"]}
    overlap = ops_names & dup_names           # 既是 ops_noise 又是 dup 多余份
    union = ops_names | dup_names | set(c["incident"])
    return {
        "A_ops_noise": sorted(ops_names),
        "B_dup_extras": sorted(dup_names),
        "B_dup_clusters": c["dup_clusters"],
        "C_incident_retro": c["incident"],
        "overlap_ops_and_dup": sorted(overlap),
        "counts": {
            "ops_noise": len(ops_names), "dup_extras": len(dup_names),
            "incident_retro": len(c["incident"]), "overlap": len(overlap),
            "union_total": len(union),
            "active_noncanonical": c["n_active_noncanon"],
        },
        "union_names": sorted(union),
    }


async def do_dry_run() -> dict:
    d = _driver()
    c = await _candidates(d, _cfg())
    return _report(c)


async def do_apply(manifest_path: str) -> dict:
    d = _driver()
    rep = _report(await _candidates(d, _cfg()))
    names = rep["union_names"]
    if not names:
        return {"applied": 0, "note": "no candidates"}
    # 逐条打 archived=true（幂等：只动当前非归档的）
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    await _q(d,
             "MATCH (n:Episodic) WHERE n.name IN $names AND NOT coalesce(n.archived,false) "
             "SET n.archived = true, n.archived_at = $ts", names=names, ts=ts)
    manifest = {"ts": ts, "archived": names, "report_counts": rep["counts"]}
    Path(manifest_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"applied": len(names), "manifest": manifest_path}


async def do_restore(manifest_path: str) -> dict:
    d = _driver()
    man = json.loads(Path(manifest_path).read_text())
    names = man.get("archived", [])
    await _q(d,
             "MATCH (n:Episodic) WHERE n.name IN $names "
             "REMOVE n.archived, n.archived_at", names=names)
    return {"restored": len(names)}


def main() -> int:
    ap = argparse.ArgumentParser(description="G3 治理噪音清理（dry-run/apply/restore）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--apply", action="store_true")
    g.add_argument("--restore", metavar="MANIFEST")
    ap.add_argument("--manifest", metavar="PATH", help="--apply 时写 manifest 到此")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.restore:
        print(json.dumps(asyncio.run(do_restore(args.restore)), ensure_ascii=False))
        return 0
    if args.apply:
        if not args.manifest:
            print("ERROR: --apply 需 --manifest PATH", file=sys.stderr); return 2
        print("⛔ 提醒：apply 前须先备份图谱、且确认 episode_search/dashboard/canonical_context "
              "已加 `WHERE NOT coalesce(n.archived,false)`（否则归档项仍显示）。", file=sys.stderr)
        print(json.dumps(asyncio.run(do_apply(args.manifest)), ensure_ascii=False))
        return 0

    rep = asyncio.run(do_dry_run())
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2)); return 0
    c = rep["counts"]
    print("=== G3 dry-run 候选（不写图；三类分列）===")
    print(f"活跃非 canonical episode 总数: {c['active_noncanonical']}")
    print(f"\nA. ops_noise（运维自指 bugfix，规则=is_ops_noise）: {c['ops_noise']} 条")
    for n in rep["A_ops_noise"][:40]:
        print(f"   - {n}")
    print(f"\nB. dup 多余份（每簇留最长一份，归档其余）: {c['dup_extras']} 条，{len(rep['B_dup_clusters'])} 簇")
    for cl in rep["B_dup_clusters"]:
        print(f"   簇[{cl['project']}] «{cl['title']}»  留 {cl['keep']}  归档 {cl['archive']}")
    print(f"\nC. INCIDENT-RETRO 胶囊: {c['incident_retro']}  {rep['C_incident_retro']}")
    print(f"\n重叠（ops_noise ∩ dup）: {c['overlap']} 条  {rep['overlap_ops_and_dup']}")
    print(f"\n合计去重后拟归档（union）: {c['union_total']} 条"
          f"（占活跃非canon {round(100*c['union_total']/max(c['active_noncanonical'],1),1)}%）")
    print("\n⚠️ 本轮仅 dry-run。apply 前置：①备份图谱 ②读路径加 WHERE NOT archived（server 改动+rebuild=生产 gate）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
