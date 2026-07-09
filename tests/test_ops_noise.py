"""utils.ops_noise.is_ops_noise 的边界测试（两颗钉子）。

无 pytest 环境下可直接跑：python tests/test_ops_noise.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.ops_noise import is_ops_noise  # noqa: E402

CFG = {
    "ops_noise": {
        "enabled": False,  # 分类器不看 enabled —— 下面用例证明关着也照样分类
        "self_markers": ["kg-hub", "kg_hub"],
        "keywords": ["docker", "falkordb", "keepalive", "push hook", "l2 fallback",
                     "daemon", "compose", "watchdog", "redeploy", "container",
                     "tailscale", "dump.rdb"],
        "min_keyword_hits": 2,
    }
}

CASES = [
    # (name, obs, expected)
    ("kg-hub 运维 bugfix 命中2词 → True",
     {"type": "bugfix", "project": "workspace_claudeCode",
      "narrative": "kg-hub FalkorDB KeepAlive deadlock; restarted docker daemon"}, True),

    ("标记在 title、narrative 无标记 → True（title 纳入检索）",
     {"type": "bugfix", "project": "workspace_claudeCode",
      "title": "[BUGFIX] kg-hub full-chain repair",
      "narrative": "restarted docker daemon and the falkordb container"}, True),

    ("钉子②：enabled=false 仍能分类（体检器可用）",
     {"type": "bugfix", "project": "workspace_claudeCode",
      "narrative": "kg-hub: docker compose redeploy fixed the watchdog"}, True),

    ("钉子①：decision 即使写满 kg-hub+Docker/FalkorDB → False",
     {"type": "decision", "project": "workspace_claudeCode",
      "narrative": "decided to run kg-hub FalkorDB in docker with a keepalive watchdog"}, False),

    ("钉子①：security_note 含 kg-hub+运维词 → False",
     {"type": "security_note", "project": "workspace_claudeCode",
      "narrative": "kg-hub container exposed; falkordb port; docker daemon"}, False),

    ("他项目 infra bugfix 不提 kg-hub → False（无自我标记）",
     {"type": "bugfix", "project": "workspace_codex",
      "narrative": "Evaluation script docker scan fails; fixed the container build"}, False),

    ("Exit node/tailscale bugfix 不提 kg-hub → False（正确排除，对应实测集 B）",
     {"type": "bugfix", "project": "workspace_claudeCode",
      "narrative": "Exit node routing fixed — IP forwarding + tailscale + container persisted"}, False),

    ("kg-hub bugfix 但只命中1词 → False（未达阈值）",
     {"type": "bugfix", "project": "workspace_claudeCode",
      "narrative": "kg-hub: tuned the docker memory limit"}, False),

    ("kg-hub 真业务 bugfix（无运维词）→ False",
     {"type": "bugfix", "project": "workspace_claudeCode",
      "narrative": "fixed kg-hub capsule ranking tie-break bug in canonical_context"}, False),

    ("facts 里的运维词也计入（narrative 只有标记）→ True",
     {"type": "bugfix", "project": "workspace_claudeCode", "narrative": "kg-hub infra repair",
      "facts": ["restarted falkordb container", "fixed docker daemon"]}, True),
]


def run() -> None:
    failures = []
    for name, obs, expected in CASES:
        got = is_ops_noise(obs, CFG)
        ok = got == expected
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  (got={got}, want={expected})")
        if not ok:
            failures.append(name)
    assert not failures, f"{len(failures)} case(s) failed: {failures}"
    print(f"\nALL {len(CASES)} CASES PASS")


if __name__ == "__main__":
    run()
