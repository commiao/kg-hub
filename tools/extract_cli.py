"""Stable released entry point; the excluded working directory never selects code.

Usage: PY ~/.local/share/kg-hub/current/tools/extract_cli.py campaign|agent|bench ...
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_ROOT))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    routes = {"campaign": "tools.extract_campaign", "agent": "tools.agent_extract", "bench": "tools.extract_bench",
              "repair": "tools.extraction_repair"}
    if not args or args[0] not in routes:
        raise SystemExit("usage: extract_cli.py campaign|agent|bench|repair <command> ...")
    return importlib.import_module(routes[args.pop(0)]).main(args)


if __name__ == "__main__":
    raise SystemExit(main())
