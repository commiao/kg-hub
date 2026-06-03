"""
kg-hub weekly report — orchestrator.

Runs via launchd weekly (Sun 09:00 local). Generates two artifacts:
  1. ~/.kg-hub/reports/quality-baseline-YYYY-MM-DD.md   (KG composition snapshot)
  2. ~/.kg-hub/reports/decisions-7d-YYYY-MM-DD.md       (filter decisions over 7d)

Combined index goes to:
  ~/.kg-hub/reports/INDEX.md   (rolling pointer to latest)

Exit codes:
  0  success
  1  any sub-report fatal (still writes whatever succeeded)
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path


KG_HUB_ROOT = Path(__file__).resolve().parent.parent
PYTHON = KG_HUB_ROOT / "spike-graphiti" / ".venv" / "bin" / "python"
REPORT_DIR = Path.home() / ".kg-hub" / "reports"


def run(args: list[str]) -> int:
    print(f"$ {' '.join(args)}", flush=True)
    try:
        r = subprocess.run(args, cwd=str(KG_HUB_ROOT))
        return r.returncode
    except Exception as exc:
        print(f"[weekly_report] FAILED: {type(exc).__name__}: {exc}")
        return 1


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
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
