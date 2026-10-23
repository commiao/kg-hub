"""NAS 端：按时间窗导出 claude-mem 观测（只依赖标准库，经 ssh 标准输入送过去执行）。
参数：db 路径、天数、每行最长正文字符（0 = 不截）。输出 JSON 行，供 tools/obs_bench.py sample 在本机分层抽样。
只读；一次读 30 天约 3 万行带正文，属 NAS 账本库的大读取，只在低峰（23 点后）跑。"""
import json
import sqlite3
import sys

db, days, cut = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
q = ("SELECT o.id, o.project, o.type, o.title, o.subtitle, o.facts, o.narrative, o.concepts, o.files_modified, "
     "o.created_at, o.content_hash, o.generated_by_model, o.relevance_count, o.memory_session_id, s.platform_source "
     "FROM observations o LEFT JOIN sdk_sessions s ON o.memory_session_id = s.memory_session_id "
     "WHERE o.created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)")
for r in con.execute(q, (f"-{days} days",)):
    d = dict(r)
    if cut:
        d["narrative"] = (d["narrative"] or "")[:cut]
    sys.stdout.write(json.dumps(d, ensure_ascii=False) + "\n")
