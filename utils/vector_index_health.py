"""RELATES_TO 边向量索引的墓碑比例（T-0347）。

FalkorDB 改一条边的**任何**属性都会让它换一个新文档号、旧号留下墓碑（2026-10-22 本机实测），
所以 `maxDocId / numDocuments` 随入图更新单调上涨，只有重建索引才回到约 1.0。比例高说明
索引被反复改写过；超过阈值就该按 docs/falkor-vector-index-rollout.md「Semantic search」重建。

只读 `CALL db.indexes()` 的元数据（毫秒级），不跑向量查询也不跑全扫，watchdog 每轮取一次也不会让 NAS 升温。
"""
from __future__ import annotations

LABEL = "RELATES_TO"
FIELD = "fact_embedding"


def edge_vector_index_stats(rows) -> dict | None:
    """从 `CALL db.indexes()` 的行里取 RELATES_TO 向量索引的统计。

    rows: 每行是 dict（graphiti FalkorDriver.execute_query 的记录，键为列名）或按列序的
    list/tuple（falkordb 客户端的 result_set：label, properties, types, options, language,
    stopwords, entitytype, status, info）。

    返回 {"status", "num_documents", "max_doc_id", "ratio"}；图里没有这个向量索引时返回 None。
    status 为 "building" 时比例不可判：重建期间 db.indexes() 里同一个标签会同时出现旧索引
    和建设中的新索引两行，数是半成品。
    """
    found = []
    for row in rows or []:
        if isinstance(row, dict):
            label, props, status, info = (row.get("label"), row.get("properties"),
                                          row.get("status"), row.get("info"))
        else:
            label, props, status, info = row[0], row[1], row[7], row[-1]
        if label == LABEL:
            found.append((props or [], str(status or ""), info if isinstance(info, dict) else {}))
    if not any(FIELD in props for props, _, _ in found):
        return None
    props, status, info = next(f for f in found if FIELD in f[0])
    building = len(found) > 1 or status != "OPERATIONAL"
    docs, max_id = info.get("numDocuments"), info.get("maxDocId")
    ratio = (round(max_id / docs, 3)
             if isinstance(docs, int) and isinstance(max_id, int) and docs > 0 else None)
    return {"status": "building" if building else "operational", "num_documents": docs,
            "max_doc_id": max_id, "ratio": None if building else ratio}
