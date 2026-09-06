"""
kg-hub weekly report — orchestrator.

Runs via launchd weekly (Sun 09:00 local). Generates two artifacts:
  1. ~/.kg-hub/reports/quality-baseline-YYYY-MM-DD.md   (KG composition snapshot)
  2. ~/.kg-hub/reports/decisions-7d-YYYY-MM-DD.md       (filter decisions over 7d)

Combined index goes to:
  ~/.kg-hub/reports/INDEX.md   (rolling pointer to latest)

Exit codes:
  0  success，或 NAS 瞬时不可达（跳过本轮，不算 errored）
  1  NAS 可达但子报告真失败（还是会写下已成功的部分）

Mac→NAS 连接韧性（2026-08-13，同一坑第三次）：子报告分别直连 FalkorDB:6379
（quality_audit / kg_eval）和 HTTP :17171（usage_ranking）。NAS 掉线/重启/温度
门控期间这些连接会被拒，此前直接 exit 1，launchd 就把本任务标 errored——而
KPI 报告因此从 2026-07-26 起断了三周，恰好断在图规模 +81% 的剧变期，等于盲飞。
纪律与 tools/capsule_watch.py、tools/feedback_digest.py 一致：**前置就绪等待 +
递进重试 + 瞬时失败退 0**。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except Exception:
    pass

from utils.wait_for_dependencies import wait_for_port  # noqa: E402

KG_HUB_ROOT = Path(__file__).resolve().parent.parent
PYTHON = KG_HUB_ROOT / "spike-graphiti" / ".venv" / "bin" / "python"
REPORT_DIR = Path.home() / ".kg-hub" / "reports"


def nas_reachable(timeout_seconds: float = 60.0) -> bool:
    """两条依赖都要通:FalkorDB(直连)+ kg-hub server(HTTP)。复用 wait_for_port 原语。"""
    db_ok = wait_for_port(
        os.environ.get("KG_HUB_FALKORDB_HOST", "127.0.0.1"),
        int(os.environ.get("KG_HUB_FALKORDB_PORT", "6379")),
        timeout_seconds=timeout_seconds, label="FalkorDB")
    u = urllib.parse.urlparse(os.environ.get("KG_HUB_URL", "http://100.123.208.32:17171"))
    srv_ok = wait_for_port(u.hostname or "100.123.208.32", u.port or 17171,
                           timeout_seconds=timeout_seconds, label="kg-hub-server")
    return db_ok and srv_ok


def run(args: list[str], retries: int = 1) -> int:
    """跑一个子报告;失败后按 15/30s 递进重试(NAS 抖动常在秒级恢复)。"""
    for attempt in range(retries + 1):
        print(f"$ {' '.join(args)}" + (f"  (retry {attempt})" if attempt else ""), flush=True)
        try:
            rc = subprocess.run(args, cwd=str(KG_HUB_ROOT)).returncode
        except Exception as exc:  # noqa: BLE001
            print(f"[weekly_report] FAILED: {type(exc).__name__}: {exc}")
            rc = 1
        if rc == 0 or attempt >= retries:
            return rc
        time.sleep(15.0 * (attempt + 1))
    return rc


def update_index() -> None:
    date_tag = datetime.now().strftime("%Y-%m-%d")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# kg-hub Reports Index",
        "",
        f"_Last updated: {datetime.now().isoformat()}_",
        "",
        "## Latest weekly artifacts",
        "",
        f"- [quality-baseline-{date_tag}.md](quality-baseline-{date_tag}.md)",
        f"- [decisions-7d-{date_tag}.md](decisions-7d-{date_tag}.md)",
        f"- [kg-eval-{date_tag}.md](kg-eval-{date_tag}.md)",
        f"- [usage-ranking-{date_tag}.md](usage-ranking-{date_tag}.md)",
        "",
        "## Archive (all reports)",
        "",
    ]
    for f in sorted(REPORT_DIR.glob("*.md"), reverse=True):
        if f.name == "INDEX.md":
            continue
        lines.append(f"- [{f.name}]({f.name})")
    (REPORT_DIR / "INDEX.md").write_text("\n".join(lines))


def main() -> int:
    print(f"[weekly_report] start {datetime.now().isoformat()}")
    rc = 0

    if not PYTHON.exists():
        print(f"[weekly_report] FATAL: python not found at {PYTHON}")
        return 1

    # 前置就绪门:NAS 掉线/重启/温度门控期间跳过本轮而不是标 errored。
    # 一个监控漏跑一次不是故障;把网络抖动当故障才会让真故障淹没在噪音里。
    if not nas_reachable(timeout_seconds=60.0):
        print("[weekly_report] NAS 不可达(FalkorDB 或 server)——瞬时,跳过本轮,"
              "下周或手动重跑即可", file=sys.stderr)
        return 0

    rc |= run([str(PYTHON), "-m", "tools.quality_audit"])
    rc |= run([str(PYTHON), "-m", "tools.decisions_summary", "--window", "7d"])
    # Layer D — retrieval quality. Non-fatal to the weekly run: a recall dip
    # is a finding to surface, not a reason to fail the whole report. We OR
    # its rc in for visibility but don't let it mask the other two.
    eval_rc = run([str(PYTHON), "-m", "tools.kg_eval"])
    if eval_rc != 0:
        print(f"[weekly_report] kg_eval recall below gate (rc={eval_rc}) — see kg-eval report")
    # Lindy / usage ranking — informational only, never fails the report.
    # Surfaces the implicit-feedback signal from the PUSH hook so promote/
    # demote decisions can be made by looking, not guessing.
    run([str(PYTHON), "-m", "tools.usage_ranking"])

    update_index()
    print(f"[weekly_report] done rc={rc}")
    if rc != 0 and not nas_reachable(timeout_seconds=5.0):
        # 跑的过程中 NAS 掉了 → 归瞬时,别把 launchd 标 errored
        print("[weekly_report] 子报告失败且 NAS 已不可达 → 判为瞬时,退 0", file=sys.stderr)
        return 0
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
