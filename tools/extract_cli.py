"""Stable released entry point; the excluded working directory never selects code.

Usage: ~/.local/share/kg-hub/current/tools/extract campaign|agent|bench|repair ...
The shell entry chooses the runtime; this module also supports explicit Python callers.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_ROOT))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "campaign" and os.environ.get("LLM_EVAL_ACTIVE_BUNDLE"):
        delegated = os.environ.get("LLM_EVAL_DELEGATED_BACKEND")
        if not delegated:
            from llm_eval.cli import main as common_main
            return common_main(["campaign", "--scenario", "kg_hub.entity_extract", "--", *args[1:]])
        release = json.loads((CODE_ROOT / "RELEASE.json").read_text())
        if release.get("commit") != delegated:
            raise SystemExit("delegated campaign backend identity differs")
    if args == ["--runtime"]:
        print(json.dumps({"python": sys.executable, "version": sys.version.split()[0], "code_root": str(CODE_ROOT)}))
        return 0
    routes = {"campaign": "tools.extract_campaign", "agent": "tools.agent_extract", "bench": "tools.extract_bench",
              "repair": "tools.extraction_repair", "runtime": "tools.extract_runtime",
              "regression": "tools.extract_regression", "eval": "tools.extract_evaluation"}
    if not args or args[0] not in routes:
        raise SystemExit("usage: extract_cli.py campaign|agent|bench|repair|runtime|regression|eval <command> ...")
    return importlib.import_module(routes[args.pop(0)]).main(args)


if __name__ == "__main__":
    raise SystemExit(main())
