"""tools/feedback_digest.py — 使用反馈每日摘要(veto-after 的监督面)。

反馈待办⑥的自动处理规则会直接执行够格的动作(auto_retire/auto_noted);
本脚本每天把过去 24h 的自动处理 + 人工处理 + 当前待拍板汇总推飞书,
让"事后可否决"真的有"事后看一眼"的入口。无活动且无积压时静默退出。

Usage:
  python -m tools.feedback_digest            # 有活动才推飞书(kg-hub webhook)
  python -m tools.feedback_digest --dry      # 只打印,不推
  python -m tools.feedback_digest --always   # 无活动也推(心跳确认用)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".claude-mem" / ".env", override=False)
except Exception:
    pass

FEISHU_SEND = Path.home() / ".claude" / "skills" / "feishu-notify" / "scripts" / "send.py"
INBOX_URL = "http://100.123.208.32:17171/dashboard/inbox"

_VLABEL = {"stale": "过时", "conflict": "冲突", "supplement": "可补充", "inaccurate": "不准确",
           "irrelevant": "召回离题", "missed": "该召回却没召回"}
_ALABEL = {"auto_retire": "自动退休", "auto_noted": "已记录", "verified": "人工·标已验证",
           "retire": "人工·退休", "dismiss": "人工·忽略"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="print only, no feishu push")
    ap.add_argument("--always", action="store_true", help="push even with no activity")
    args = ap.parse_args()

    from falkordb import FalkorDB

    # Mac→NAS 链路会假超时(tailscale 抖动/NAS 冷启动)——递进重试,仍连不上按
    # 瞬时跳过退 0:日报缺一天不是故障,别让 launchd 记 errored(2026-07-31 实发,
    # 同 capsule_watch 的教训)。
    import time as _time
    g = None
    for _i in range(3):
        try:
            db = FalkorDB(
                host=os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1"),
                port=int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379")),
                password=os.environ.get("KG_HUB_FALKORDB_PASSWORD") or None,
            )
            g = db.select_graph(os.environ.get("KG_HUB_FALKORDB_DATABASE", "kg_hub"))
            g.ro_query("RETURN 1")
            break
        except Exception as exc:  # noqa: BLE001
            if _i < 2:
                _time.sleep(10)
                continue
            print(f"[digest] NAS 不可达(瞬时,本次跳过): {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 0
    cut = (datetime.now(tz=timezone.utc) - timedelta(hours=24)).isoformat()

    handled = g.query(
        "MATCH (f:UsageFeedback) WHERE f.status IN ['auto_handled','handled'] "
        "AND coalesce(f.handled_at,'') >= $cut "
        "RETURN f.episode_name, f.verdict, f.handled_action "
        "ORDER BY f.handled_at DESC LIMIT 20", params={"cut": cut}).result_set
    pending = g.query(
        "MATCH (f:UsageFeedback) WHERE f.status = 'pending' "
        "RETURN f.episode_name, f.verdict, coalesce(f.updated_at, f.created_at) "
        "ORDER BY 3 ASC LIMIT 10").result_set
    # 检索质量诊断(不需拍板,只报数——搜得准不准的体温计)
    diag = g.query(
        "MATCH (f:UsageFeedback) WHERE f.status = 'diagnostic' "
        "AND coalesce(f.updated_at, f.created_at) >= $cut "
        "RETURN coalesce(f.query,''), f.verdict, coalesce(f.report_count,1) "
        "ORDER BY 3 DESC LIMIT 5", params={"cut": cut}).result_set

    if not handled and not pending and not diag and not args.always:
        print("[digest] no activity in 24h, nothing pending — skip")
        return 0

    L = [f"📮 kg-hub 使用反馈日报 {datetime.now().strftime('%m-%d')}"]
    if handled:
        auto = [h for h in handled if str(h[2]).startswith("auto")]
        manual = [h for h in handled if not str(h[2]).startswith("auto")]
        if auto:
            L.append(f"🤖 24h 自动处理 {len(auto)} 条(待办⑥底部可撤销):")
            for name, verdict, act in auto[:8]:
                L.append(f"  · {name} [{_VLABEL.get(verdict, verdict)}→{_ALABEL.get(act, act)}]")
        if manual:
            L.append(f"👤 人工处理 {len(manual)} 条")
    if pending:
        L.append(f"⏳ 待你拍板 {len(pending)} 条(conflict/已验证知识类,机器不代劳):")
        for name, verdict, _ in pending[:5]:
            L.append(f"  · {name} [{_VLABEL.get(verdict, verdict)}]")
    if diag:
        L.append(f"🔍 检索质量:{len(diag)} 条离题/漏召回上报(只是度量,不用你处理):")
        for q, verdict, rc in diag[:3]:
            L.append(f"  · 「{q}」{_VLABEL.get(verdict, verdict)}" + (f" ×{rc}" if rc > 1 else ""))
    if not handled and not pending and not diag:
        L.append("(无活动,心跳确认)")
    L.append(INBOX_URL)
    msg = "\n".join(L)
    print(msg)

    if args.dry:
        return 0
    try:
        subprocess.run(
            [sys.executable, str(FEISHU_SEND), msg,
             "--webhook", "kg-hub", "--title", "kg-hub 使用反馈日报"],
            check=False, timeout=20)
    except Exception as exc:  # noqa: BLE001
        print(f"[feishu] skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
