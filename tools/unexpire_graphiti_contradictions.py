"""T-0285 存量纠正：撤销 graphiti 矛盾判定写在边上的失效标记。

背景：graphiti 0.29.0 的矛盾判定把约 22% 的 RELATES_TO 边标成失效（invalid_at +
expired_at），抽样多数仍成立。入图侧已在 utils/edge_invalidation_shadow.py 改为只记
日志、不再写失效；本工具处理已经写进图里的那批。

判据（哪条失效来自矛盾判定）：graphiti 写失效只有两条来源 ——

  矛盾判定   invalid_at := 另一条边的 valid_at（旧边被新边推翻：取新边的；新边入图
             即过期：取那条日期更晚的已有边的）
  原文结束   invalid_at := 时间抽取 prompt 从事实原文读出的结束时间

所以「存在另一条边：来自不同 episode、在失效发生时已入图、valid_at 恰好等于本条
invalid_at」就是矛盾判定留下的指纹。整点零时（00:00:00）的值不算数：LLM 从原文读
出的日期多是这种形式，与别的边撞上是巧合（2026-10-14 抽样：「告警 5/29 触发、5/31
恢复」这类原文日期就落在里面），这部分保留。expired_at 与 created_at 的时间差不能
当判据：线上分布从 1 分钟到 1 天连续，没有断层。

模式：
  （默认）dry-run   只读（ro_query）。打印各类数量和抽样；带 --manifest P 时把待恢复的
                    边冻结成清单（JSONL，首行是元数据），apply 只认这份清单。
  --apply P         按清单分批清空 invalid_at/expired_at。每条都要求当前值仍等于清单
                    记录的值，否则跳过（dry-run 之后变过的边不动）。持 writer lock。
                    可重跑：已恢复的边因当前值为空而被跳过。
  --restore P       按清单写回原值；只动两字段当前都为空的边。
  --since ISO       只看 expired_at 晚于该时刻的边（入图恢复后补扫用）。

为什么回滚依据只能是清单文件：graphiti 保存边是 `SET e = $edge_data`，整体覆盖属性，
写在边上的备份字段遇到一次重存就没了。

在 kg-hub-server 容器内跑（FalkorDB 只在 NAS 本地可达），需 PYTHONPATH=/app。
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOOL = "unexpire_graphiti_contradictions"
RESTORE = "restore"
KEEP_TEXT_END = "keep_text_end"
KEEP_MIDNIGHT = "keep_midnight"
KEEP_NO_INVALID_AT = "keep_no_invalid_at"
BATCH = 500
TIMEOUT_MS = 60_000  # 线上 FalkorDB 的 TIMEOUT_MAX 不允许更大

LOAD = ("MATCH ()-[e:RELATES_TO]->() RETURN e.uuid, e.valid_at, e.invalid_at, "
        "e.expired_at, e.created_at, e.episodes[0]")
APPLY = ("UNWIND $rows AS r MATCH ()-[e:RELATES_TO {uuid: r.uuid}]->() "
         "WHERE e.invalid_at = r.invalid_at AND e.expired_at = r.expired_at "
         "SET e.invalid_at = NULL, e.expired_at = NULL RETURN count(e)")
RESTORE_Q = ("UNWIND $rows AS r MATCH ()-[e:RELATES_TO {uuid: r.uuid}]->() "
             "WHERE e.invalid_at IS NULL AND e.expired_at IS NULL "
             "SET e.invalid_at = r.invalid_at, e.expired_at = r.expired_at RETURN count(e)")


def _ts(value):
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _midnight(dt) -> bool:
    return (dt.hour, dt.minute, dt.second, dt.microsecond) == (0, 0, 0, 0)


def classify(rows, since=None):
    """rows: (uuid, valid_at, invalid_at, expired_at, created_at, episode) as stored.

    Returns [(row, category)] for expired edges (expired_at after ``since`` if given).
    """
    since = _ts(since)
    # valid_at -> [(created_at, episode)] sorted by created_at
    by_valid = defaultdict(list)
    for _, valid_at, _, _, created_at, episode in rows:
        if valid_at is not None and created_at is not None:
            by_valid[_ts(valid_at)].append((_ts(created_at), episode))
    for entries in by_valid.values():
        entries.sort(key=lambda entry: entry[0])
    created_keys = {valid: [entry[0] for entry in entries] for valid, entries in by_valid.items()}

    out = []
    for row in rows:
        _, _, invalid_at, expired_at, _, episode = row
        expired = _ts(expired_at)
        if expired is None or (since is not None and expired <= since):
            continue
        invalid = _ts(invalid_at)
        if invalid is None:
            out.append((row, KEEP_NO_INVALID_AT))
            continue
        if _midnight(invalid):
            out.append((row, KEEP_MIDNIGHT))
            continue
        entries = by_valid.get(invalid, ())
        end = bisect.bisect_right(created_keys.get(invalid, ()), expired)
        # The edge itself always shares its own episode, so it never counts.
        hit = any(entries[i][1] != episode for i in range(end))
        out.append((row, RESTORE if hit else KEEP_TEXT_END))
    return out


def _graph():
    from falkordb import FalkorDB
    return FalkorDB(
        host=os.environ.get("KG_HUB_FALKORDB_HOST", "falkordb"), port=6379,
        password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
    ).select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))


def load_rows(graph):
    return [tuple(r) for r in graph.ro_query(LOAD, timeout=TIMEOUT_MS).result_set]


def write_manifest(path, rows, *, since, counts):
    header = {"tool": TOOL, "created_at": datetime.now(timezone.utc).isoformat(),
              "since": since, "counts": counts, "rows": len(rows)}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        for uuid, _, invalid_at, expired_at, _, _ in rows:
            fh.write(json.dumps({"uuid": uuid, "invalid_at": str(invalid_at),
                                 "expired_at": str(expired_at)}) + "\n")


def read_manifest(path):
    with open(path, encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    if not lines or lines[0].get("tool") != TOOL:
        raise ValueError(f"{path} is not a {TOOL} manifest")
    header, rows = lines[0], lines[1:]
    if header.get("rows") != len(rows):
        raise ValueError(f"{path} is truncated: header says {header.get('rows')}, has {len(rows)}")
    return header, rows


def _batched_write(graph, query, rows, batch):
    changed = 0
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        changed += graph.query(query, {"rows": chunk}, timeout=TIMEOUT_MS).result_set[0][0]
    return {"rows": len(rows), "changed": changed, "skipped": len(rows) - changed}


def _locked(fn):
    from utils.writer_lock import writer_lock
    with writer_lock(owner=f"{TOOL}", timeout_seconds=120):
        return fn()


def apply(graph, rows, batch=BATCH):
    return _locked(lambda: _batched_write(graph, APPLY, rows, batch))


def restore(graph, rows, batch=BATCH):
    return _locked(lambda: _batched_write(graph, RESTORE_Q, rows, batch))


def dry_run(graph, *, since=None, manifest=None, samples=8):
    classified = classify(load_rows(graph), since)
    counts = Counter(category for _, category in classified)
    report = {"expired_considered": len(classified), "counts": dict(counts), "samples": {}}
    rng = random.Random(7)
    for category in counts:
        uuids = [row[0] for row, cat in classified if cat == category]
        picked = rng.sample(uuids, min(samples, len(uuids)))
        facts = graph.ro_query(
            "MATCH ()-[e:RELATES_TO]->() WHERE e.uuid IN $u "
            "RETURN e.uuid, left(e.fact, 140), e.valid_at, e.invalid_at",
            {"u": picked}, timeout=TIMEOUT_MS).result_set
        report["samples"][category] = [list(map(str, fact)) for fact in facts]
    if manifest:
        restore_rows = [row for row, cat in classified if cat == RESTORE]
        write_manifest(manifest, restore_rows, since=since, counts=dict(counts))
        report["manifest"] = {"path": manifest, "rows": len(restore_rows)}
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="撤销 graphiti 矛盾判定写下的失效标记（T-0285）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", metavar="MANIFEST")
    mode.add_argument("--restore", metavar="MANIFEST")
    ap.add_argument("--manifest", metavar="PATH", help="dry-run 时把待恢复的边冻结成清单")
    ap.add_argument("--since", metavar="ISO", help="只看 expired_at 晚于该时刻的边")
    args = ap.parse_args(argv)

    graph = _graph()
    if args.apply or args.restore:
        path = args.apply or args.restore
        _, rows = read_manifest(path)
        result = (apply if args.apply else restore)(graph, rows)
        print(json.dumps({"mode": "apply" if args.apply else "restore",
                          "manifest": path, **result}, ensure_ascii=False))
        return 0
    print(json.dumps(dry_run(graph, since=args.since, manifest=args.manifest),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
