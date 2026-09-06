"""Single project-owned dotenv boundary for kg-hub.

kg-hub must never inherit credentials from claude-mem's private directory.  A
deployment may point ``KG_HUB_ENV_FILE`` at another project-owned file, but the
path must not resolve inside ``~/.claude-mem``.
"""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


def env_file() -> Path:
    path = Path(os.environ.get("KG_HUB_ENV_FILE", str(DEFAULT_ENV_FILE))).expanduser()
    claude_root = (Path.home() / ".claude-mem").resolve()
    try:
        resolved = path.resolve()
        if resolved == claude_root or claude_root in resolved.parents:
            raise RuntimeError("KG_HUB_ENV_FILE must not use ~/.claude-mem")
    except OSError as exc:
        raise RuntimeError("KG_HUB_ENV_FILE cannot be resolved") from exc
    return path


def load_kg_hub_env(*, override: bool = False) -> bool:
    from dotenv import load_dotenv

    return bool(load_dotenv(env_file(), override=override))
