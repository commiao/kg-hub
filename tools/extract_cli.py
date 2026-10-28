"""Stable released entry point; the excluded working directory never selects code.

Usage: ~/.local/share/kg-hub/current/tools/extract campaign|agent|bench|repair|nodes ...
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
            # A long-lived native can inherit the previous adapter's recursion
            # marker. Never execute this root under a mismatched identity:
            # reselect the active, hash-verified adapter once, only for a known
            # published prior release. Unknown identities still fail closed.
            prior = CODE_ROOT.parent / delegated / 'RELEASE.json'
            known = (len(delegated)==40 and all(c in '0123456789abcdef' for c in delegated)
                     and prior.is_file() and json.loads(prior.read_text()).get('commit')==delegated
                     and json.loads(prior.read_text()).get('name')=='kg-hub')
            if not known or os.environ.get('LLM_EVAL_BACKEND_RESELECTED'):
                raise SystemExit("delegated campaign backend identity differs")
            from llm_eval.cli import main as common_main
            previous=os.environ.pop('LLM_EVAL_DELEGATED_BACKEND')
            os.environ['LLM_EVAL_BACKEND_RESELECTED']='1'
            try:
                return common_main(["campaign", "--scenario", "kg_hub.entity_extract", "--", *args[1:]])
            finally:
                os.environ['LLM_EVAL_DELEGATED_BACKEND']=previous
                os.environ.pop('LLM_EVAL_BACKEND_RESELECTED',None)
    if args == ["--runtime"]:
        print(json.dumps({"python": sys.executable, "version": sys.version.split()[0], "code_root": str(CODE_ROOT)}))
        return 0
    routes = {"campaign": "tools.extract_campaign", "agent": "tools.agent_extract", "bench": "tools.extract_bench",
              "repair": "tools.extraction_repair", "nodes": "tools.graph_governance", "runtime": "tools.extract_runtime",
              "regression": "tools.extract_regression", "eval": "tools.extract_evaluation"}
    if not args or args[0] not in routes:
        raise SystemExit("usage: extract_cli.py campaign|agent|bench|repair|nodes|runtime|regression|eval <command> ...")
    return importlib.import_module(routes[args.pop(0)]).main(args)


if __name__ == "__main__":
    raise SystemExit(main())
