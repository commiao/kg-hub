"""tools/agent_extract_remote.py — 在 NAS 上选出「真正待处理」的观测（只依赖标准库）。

由 tools/agent_extract.py 经 ssh 标准输入送到 NAS 执行：
    python3 - <claude-mem.db> <watermark.json> <after_id> <limit>

为什么不能只按 id 游标取：2026-10-17 冒烟时游标从 0 起，取到了 id 1、2 —— 它们 6 月就入图了，
但当年走的是旧水位线，图里没有 IngestedKey，服务端的查键接口认不出来。refinery 的
watermark.json 才是「哪些观测已处理」的正本：ingested / rejected / held 三个集合加 boundary_id
（live 线起点，更早的归 backlog 线，不归本执行器）。

held（待核验）也排除：它们的 IngestedKey 停在 needs_reconciliation，预抽取接口会回 409；
要消化它们得先用 tools/release_held.py 放回队列，那是另一步。
"""
from __future__ import annotations

import json
import sqlite3
import sys

SQL = (
    "SELECT o.id, o.project, o.type, o.title, o.subtitle, o.facts, o.narrative, o.concepts, "
    "o.files_read, o.files_modified, o.created_at, o.content_hash, o.generated_by_model, "
    "o.relevance_count, s.platform_source FROM observations o "
    "LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id "
    "WHERE o.id > ? ORDER BY o.id ASC LIMIT ?")


def load_watermark(path: str) -> tuple[int, set[int]]:
    try:
        wm = json.load(open(path))
    except (OSError, ValueError):
        return 0, set()
    excl: set[int] = set()
    for key in ("ingested", "rejected", "held"):
        v = wm.get(key) or []
        excl.update(int(x) for x in (v if isinstance(v, list) else list(v)))
    return int(wm.get("boundary_id") or 0), excl


def select_pending(db_path: str, wm_path: str, after_id: int, limit: int,
                   chunk: int = 500, max_scan: int = 20000) -> dict:
    boundary, excl = load_watermark(wm_path)
    after = max(int(after_id), boundary)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    rows: list[dict] = []
    scanned = skipped = 0
    cursor = after
    while len(rows) < limit and scanned < max_scan:
        batch = [dict(r) for r in conn.execute(SQL, (cursor, chunk))]
        if not batch:
            break
        for r in batch:
            scanned += 1
            cursor = r["id"]
            if r["id"] in excl:
                skipped += 1
                continue
            rows.append(r)
            if len(rows) >= limit:
                break
    conn.close()
    return {"rows": rows, "boundary": boundary, "after": after, "scanned": scanned,
            "skipped_by_watermark": skipped, "excluded_total": len(excl),
            "exhausted": scanned < max_scan and len(rows) < limit}


if __name__ == "__main__":
    db, wm, after, limit = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    print(json.dumps(select_pending(db, wm, after, limit), ensure_ascii=False))
