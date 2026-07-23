"""tools/dedup_capsules_once.py — 一次性去重:移除 .md/.md.gz 重复入图的较新副本。

背景: OpenClaw 胶囊在 6 月原始入图 + 7-22 gz 支持上线时重新入图,产生 33 对
同名同内容(sha 一致)的 Episodic。每对保留 created_at 较早者(时间锚更准),
用 graphiti.remove_episode 安全移除较新者(自动回退其抽取贡献,共享实体/边保留)。

前置已完成: dedup-plan.json(内容一致性校验通过,零分歧)、
data/dedup-backup-YYYYMMDD.json(33 待删节点全文备份,可恢复)。

用法: python -m tools.dedup_capsules_once --plan <path> [--dry-run]
幂等: 目标已不存在时 remove_episode 静默跳过。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

from graphiti_client import build_graphiti  # noqa: E402


async def _main(plan_path: str, dry_run: bool) -> int:
    plan = json.loads(Path(plan_path).read_text())
    targets = [d["uuid"] for d in plan["delete"]]
    print(f"[dedup] {len(targets)} duplicate episodes to remove (keep earlier of each pair)")
    if dry_run:
        for d in plan["delete"]:
            print(f"  [dry] would remove {d['uuid']}  {d['sd']}")
        return 0
    g = await build_graphiti(fresh=False)
    ok = fail = 0
    for u in targets:
        try:
            await g.remove_episode(u)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  ✗ {u}: {type(exc).__name__}: {exc}")
    print(f"[dedup] done: removed={ok} failed={fail}")
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True, help="dedup-plan.json path")
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(_main(ap.plan if False else ap.parse_args().plan,
                             "--dry-run" in sys.argv))


if __name__ == "__main__":
    sys.exit(main())
