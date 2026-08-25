"""Regression test for Qoder's independently labeled claude-mem capture.

Run directly: python3 tests/test_qoder_cm_hook.py
"""
from __future__ import annotations

from pathlib import Path


HOOK = Path(__file__).resolve().parent.parent / "tools" / "qoder_cm_hook.sh"


def run() -> None:
    source = HOOK.read_text(encoding="utf-8")

    assert 'worker-service.cjs" hook qoder "$MODE"' in source, (
        "Qoder must invoke claude-mem with platform_source=qoder"
    )
    assert 'worker-service.cjs" hook claude-code "$MODE"' not in source, (
        "Qoder must not be folded into Claude's source"
    )
    print("PASS qoder claude-mem source is independent")


if __name__ == "__main__":
    run()
