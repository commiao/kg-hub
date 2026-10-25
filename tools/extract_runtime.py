"""Compatibility alias to the common evaluation runtime."""
import sys
from importlib import import_module
_module = import_module('llm_eval.executors.launcher')
if __name__ == "__main__":
    raise SystemExit(_module.main())
sys.modules[__name__] = _module
