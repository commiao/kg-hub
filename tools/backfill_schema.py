"""tools/backfill_schema.py — 存量 Episodic 回填一等来源/内容元数据(北极星 §9)。

给图里全部 Episodic 补 origin_device/origin_tool/origin_project/durability/kind/
kind_confidence:
  - origin / durability / 确定性 kind:纯派生(utils.origin),无 LLM
  - kind 派生不出(unclassified)且是 openclaw-capsule-*:跑 LLM 分类器 pass
    (Mac 本地 anthropic,凭据取 ~/.claude-mem/.env);低置信弃权=unclassified
  - 派生不出的 origin 一律标 unknown(不留空,北极星 P2)

安全: --dry-run 先看分布不写;写前自动备份受影响节点旧值到 data/。
幂等: 默认只补 origin_device IS NULL 的节点;--force 全量重算。

用法:
  python -m tools.backfill_schema --dry-run
  python -m tools.backfill_schema            # 备份后回填
  python -m tools.backfill_schema --force    # 重算全部
  python -m tools.backfill_schema --no-llm   # 跳过 LLM,胶囊 kind 留 unclassified
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

from utils.origin import (  # noqa: E402
    derive_origin, derive_durability, derive_kind, KIND_PROMPT, parse_kind_json,
)

KIND_CONF_THRESHOLD = 0.7


def _graph():
    from falkordb import FalkorDB
    db = FalkorDB(
        host=os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1"),
        port=int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379")),
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
    )
    return db.select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))


async def _llm_kind(client, body: str) -> tuple[str, float, bool]:
    """独立分类器 pass(与 server.classify_kind 同 prompt/解析)。
    返回 (kind, conf, failed);failed=True 表示 LLM 调用异常(区别于"跑成功但判 unclassified")。"""
    try:
        resp = await client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "qwen3.6-plus"),
            max_tokens=120,
            messages=[{"role": "user", "content": KIND_PROMPT.format(body=(body or "")[:6000])}],
            extra_body={"thinking": {"type": "disabled"}},
        )
        k, c = parse_kind_json("".join(getattr(b, "text", "") for b in resp.content))
        return (k, c, False)
    except Exception as e:
        print(f"  [llm-fail] {type(e).__name__}: {e}")
        return ("unclassified", 0.0, True)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="重算全部(默认只补未回填的)")
    ap.add_argument("--no-llm", action="store_true", help="不跑 LLM,胶囊 kind 留 unclassified")
    ap.add_argument("--retry-llm", action="store_true",
                    help="只重试 kind 仍 unclassified 的胶囊(修 D2:LLM 曾失败的重跑,不动其他)")
    args = ap.parse_args()

    g = _graph()
    if args.retry_llm:
        where = ("WHERE n.name STARTS WITH 'openclaw-capsule-' "
                 "AND (n.kind IS NULL OR n.kind = 'unclassified') ")
    elif args.force:
        where = ""
    else:
        where = "WHERE n.origin_device IS NULL "
    rows = g.query(
        "MATCH (n:Episodic) " + where +
        "RETURN n.uuid, n.name, n.source_description, substring(coalesce(n.content,''),0,6000), "
        "n.kind, n.kind_confidence"
    ).result_set
    print(f"[backfill] {len(rows)} nodes to process{' (force)' if args.force else ''}")
    if not rows:
        print("[done] nothing to do")
        return 0

    # 先算确定性部分 + 标记需要 LLM 的胶囊。existing_* 用于 D1 不降级保护。
    plan = []
    need_llm = []
    for uuid, name, sd, content, ex_kind, ex_conf in rows:
        o = derive_origin(name or "", sd or "")
        dur = derive_durability(name or "", content or "")
        kind, conf = derive_kind(name or "", sd or "")
        rec = dict(uuid=uuid, **o, durability=dur, kind=kind, kind_confidence=conf,
                   _ex_kind=ex_kind, _ex_conf=ex_conf or 0.0)
        if conf < KIND_CONF_THRESHOLD and (name or "").startswith("openclaw-capsule-") and not args.no_llm:
            need_llm.append((rec, content))
        plan.append(rec)

    if args.dry_run:
        print(f"[dry] 确定性派生完成;需 LLM 分类的胶囊: {len(need_llm)}")
        print("  origin_tool:", dict(Counter(r["origin_tool"] for r in plan)))
        print("  origin_device:", dict(Counter(r["origin_device"] for r in plan)))
        print("  durability:", dict(Counter(r["durability"] for r in plan)))
        print("  kind(确定性,LLM前):", dict(Counter(r["kind"] for r in plan)))
        return 0

    # LLM 分类(仅胶囊,串行控速;client 复用一个,修 D5)
    llm_fail = 0
    if need_llm:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(
            auth_token=os.environ["ANTHROPIC_AUTH_TOKEN"],
            base_url=os.environ["ANTHROPIC_BASE_URL"], max_retries=2, timeout=90.0)
        try:
            for rec, content in need_llm:
                kind, conf, failed = await _llm_kind(client, content)
                llm_fail += 1 if failed else 0
                if conf < KIND_CONF_THRESHOLD:
                    kind, conf = "unclassified", 0.0   # 修 D3:unclassified 配 0 置信
                rec["kind"], rec["kind_confidence"] = kind, conf
                print(f"  [kind] {rec['uuid'][:12]} → {kind} ({conf}){' [FAILED]' if failed else ''}")
        finally:
            await client.close()

    # D1 不降级保护:重算出的 unclassified 绝不覆盖已有的已分类值(人工/显式/上轮 LLM)。
    for rec in plan:
        if rec["kind"] == "unclassified" and rec["_ex_kind"] not in (None, "", "unclassified"):
            rec["kind"], rec["kind_confidence"] = rec["_ex_kind"], rec["_ex_conf"]
            rec["_preserved"] = True

    # 备份受影响节点旧值
    bak = g.query(
        "MATCH (n:Episodic) WHERE n.uuid IN $us "
        "RETURN n.uuid, n.origin_device, n.origin_tool, n.origin_project, "
        "n.durability, n.kind, n.kind_confidence",
        params={"us": [r["uuid"] for r in plan]}).result_set
    Path("data").mkdir(exist_ok=True)   # 修 D4:不依赖 cwd 下 data/ 已存在
    bakpath = f"data/backfill-schema-backup-{datetime.now(tz=timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    Path(bakpath).write_text(json.dumps(
        [dict(zip(["uuid", "origin_device", "origin_tool", "origin_project",
                   "durability", "kind", "kind_confidence"], r)) for r in bak],
        ensure_ascii=False, indent=1))
    print(f"[backup] {len(bak)} nodes old values → {bakpath}")

    # 写入
    for r in plan:
        g.query(
            "MATCH (n:Episodic {uuid:$u}) SET "
            "n.origin_device=$dev, n.origin_tool=$tool, n.origin_project=$proj, "
            "n.durability=$dur, n.kind=$kind, n.kind_confidence=$conf",
            params={"u": r["uuid"], "dev": r["origin_device"], "tool": r["origin_tool"],
                    "proj": r["origin_project"], "dur": r["durability"],
                    "kind": r["kind"], "conf": r["kind_confidence"]})
    preserved = sum(1 for r in plan if r.get("_preserved"))
    print(f"[done] backfilled {len(plan)} nodes"
          + (f" (D1保护:保留了 {preserved} 个已分类值不被降级)" if preserved else ""))
    print("  final kind:", dict(Counter(r["kind"] for r in plan)))
    if llm_fail:
        # 修 D2:LLM 有失败就大声报警 + 非零退出;失败项可事后 --retry-llm 单独重跑
        print(f"\n⚠️  [WARN] {llm_fail}/{len(need_llm)} 个胶囊 LLM 分类失败,已留 unclassified。")
        print("    修复凭据/网络后跑:python -m tools.backfill_schema --retry-llm(只重试这些,不动其他)")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
