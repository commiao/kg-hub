"""Compatibility alias; business rules live in the installed KG evaluation adapter."""
import sys
from importlib import import_module
_module = import_module('kg_hub_eval.business.utils.semantic_regression')
if __name__ == "__main__":
    raise SystemExit(_module.main())
sys.modules[__name__] = _module
