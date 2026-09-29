"""Shared policy for a batched model answer that fails its own contract.

A batched request replaces N upstream per-item requests. When the paid answer
is unusable (schema validation, or rows that do not map one-to-one onto the
request), only that batch falls back to the upstream per-item prompts. Every
other failure (transport, reconciliation, cancellation) keeps propagating.
"""
from __future__ import annotations

import logging

from pydantic import ValidationError

_FALLBACKS_TOTAL: dict[str, int] = {}
log = logging.getLogger("kg_hub.batch_fallback")


class BatchAnswerMismatch(RuntimeError):
    """The batched rows do not correspond one-to-one to the requested items."""


def is_bad_batch_answer(exc: BaseException) -> bool:
    return isinstance(exc, (ValidationError, BatchAnswerMismatch))


def note_fallback(stage: str, items: int, exc: BaseException) -> None:
    _FALLBACKS_TOTAL[stage] = _FALLBACKS_TOTAL.get(stage, 0) + 1
    log.warning("[batch_fallback] stage=%s items=%d reason=%s: %s",
                stage, items, type(exc).__name__, (str(exc).splitlines() or [""])[0][:200])


def batch_fallbacks_total() -> dict[str, int]:
    return dict(_FALLBACKS_TOTAL)
